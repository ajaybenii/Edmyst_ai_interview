"""
Post–user-audio upload: STT (Vertex), S3 communication JSON, MongoDB Tavus-style transcript.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

logger = logging.getLogger(__name__)


def _render(msg: str) -> None:
    print(f"[RENDER] {msg}", flush=True)


PENDING_LOCK = asyncio.Lock()
# key -> list[{"role","content"}]
PENDING_LIVE_TRANSCRIPTS: dict[str, list[dict[str, str]]] = {}

MONGO_PATH_TRANSCRIPT = "converse_data.application.transcription_ready.properties.transcript"


def transcript_cache_key(assessment_result_id: str, video_token: str) -> str:
    return f"{assessment_result_id or 'no_result'}|{video_token or 'no_token'}"


async def store_pending_live_transcript(
    assessment_result_id: str,
    video_token: str,
    messages: list[dict[str, str]],
) -> None:
    key = transcript_cache_key(assessment_result_id, video_token)
    async with PENDING_LOCK:
        PENDING_LIVE_TRANSCRIPTS[key] = messages
    logger.info("[Transcript] Stored pending live transcript key=%s turns=%s", key, len(messages))


async def pop_pending_live_transcript(
    assessment_result_id: str,
    video_token: str,
    max_wait_seconds: float = 15.0,
) -> list[dict[str, str]]:
    """Wait until frontend has submitted turns (upload may finish slightly after POST)."""
    key = transcript_cache_key(assessment_result_id, video_token)
    deadline = time.monotonic() + max_wait_seconds
    while True:
        async with PENDING_LOCK:
            if key in PENDING_LIVE_TRANSCRIPTS:
                data = PENDING_LIVE_TRANSCRIPTS.pop(key)
                logger.info("[Transcript] Popped pending live transcript key=%s turns=%s", key, len(data))
                return data
        if time.monotonic() >= deadline:
            logger.warning("[Transcript] No pending live transcript for key=%s (timeout)", key)
            return []
        await asyncio.sleep(0.25)


def normalize_live_messages(raw: list[dict[str, Any]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for m in raw:
        role = (m.get("role") or "").strip().lower()
        content = (m.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        out.append({"role": role, "content": content})
    return out


def split_user_stt_into_n_chunks(text: str, n: int) -> list[str]:
    text = text.strip()
    if not text:
        return [""] * n
    if n <= 1:
        return [text]
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(paras) >= n:
        if len(paras) > n:
            paras = paras[: n - 1] + ["\n\n".join(paras[n - 1 :])]
        return paras[:n]
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    if len(sentences) >= n:
        buckets: list[list[str]] = [[] for _ in range(n)]
        for i, s in enumerate(sentences):
            buckets[i % n].append(s)
        parts = [" ".join(b).strip() for b in buckets]
        while len(parts) < n:
            parts.append("")
        return parts[:n]
    # Rough equal split by words
    words = text.split()
    if not words:
        return [text] + [""] * (n - 1)
    chunk_size = max(len(words) // n, 1)
    parts = []
    for i in range(n):
        start = i * chunk_size
        end = (i + 1) * chunk_size if i < n - 1 else len(words)
        segment = " ".join(words[start:end]).strip()
        parts.append(segment)
    return parts[:n]


def merge_live_transcript_with_user_stt(
    live: list[dict[str, str]],
    stt: str,
) -> list[dict[str, str]]:
    """
    Keep assistant lines from the live session; spread mic STT across user turns in order.
    """
    stt = (stt or "").strip()
    if not live:
        return [{"role": "user", "content": stt}] if stt else []

    if not stt:
        return list(live)

    user_count = sum(1 for m in live if m["role"] == "user")
    if user_count == 0:
        return live + [{"role": "user", "content": stt}]

    parts = split_user_stt_into_n_chunks(stt, user_count)
    out: list[dict[str, str]] = []
    ui = 0
    for m in live:
        if m["role"] == "assistant":
            out.append(dict(m))
        else:
            chunk = parts[ui] if ui < len(parts) else m["content"]
            out.append({"role": "user", "content": chunk or m["content"]})
            ui += 1
    return out


def transcribe_user_audio_bytes_sync(audio_bytes: bytes, mime_type: str = "audio/webm") -> str:
    import vertexai
    from vertexai.generative_models import GenerativeModel, Part

    project = os.getenv("PROJECT_ID", "sqy-prod")
    location = os.getenv("LOCATION", "us-central1")
    vertexai.init(project=project, location=location)
    model = GenerativeModel("gemini-2.5-flash")
    part = Part.from_data(audio_bytes, mime_type=mime_type)
    prompt = (
        "Transcribe ONLY the human candidate's speech from this recording. "
        "Preserve the original language(s). Output plain text only — no timestamps, "
        "no speaker labels, no markdown."
    )
    resp = model.generate_content([part, prompt])
    return (getattr(resp, "text", None) or "").strip()


def build_mongo_filter(assessment_result_id: str, video_token: str) -> dict[str, Any] | None:
    if not assessment_result_id and not video_token:
        return None
    ors: list[dict[str, Any]] = []
    if assessment_result_id:
        ors.append({"assessment_result_id": assessment_result_id})
        try:
            from bson import ObjectId  # type: ignore

            if len(assessment_result_id) == 24:
                ors.append({"_id": ObjectId(assessment_result_id)})
        except Exception:
            pass
        ors.append({"_id": assessment_result_id})
    if os.getenv("MONGODB_MATCH_VIDEO_TOKEN", "").lower() in ("1", "true", "yes") and video_token:
        ors.append({"video_token": video_token})
    if not ors:
        return None
    return {"$or": ors}


def mongo_set_transcript_sync(merged_messages: list[dict[str, str]], assessment_result_id: str, video_token: str) -> bool:
    uri = (os.getenv("MONGODB_URI") or "").strip()
    if not uri:
        logger.info("[Transcript] MONGODB_URI not set — skipping DB update")
        return False

    filt = build_mongo_filter(assessment_result_id, video_token)
    if not filt:
        logger.warning("[Transcript] No Mongo filter (missing ids) — skipping DB update")
        return False

    from pymongo import MongoClient

    db_name = os.getenv("MONGODB_DATABASE", "edy")
    coll_name = os.getenv("MONGODB_COLLECTION", "assessment_results")

    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    try:
        coll = client[db_name][coll_name]
        result = coll.update_one(filt, {"$set": {MONGO_PATH_TRANSCRIPT: merged_messages}})
        if result.matched_count == 0:
            logger.warning("[Transcript] Mongo update matched 0 documents filter=%s", filt)
            return False
        logger.info(
            "[Transcript] Mongo updated transcript path=%s modified=%s",
            MONGO_PATH_TRANSCRIPT,
            result.modified_count,
        )
        return True
    finally:
        client.close()


def put_s3_communication_json_sync(
    s3_client: Any,
    bucket: str,
    base_folder: str,
    assessment_result_id: str,
    user_id: str,
    video_token: str,
    stt_text: str,
) -> str:
    ts = time.strftime("%Y%m%d_%H%M%S")
    rid = assessment_result_id or "unknown"
    key = f"{base_folder}/transcripts/{rid}_{ts}_communication.json"
    body = json.dumps({"transcript": [{"text": stt_text}]}, ensure_ascii=False).encode("utf-8")
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
        Metadata={
            "assessment_result_id": assessment_result_id or "",
            "user_id": user_id or "",
            "video_token": video_token or "",
            "kind": "communication_stt",
        },
    )
    logger.info("[Transcript] S3 communication JSON s3://%s/%s", bucket, key)
    return key


def put_s3_combined_transcript_json_sync(
    s3_client: Any,
    bucket: str,
    base_folder: str,
    assessment_result_id: str,
    user_id: str,
    video_token: str,
    merged_messages: list[dict[str, str]],
) -> str:
    """
    Upload combined AI + User transcript (well-formatted) as JSON to S3.
    Format: { "transcript": [ {"role": "assistant", "content": "..."}, {"role": "user", "content": "..."}, ... ] }
    """
    ts = time.strftime("%Y%m%d_%H%M%S")
    rid = assessment_result_id or "unknown"
    key = f"{base_folder}/transcripts/{rid}_{ts}_combined_transcript.json"
    body = json.dumps(
        {
            "assessment_result_id": assessment_result_id,
            "user_id": user_id,
            "video_token": video_token,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "total_turns": len(merged_messages),
            "transcript": merged_messages,
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
        Metadata={
            "assessment_result_id": assessment_result_id or "",
            "user_id": user_id or "",
            "video_token": video_token or "",
            "kind": "combined_transcript",
        },
    )
    logger.info("[Transcript] S3 combined transcript JSON s3://%s/%s (%d turns)", bucket, key, len(merged_messages))
    return key


async def run_user_audio_post_process(
    audio_bytes: bytes,
    *,
    s3_client: Any,
    s3_bucket: str,
    base_folder: str,
    assessment_id: str,
    assessment_result_id: str,
    user_id: str,
    video_token: str,
    audio_s3_key: str,
    content_type: str,
) -> None:
    try:
        _render(
            f"STT_PIPELINE_START bytes={len(audio_bytes)} audio_s3_key={audio_s3_key!r} "
            f"assessment_result_id={assessment_result_id!r}"
        )
        live = await pop_pending_live_transcript(assessment_result_id, video_token)
        mime = "audio/webm"
        if "mpeg" in (content_type or "").lower() or (audio_bytes[:3] == b"ID3"):
            mime = "audio/mpeg"
        elif "wav" in (content_type or "").lower():
            mime = "audio/wav"

        stt_text = await asyncio.to_thread(transcribe_user_audio_bytes_sync, audio_bytes, mime)
        if not stt_text:
            logger.warning("[Transcript] STT returned empty (audio_s3_key=%s)", audio_s3_key)
        _render(f"STT_DONE chars={len(stt_text)} live_turns={len(live)}")

        merged = merge_live_transcript_with_user_stt(live, stt_text)

        # Upload user-only communication JSON (backward compat)
        comm_key = await asyncio.to_thread(
            put_s3_communication_json_sync,
            s3_client,
            s3_bucket,
            base_folder,
            assessment_result_id,
            user_id,
            video_token,
            stt_text,
        )
        _render(f"TRANSCRIPT_JSON_S3 key={comm_key}")

        # Upload combined AI + User transcript (well-formatted)
        combined_key = await asyncio.to_thread(
            put_s3_combined_transcript_json_sync,
            s3_client,
            s3_bucket,
            base_folder,
            assessment_result_id,
            user_id,
            video_token,
            merged,
        )
        _render(f"COMBINED_TRANSCRIPT_S3 key={combined_key} turns={len(merged)}")

        ok = await asyncio.to_thread(
            mongo_set_transcript_sync,
            merged,
            assessment_result_id,
            video_token,
        )
        _render(f"STT_PIPELINE_END mongo_updated={ok} merged_turns={len(merged)}")
    except Exception:
        logger.exception("[Transcript] Post-process failed audio_s3_key=%s", audio_s3_key)
        _render(f"STT_PIPELINE_FAIL audio_s3_key={audio_s3_key!r}")
