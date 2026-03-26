"""
Proctoring — embedded on assessment_results.proctoring.
API path session_id = video_token (same as interview URL). Resolves row via VIDEO_TOKEN_FIELD on document.
Fallback: legacy lookup by assessment_results._id.
"""
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from passlib.context import CryptContext
from pydantic import BaseModel, Field
import jwt
import httpx
import logging

_root = Path(__file__).resolve().parent
load_dotenv(_root / ".env")

logger = logging.getLogger(__name__)

# DocumentDB / Mongo: MONGO_URI or MONGODB_URI in proctoring/backend/.env
MONGODB_URI = (os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "").strip()
MONGODB_DB_NAME = (os.getenv("MONGODB_DB_NAME") or os.getenv("APP_ENV") or "Dev").strip()
# Same Lambda URL as AI interview GET /api/config — when set, POST /v1/sessions resolves row via Lambda (user_id + video_token) then Mongo by assessment_result_id
LAMBDA_SESSION_URL = os.getenv("LAMBDA_SESSION_URL", "").strip()
PROCTORING_USE_LAMBDA_SESSION_LOOKUP = os.getenv(
    "PROCTORING_USE_LAMBDA_SESSION_LOOKUP", "true"
).lower() in ("1", "true", "yes")
ASSESSMENT_RESULTS_COLL = os.getenv("MONGODB_COLLECTION_ASSESSMENT_RESULTS", "assessment_results")
# Field on assessment_results that stores the unique token from interview URL (same as Lambda/config)
VIDEO_TOKEN_FIELD = os.getenv("VIDEO_TOKEN_FIELD", "video_token")
DASHBOARD_USERS_COLL = os.getenv("MONGODB_COLLECTION_DASHBOARD_USERS", "proctor_dashboard_users")
# Set false to block new dashboard signups (login still works)
ALLOW_DASHBOARD_REGISTER = os.getenv("ALLOW_DASHBOARD_REGISTER", "true").lower() in (
    "1",
    "true",
    "yes",
)

API_KEY = os.getenv("PROCTORING_API_KEY", "dev-proctoring-key")
JWT_SECRET = os.getenv("JWT_SECRET", "dev-jwt-secret-change-in-production-min-32")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 24 * 7
CORS = os.getenv("CORS_ORIGINS", "*")
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

mongo_client: AsyncIOMotorClient | None = None
db = None

WEIGHTS = {
    "noise_detected": 2,
    "went_offline": 5,
    "tab_switched": 2,
    "no_face_detected": 4,
    "multiple_faces": 10,
    "exited_fullscreen": 3,
    "multiple_monitors": 5,
    "window_blur": 1,
}


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def id_filter(assessment_result_id: str) -> dict:
    """Match assessment_results._id — ObjectId if valid 24-hex, else string."""
    s = (assessment_result_id or "").strip()
    if not s:
        raise HTTPException(status_code=400, detail="assessment_result_id required")
    if len(s) == 24:
        try:
            return {"_id": ObjectId(s)}
        except InvalidId:
            pass
    return {"_id": s}


async def find_assessment_result_by_session_key(session_id: str):
    """
    session_id from URL = video_token (interview flow).
    Falls back to _id lookup for older clients.
    """
    s = (session_id or "").strip()
    if not s:
        raise HTTPException(status_code=400, detail="session_id required")
    doc = await db[ASSESSMENT_RESULTS_COLL].find_one({VIDEO_TOKEN_FIELD: s})
    if doc:
        return doc
    return await db[ASSESSMENT_RESULTS_COLL].find_one(id_filter(s))


async def find_assessment_result_for_report(
    environment: str,
    lookup: str | None,
    video_token: str | None,
    assessment_result_id: str | None,
):
    """Resolve row: prefer explicit params; else `lookup` tries video_token field first, then _id."""
    env = environment.lower().strip()
    doc = None
    if video_token and video_token.strip():
        doc = await db[ASSESSMENT_RESULTS_COLL].find_one({VIDEO_TOKEN_FIELD: video_token.strip()})
    elif assessment_result_id and assessment_result_id.strip():
        doc = await db[ASSESSMENT_RESULTS_COLL].find_one(id_filter(assessment_result_id.strip()))
    elif lookup and lookup.strip():
        q = lookup.strip()
        doc = await db[ASSESSMENT_RESULTS_COLL].find_one({VIDEO_TOKEN_FIELD: q})
        if not doc:
            doc = await db[ASSESSMENT_RESULTS_COLL].find_one(id_filter(q))
    else:
        raise HTTPException(
            status_code=400,
            detail="Provide lookup, video_token, or assessment_result_id",
        )
    if not doc:
        raise HTTPException(404, "assessment_result not found")
    p = doc.get("proctoring") or {}
    if p.get("environment") != env:
        raise HTTPException(404, "No proctoring data for this id and environment")
    return doc


def counts_from_events(events: list) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in events or []:
        t = e.get("event_type", "")
        counts[t] = counts.get(t, 0) + 1
    return counts


def compute_trust_score(counts: dict) -> int:
    score = 100
    for k, n in counts.items():
        w = WEIGHTS.get(k, 1)
        score -= w * min(n, 50)
    return max(0, min(100, score))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global mongo_client, db
    if not MONGODB_URI:
        raise RuntimeError("MONGO_URI or MONGODB_URI missing — set in proctoring/backend/.env")
    mongo_client = AsyncIOMotorClient(MONGODB_URI)
    db = mongo_client[MONGODB_DB_NAME]
    await db[ASSESSMENT_RESULTS_COLL].create_index(VIDEO_TOKEN_FIELD)
    await db[ASSESSMENT_RESULTS_COLL].create_index([("proctoring.environment", 1)])
    await db[DASHBOARD_USERS_COLL].create_index("email", unique=True)
    yield
    if mongo_client:
        mongo_client.close()


app = FastAPI(title="Proctoring API", version="2.1.0", lifespan=lifespan)
_origins = [o.strip() for o in CORS.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins if _origins != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def verify_api_key(x_api_key: str | None = Header(None, alias="X-API-Key")):
    if not x_api_key or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")
    return True


def decode_jwt(token: str) -> dict | None:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except Exception:
        return None


def verify_bearer_or_api_key(
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> str | None:
    if x_api_key and x_api_key == API_KEY:
        return None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
        payload = decode_jwt(token)
        if payload and payload.get("sub"):
            return payload["sub"]
    raise HTTPException(status_code=401, detail="Invalid or missing auth (Bearer token or X-API-Key)")


class SessionCreate(BaseModel):
    """video_token required — matches assessment_results row (same as interview URL)."""
    environment: str = Field(..., description="dev | stage | prod")
    video_token: str = Field(..., description="Unique token from interview URL; resolves the result row")
    assessment_id: str | None = None
    assessment_result_id: str | None = Field(None, description="Optional; must match _id if provided")
    user_id: str | None = None
    client_info: dict | None = None


class EventIn(BaseModel):
    event_type: str
    occurred_at: str | None = None
    metadata: dict | None = None
    evidence_url: str | None = None


class EventsBatch(BaseModel):
    events: list[EventIn]


def build_session_api(doc: dict) -> dict:
    p = doc.get("proctoring") or {}
    rid = doc.get("_id")
    vt = p.get("video_token") or doc.get(VIDEO_TOKEN_FIELD)
    return {
        "id": str(rid),
        "environment": p.get("environment"),
        "assessment_id": p.get("assessment_id"),
        "assessment_result_id": str(rid),
        "video_token": vt,
        "user_id": p.get("user_id"),
        "started_at": p.get("started_at"),
        "ended_at": p.get("ended_at"),
        "client_info": p.get("client_info") or {},
    }


async def lambda_fetch_session(user_id: str, video_token: str) -> dict:
    """Same HTTP contract as AI interview GET /api/config → Lambda."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            LAMBDA_SESSION_URL,
            params={"user_id": user_id, "video_token": video_token},
        )
    if r.status_code == 404:
        raise HTTPException(
            status_code=404,
            detail="Lambda: session not found (same lookup as interview /api/config)",
        )
    if r.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Lambda HTTP {r.status_code}: {r.text[:400]}",
        )
    try:
        return r.json()
    except Exception as e:
        logger.exception("Lambda invalid JSON")
        raise HTTPException(status_code=502, detail="Lambda returned invalid JSON") from e


@app.post("/v1/sessions")
async def create_session(body: SessionCreate, _: bool = Depends(verify_api_key)):
    vt = body.video_token.strip()
    if not vt:
        raise HTTPException(400, "video_token required")

    lambda_data: dict | None = None
    use_lambda = bool(LAMBDA_SESSION_URL) and PROCTORING_USE_LAMBDA_SESSION_LOOKUP
    if use_lambda:
        uid = (body.user_id or "").strip()
        if not uid:
            raise HTTPException(
                400,
                "user_id required when Lambda session lookup is enabled (same as AI interview)",
            )
        lambda_data = await lambda_fetch_session(uid, vt)
        ar_id = lambda_data.get("assessment_result_id")
        if not ar_id:
            raise HTTPException(502, detail="Lambda response missing assessment_result_id")
        existing = await db[ASSESSMENT_RESULTS_COLL].find_one(id_filter(str(ar_id)))
        if not existing:
            raise HTTPException(
                404,
                detail="No assessment_results row for Lambda assessment_result_id",
            )
    else:
        existing = await db[ASSESSMENT_RESULTS_COLL].find_one({VIDEO_TOKEN_FIELD: vt})
        if not existing:
            raise HTTPException(
                404,
                detail=f"No assessment_results row with {VIDEO_TOKEN_FIELD}={vt!r} — token must match existing result",
            )

    if body.assessment_result_id and str(existing["_id"]).strip() != body.assessment_result_id.strip():
        raise HTTPException(400, "assessment_result_id does not match resolved session")

    resolved_assessment_id = (
        body.assessment_id
        or (lambda_data or {}).get("assessment_id")
        or ""
    )
    env = body.environment.lower().strip()
    await db[ASSESSMENT_RESULTS_COLL].update_one(
        {"_id": existing["_id"]},
        {
            "$set": {
                VIDEO_TOKEN_FIELD: vt,
                "proctoring.environment": env,
                "proctoring.assessment_id": resolved_assessment_id,
                "proctoring.user_id": body.user_id or "",
                "proctoring.video_token": vt,
                "proctoring.started_at": utc_now_iso(),
                "proctoring.ended_at": None,
                "proctoring.events": [],
                "proctoring.counts": {},
                "proctoring.trust_score": None,
                "proctoring.client_info": body.client_info or {},
            }
        },
    )
    # Client uses this for /events and /end — must be video_token
    return {"session_id": vt, "status": "created"}


@app.post("/v1/sessions/{session_id}/events")
async def add_events(session_id: str, body: EventsBatch, _: bool = Depends(verify_api_key)):
    doc = await find_assessment_result_by_session_key(session_id)
    fid = {"_id": doc["_id"]}
    to_push = []
    for ev in body.events:
        ts = ev.occurred_at or utc_now_iso()
        to_push.append(
            {
                "event_type": ev.event_type,
                "occurred_at": ts,
                "metadata": ev.metadata or {},
                "evidence_url": ev.evidence_url or "",
            }
        )
    if to_push:
        await db[ASSESSMENT_RESULTS_COLL].update_one(
            fid, {"$push": {"proctoring.events": {"$each": to_push}}}
        )
    return {"status": "ok", "count": len(body.events)}


@app.patch("/v1/sessions/{session_id}/end")
async def end_session(session_id: str, _: bool = Depends(verify_api_key)):
    doc = await find_assessment_result_by_session_key(session_id)
    if not doc.get("proctoring"):
        raise HTTPException(404, "proctoring not initialized")
    events = doc["proctoring"].get("events") or []
    counts = counts_from_events(events)
    trust = compute_trust_score(counts)
    await db[ASSESSMENT_RESULTS_COLL].update_one(
        {"_id": doc["_id"]},
        {
            "$set": {
                "proctoring.ended_at": utc_now_iso(),
                "proctoring.counts": counts,
                "proctoring.trust_score": trust,
            }
        },
    )
    return {"status": "ended", "trust_score": trust}


class LoginIn(BaseModel):
    email: str
    password: str


class RegisterIn(BaseModel):
    email: str
    password: str
    name: str | None = None


@app.post("/auth/login")
async def login(body: LoginIn):
    row = await db[DASHBOARD_USERS_COLL].find_one({"email": body.email.strip().lower()})
    if not row:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not pwd_ctx.verify(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    uid = row["_id"]
    expire = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS)
    payload = {"sub": uid, "exp": int(expire.timestamp())}
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return {"access_token": token, "token_type": "bearer"}


@app.post("/auth/register")
async def register(body: RegisterIn):
    if not ALLOW_DASHBOARD_REGISTER:
        raise HTTPException(status_code=403, detail="Registration disabled (set ALLOW_DASHBOARD_REGISTER=true in server env)")
    email = body.email.strip().lower()
    if not email or not body.password:
        raise HTTPException(status_code=400, detail="Email and password required")
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    existing = await db[DASHBOARD_USERS_COLL].find_one({"email": email})
    if existing:
        raise HTTPException(status_code=409, detail="This email is already registered — use Login")
    uid = str(uuid.uuid4())
    password_hash = pwd_ctx.hash(body.password)
    await db[DASHBOARD_USERS_COLL].insert_one(
        {
            "_id": uid,
            "email": email,
            "password_hash": password_hash,
            "name": (body.name or "").strip() or None,
            "created_at": utc_now_iso(),
        }
    )
    return {"status": "created", "email": email}


@app.get("/v1/report")
async def get_report(
    environment: str,
    _auth: str | None = Depends(verify_bearer_or_api_key),
    lookup: str | None = Query(
        None,
        description="Video token or Mongo _id — tries video_token field first, then _id",
    ),
    video_token: str | None = Query(None, description="Explicit video_token (interview URL)"),
    assessment_result_id: str | None = Query(None, description="Explicit assessment_results._id"),
):
    doc = await find_assessment_result_for_report(
        environment, lookup, video_token, assessment_result_id
    )
    p = doc.get("proctoring") or {}
    events = list(p.get("events") or [])
    counts = p.get("counts") or counts_from_events(events)
    trust = p.get("trust_score")
    if trust is None:
        trust = compute_trust_score(counts)
    for e in events:
        if "metadata" not in e or e["metadata"] is None:
            e["metadata"] = {}

    sess = build_session_api(doc)

    return {
        "session": sess,
        "events": events,
        "counts": counts,
        "trust_score": trust,
        "weights_reference": WEIGHTS,
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "database": MONGODB_DB_NAME,
        "video_token_field": VIDEO_TOKEN_FIELD,
        "assessment_results_collection": ASSESSMENT_RESULTS_COLL,
        "dashboard_users_collection": DASHBOARD_USERS_COLL,
        "dashboard_register_open": ALLOW_DASHBOARD_REGISTER,
        "lambda_session_lookup": bool(LAMBDA_SESSION_URL) and PROCTORING_USE_LAMBDA_SESSION_LOOKUP,
        "lambda_session_url_configured": bool(LAMBDA_SESSION_URL),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8010")), reload=True)
