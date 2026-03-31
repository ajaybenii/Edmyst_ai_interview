from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from dotenv import load_dotenv
import asyncio
import json
import os
import logging
from websockets.legacy.client import connect
from datetime import datetime, timedelta
import time
from collections import deque
import uuid
import io
from pathlib import Path
import vertexai
from vertexai.generative_models import GenerativeModel, Part
from pydantic import BaseModel
import httpx

from transcript_service import (
    normalize_live_messages,
    run_user_audio_post_process,
    store_pending_live_transcript,
)

load_dotenv(override=True)

BASE_DIR = Path(__file__).parent

# Environment
APP_ENV = os.getenv("APP_ENV", "Dev")  # Dev | Stage | Prod

# Lambda Function URL for DocumentDB session lookup (bypasses VPC restrictions)
LAMBDA_SESSION_URL = os.getenv(
    "LAMBDA_SESSION_URL",
    "https://7g7pnrwtqchpsqiz3dnuadh4cu0tdact.lambda-url.us-east-1.on.aws/"
)
print(f"[STARTUP] APP_ENV={APP_ENV}")
print(f"[STARTUP] LAMBDA_SESSION_URL={LAMBDA_SESSION_URL}")

# Tavus-style screen recordings (Step Function lists tavus/{conversation_id}/)
TAVUS_RECORDINGS_BUCKET = (
    os.getenv("TAVUS_RECORDINGS_BUCKET") or os.getenv("EDY_TAVUS_BUCKET") or "edy-tavus"
).strip()
# Optional GET URL (edy-apis etc.) — same query params as Lambda: user_id, video_token, assessment_result_id
CONVERSATION_RESOLVE_URL = (os.getenv("CONVERSATION_RESOLVE_URL") or "").strip()
# Dev / broken Lambda only: accept multipart conversation_id for tavus/ screen key when server cannot resolve from Lambda
ALLOW_CLIENT_CONVERSATION_ID_FOR_SCREEN_UPLOAD = os.getenv(
    "ALLOW_CLIENT_CONVERSATION_ID_FOR_SCREEN_UPLOAD", ""
).strip().lower() in ("1", "true", "yes")
print(f"[STARTUP] TAVUS_RECORDINGS_BUCKET={TAVUS_RECORDINGS_BUCKET}")
if ALLOW_CLIENT_CONVERSATION_ID_FOR_SCREEN_UPLOAD:
    print("[STARTUP] ALLOW_CLIENT_CONVERSATION_ID_FOR_SCREEN_UPLOAD=1 (screen S3 key may use form conversation_id if Lambda unresolved)")

# Edy candidate API (POST /update/assessment_result) — used by /api/edy/assessment-complete proxy for Netlify player CORS
EDY_CANDIDATE_API_BASE = (os.getenv("EDY_CANDIDATE_API_BASE") or "").strip()

# Configure Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Backend Logger (app.log)
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "app.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def render_log(msg: str) -> None:
    """Stdout + flush — Render dashboard par clearly dikhe (logger ke alawa)."""
    print(f"[RENDER] {msg}", flush=True)


logger.info(f"[Config] Lambda session lookup URL: {LAMBDA_SESSION_URL}")
render_log(f"startup Lambda URL configured | APP_ENV={APP_ENV} | Tavus bucket={TAVUS_RECORDINGS_BUCKET} | S3 bucket={os.getenv('AWS_S3_BUCKET', 'edy-temp-videos')}")

# Frontend Logger (frontend.log)
frontend_logger = logging.getLogger("frontend")
frontend_logger.setLevel(logging.INFO)
frontend_handler = logging.FileHandler(LOG_DIR / "frontend.log")
frontend_handler.setFormatter(logging.Formatter("%(asctime)s [FRONTEND] %(message)s"))
frontend_logger.addHandler(frontend_handler)

# Import for service account authentication
from google.oauth2 import service_account
from google.auth.transport.requests import Request

# Configuration (from environment variables)
PROJECT_ID = os.getenv("PROJECT_ID", "sqy-prod")
LOCATION = os.getenv("LOCATION", "us-central1")
MODEL_ID = "gemini-live-2.5-flash-native-audio"
MODEL = f"projects/{PROJECT_ID}/locations/{LOCATION}/publishers/google/models/{MODEL_ID}"
HOST = f"wss://{LOCATION}-aiplatform.googleapis.com/ws/google.cloud.aiplatform.v1.LlmBidiService/BidiGenerateContent"

# 🔥 PRODUCTION SETTINGS
MAX_CONCURRENT_CONNECTIONS = int(os.getenv("MAX_CONCURRENT_CONNECTIONS", "1000"))
CONNECTION_TIMEOUT = int(os.getenv("CONNECTION_TIMEOUT", "1800"))  # 30 minutes

INTERVIEW_PROMPT = """
You are conducting a real-time technical interview for {role_title} position at {organization}.
You are based in INDIA and conducting this interview in Indian Standard Time (IST, UTC+5:30).

You can hear and also see the candidate through audio and video.

# 🔴 PROCTORING & MONITORING (HIGH PRIORITY):
You MUST continuously monitor the video feed and IMMEDIATELY warn the candidate if you detect:

1. **Multiple People Detected**: If you see more than ONE person in the frame:
   - Immediately say: "I notice there might be someone else in the room. For interview integrity, please ensure you are alone. This will be noted."

2. **Mobile Phone Usage**: If you see the candidate using or looking at a mobile phone:
   - Immediately say: "I noticed you looking at your phone. Please keep your phone away during the interview. Using external devices is not allowed."

3. **Candidate Not Visible**: If the candidate is not visible or has moved out of frame:
   - Immediately say: "I can't see you on the screen. Please adjust your camera so I can see you clearly."

4. **Looking Away / Not Focused**: If the candidate is frequently looking away from the screen (looking left, right, up, or down repeatedly):
   - Say: "I notice you're looking away from the screen. Please focus on the interview and maintain eye contact with the camera."

5. **Suspicious Behavior**: If you see any suspicious behavior like reading from another screen, someone whispering, or unusual movements:
   - Say: "I noticed some unusual activity. Please remember this is a proctored interview and any unfair means will be recorded."

6. **Tab Switching / Distraction**: If the candidate appears distracted or seems to be reading something off-screen:
   - Say: "It seems like you might be looking at something else. Please give your full attention to the interview."

# 🌐 NETWORK MONITORING:
If you receive a message indicating the candidate's network quality is POOR:
- Say: "I'm noticing some connectivity issues on your end. If possible, please move to a location with better internet connection for a smoother interview experience."


- If you receive "[SYSTEM] Interview time limit (30 minutes) reached. Ending interview.":
  Say briefly and professionally that the 30-minute window is over, thank the candidate, and confirm the session will be submitted — then stop asking new interview questions.

- If you receive "[SYSTEM] There are 5 minutes remaining in this interview.":
  Acknowledge naturally in one short sentence (e.g. that about five minutes are left) and continue the interview calmly — do not rush or sound alarming.

- If you receive a "[SYSTEM] Welfare check:" message about the candidate being silent for ~5 minutes:
  Follow that message exactly: one brief, warm sentence in the candidate's interview language — they may have been thinking or giving a long explanation; check if they can hear you, need more time, or have an audio issue — natural tone, not robotic.

# 🔄 IMPORTANT - CONTINUING INTERVIEW:
If the candidate speaks or responds after any warning (including the final warning), you MUST:
- IMMEDIATELY continue the interview as normal
- Do NOT say "the interview has ended" or "we've reached the time limit" (unless the 30-minute timer actually reached zero)
- Do NOT refuse to continue - just pick up where you left off
- Simply acknowledge their response and continue with the next question
The silence warnings are just prompts - if the user responds, the interview continues!

# 🚫 SILENCE & TURN-TAKING (NATURAL CONVERSATION):
- Short pauses (even ~3–5 seconds) are normal while the candidate thinks. Do NOT interrupt during brief silence.
- If the candidate is silent, WAIT for them to speak — do not fill silence with "are you there?" or filler.
- EXCEPTION: If you receive an explicit "[SYSTEM]" instruction (time remaining, welfare check, or time limit), follow that instruction — those override generic silence rules for that turn only.
- NEVER say "I am waiting for your response".
- NEVER output "[SYSTEM]" tags or internal instruction text in your own speech.
- If the candidate gives a short response like "okay" or "yes", acknowledge it and continue.
- If you get some unrelated input except english and hindi then reconfirm the question in english or hindi.
- If an answer sounds incomplete or cut off, ask ONE natural follow-up before moving on — e.g. "Would you like to add anything to that, or shall we move to the next question?" — warm and human, not robotic.

# ⚠️ IMPORTANT: Issue warnings in a FIRM but PROFESSIONAL tone. Do not be rude, but be clear that violations are being noted.

# Interview Structure (opening — do NOT skip):
1. **Organization & role overview (mandatory first):** Before any interview or technical questions, speak in a casual, human way for a short segment (about 2–5 sentences). Give context on **{organization}** (what kind of company/team it is, in plain language) and the **{role_title}** opportunity — warm and conversational, not like reading a brochure or legal text. Then smoothly transition to the greeting.

2. **Time-based greeting** using [CURRENT CONTEXT] IST:
   - 6 AM - 12 PM: "Good morning"
   - 12 PM - 5 PM: "Good afternoon"
   - 5 PM - 9 PM: "Good evening"
   - 9 PM - 6 AM: "Hello"

3. **Introduction:** Invite the candidate to introduce themselves briefly. Only after that, move into the substantive interview flow below.

{interview_structure}

# Interview Closing:
- After all skills are evaluated, say:
  "Thank you! Your interview is now complete. If you have any questions for me, feel free to ask. Otherwise, you can click the Submit Interview button to end the session."
- If the candidate has questions, answer them naturally, then repeat the closing.

# Visual Observation Rules:
- You can see the candidate through video
- Answer visual questions ONLY based on what is clearly visible
- If something is not clearly visible, say you are not certain
- Do not guess or assume

# Communication Rules:
- Be professional but friendly (Indian professional context)
- Listen carefully and ask follow-up questions
- Keep responses concise but human — like a real interviewer, not a script
- Encourage the candidate when they do well
- Use natural, conversational language with appropriate pacing; allow thinking time
- Speak clearly in English (Indian candidates may have regional accents - be patient)
- If user want to switch language then proceed with that language for the rest of the interview when practical
- You are an English-speaking interviewer by default. Always interpret input as English unless explicitly told otherwise.
"""

# Default interview structure (adaptive — used when no assessment data is available)
DEFAULT_INTERVIEW_STRUCTURE = """
4. After their introduction, dig deeper into their background and current role.
   - Listen carefully to extract:
     a) What profile/domain they work in (e.g. backend, data science, frontend, DevOps)
     b) How many years of experience they have
     c) What technologies or tools they primarily use

5. Based ONLY on what the candidate tells you in their introduction, adapt the interview:
   - Identify the most relevant technical skills from their background
   - Ask 3-4 technical questions specific to THEIR profile and experience level
   - If 0-2 years experience: ask foundational/conceptual questions
   - If 3-5 years experience: ask intermediate + scenario-based questions
   - If 6+ years experience: ask architecture, leadership, and deep technical questions
   - Ask 1-2 follow-up questions based on their answers

6. Ask 1-2 behavioral/soft-skill questions relevant to their role

7. Close the interview professionally
"""


async def fetch_lambda_session_response(
    user_id: str, video_token: str,
) -> tuple[dict | None, int | None, str]:
    """Lambda session lookup. Returns (json_or_none, http_status_or_none, error_body_snippet)."""
    if not (user_id or "").strip() or not (video_token or "").strip():
        return None, None, ""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                LAMBDA_SESSION_URL,
                params={"user_id": user_id.strip(), "video_token": video_token.strip()},
            )
            if resp.status_code == 200:
                return resp.json(), 200, ""
            logger.warning(
                "Lambda session lookup non-200: status=%s user_id=%s",
                resp.status_code,
                user_id,
            )
            return None, resp.status_code, (resp.text or "")[:200]
    except Exception as e:
        logger.warning("fetch_lambda_session_response error: %s", e)
        return None, None, str(e)[:200]


async def fetch_lambda_session_json(user_id: str, video_token: str) -> dict | None:
    """Session document from Lambda (same as /api/config lookup)."""
    data, code, _ = await fetch_lambda_session_response(user_id, video_token)
    return data if code == 200 else None


def _plausible_conversation_id(cid: str) -> bool:
    s = (cid or "").strip()
    if len(s) < 8 or len(s) > 200:
        return False
    return all(c.isalnum() or c in "-_" for c in s)


def extract_conversation_id_from_payload(data: dict | None) -> str | None:
    if not data or not isinstance(data, dict):
        return None
    cid = data.get("conversation_id")
    if cid is not None and str(cid).strip():
        return str(cid).strip()
    for nested_key in ("assessment_result", "assessment_result_data", "data"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            cid = nested.get("conversation_id")
            if cid is not None and str(cid).strip():
                return str(cid).strip()
    return None


def _session_assessment_result_id_matches(payload: dict, submitted_assessment_result_id: str) -> bool:
    """If client sent assessment_result_id, it must match Lambda row (do not trust spoofed id)."""
    if not (submitted_assessment_result_id or "").strip():
        return True
    lam_rid = str(payload.get("assessment_result_id") or "").strip()
    if not lam_rid:
        return True
    return lam_rid == (submitted_assessment_result_id or "").strip()


async def resolve_conversation_id_for_screen_upload(
    user_id: str,
    video_token: str,
    assessment_result_id: str = "",
) -> str | None:
    """
    Server-side only. Never use client-supplied conversation_id for S3 keys.
    Order: Lambda session JSON → optional CONVERSATION_RESOLVE_URL.
    """
    payload = await fetch_lambda_session_json(user_id, video_token)
    cid: str | None = None
    if payload:
        if _session_assessment_result_id_matches(payload, assessment_result_id):
            cid = extract_conversation_id_from_payload(payload)
        else:
            logger.error(
                "[Tavus] assessment_result_id mismatch vs Lambda — not using Lambda conversation_id "
                "(submitted=%s lambda=%s)",
                assessment_result_id,
                payload.get("assessment_result_id"),
            )
    if cid:
        return cid

    if CONVERSATION_RESOLVE_URL:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    CONVERSATION_RESOLVE_URL,
                    params={
                        "user_id": (user_id or "").strip(),
                        "video_token": (video_token or "").strip(),
                        "assessment_result_id": (assessment_result_id or "").strip(),
                    },
                )
                if resp.status_code == 200:
                    j = resp.json()
                    if isinstance(j, dict):
                        if _session_assessment_result_id_matches(j, assessment_result_id):
                            cid2 = extract_conversation_id_from_payload(j)
                            if cid2:
                                return cid2
        except Exception as e:
            logger.warning("[Tavus] CONVERSATION_RESOLVE_URL failed: %s", e)

    return None


async def fetch_assessment_data(assessment_id: str) -> dict | None:
    """Fetch assessment details from Lambda for dynamic prompt building."""
    if not assessment_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                LAMBDA_SESSION_URL,
                params={"assessment_id": assessment_id}
            )
            if resp.status_code == 200:
                data = resp.json()
                logger.info(f"[Prompt] Assessment data fetched: {data.get('title', '?')} at {data.get('organization', '?')}")
                return data
            else:
                logger.warning(f"[Prompt] Lambda returned {resp.status_code} for assessment_id={assessment_id}")
                render_log(f"PROMPT_LAMBDA_FAIL status={resp.status_code} assessment_id={assessment_id}")
                return None
    except Exception as e:
        logger.error(f"[Prompt] Failed to fetch assessment data: {e}")
        return None


def build_interview_prompt(assessment_data: dict | None) -> str:
    """Build a complete interview prompt, injecting dynamic assessment data into the template."""
    if not assessment_data:
        # Fallback: generic prompt
        return INTERVIEW_PROMPT.format(
            role_title="a Software Engineer",
            organization="the company",
            interview_structure=DEFAULT_INTERVIEW_STRUCTURE
        )

    title = assessment_data.get("title", "Software Engineer")
    org = assessment_data.get("organization", "the company")
    jd = assessment_data.get("job_description", "")
    industry = assessment_data.get("industry", "")
    min_exp = assessment_data.get("min_experience", 0)
    max_exp = assessment_data.get("max_experience", 0)
    role_type = assessment_data.get("role_type", "")
    required_time = assessment_data.get("required_time", 30)
    multilingual = assessment_data.get("multilingual", ["English"])
    skills_weightage = assessment_data.get("skills_weightage", {})
    tech_skills = assessment_data.get("technical_skills", [])
    additional = assessment_data.get("additional_instruction", [])

    # Build skills list
    skills_text = ""
    for i, skill in enumerate(tech_skills, 1):
        skills_text += f"   {i}. {skill['name']} ({skill.get('proficiency', 'Medium')}) — {skill.get('description', '')}\n"

    # Build weightage info
    tech_weight = skills_weightage.get("technical_skill", 70)
    comm_weight = skills_weightage.get("conversation", 30)

    # Build dynamic interview structure
    interview_structure = f"""
# AFTER OPENING (overview + greeting + their intro): follow job-specific flow below for questions.

# JOB DESCRIPTION:
{jd}

# CANDIDATE PROFILE:
- Expected Experience: {min_exp}-{max_exp} years
- Role Type: {role_type}
- Industry: {industry}
- Language: {', '.join(multilingual)}
- Interview Duration: {required_time} minutes

# SKILL EVALUATION:
You must allocate your questions based on these weightages:
- Technical Skills: {tech_weight}% of the interview
- Communication & Soft Skills: {comm_weight}% of the interview

Technical skills to evaluate:
{skills_text}
# QUESTION GENERATION RULES:
- Generate UNIQUE questions for each interview (never use the same questions)
- Automatically calculate how many questions per skill based on weightage and interview time
- Start with easier questions and gradually increase difficulty
- Ask follow-up questions based on candidate's responses for deeper evaluation
- If a candidate struggles, move to the next skill gracefully
- Cover ALL listed technical skills before closing the interview
"""

    # Add any additional instructions from the client
    if additional and len(additional) > 0:
        interview_structure += "\n# ADDITIONAL CLIENT INSTRUCTIONS:\n"
        for instr in additional:
            interview_structure += f"- {instr}\n"

    return INTERVIEW_PROMPT.format(
        role_title=f"a {title}",
        organization=org,
        interview_structure=interview_structure
    )

# Service Account Authentication
def get_access_token():
    """Get access token using service account credentials"""
    try:
        # Load credentials from environment or file
        credentials_path = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
        
        if credentials_path and os.path.exists(credentials_path):
            credentials = service_account.Credentials.from_service_account_file(
                credentials_path,
                scopes=['https://www.googleapis.com/auth/cloud-platform']
            )
        else:
            # Load from JSON string in environment variable
            credentials_json = os.getenv('GOOGLE_CREDENTIALS_JSON')
            if credentials_json:
                credentials_info = json.loads(credentials_json)
                credentials = service_account.Credentials.from_service_account_info(
                    credentials_info,
                    scopes=['https://www.googleapis.com/auth/cloud-platform']
                )
            else:
                logger.error("No credentials found in environment")
                return None
        
        # Refresh token
        credentials.refresh(Request())
        return credentials.token
    except Exception as e:
        logger.error(f"Error getting access token: {e}")
        return None

# Lifespan context manager (replaces deprecated @app.on_event)
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("=" * 60)
    logger.info("VOICE + VIDEO INTERVIEW BOT API")
    logger.info("=" * 60)
    logger.info(f"Model: {MODEL_ID}")
    logger.info(f"Max Connections: {MAX_CONCURRENT_CONNECTIONS}")
    logger.info(f"Connection Timeout: {CONNECTION_TIMEOUT}s")
    logger.info(f"Log Level: {LOG_LEVEL}")
    logger.info(f"Video Support: ENABLED")
    logger.info("=" * 60)
    try:
        vertexai.init(project=PROJECT_ID, location=LOCATION)
        logger.info("Vertex AI SDK initialized for STT / scoring models")
    except Exception as e:
        logger.warning("Vertex AI SDK init skipped or failed: %s", e)
    logger.info("Server Ready!")
    render_log("=== SERVER READY (interview API) ===")
    yield
    # Shutdown
    logger.info("Server shutting down...")
    render_log("=== SERVER SHUTDOWN ===")

# Initialize FastAPI
app = FastAPI(
    title="Voice + Video Interview Bot API - Production",
    description="High-performance interview bot with unlimited rate limits",
    version="2.0.0",
    lifespan=lifespan
)

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Update with your frontend domain in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



# Connection Management
class ConnectionManager:
    def __init__(self):
        self.active_connections = 0
        self.total_connections = 0
        self.connection_history = deque(maxlen=100)
        self.token_cache = None
        self.token_expiry = None
        self.start_time = datetime.now()
        self._token_lock = asyncio.Lock()
    
    def can_accept_connection(self) -> bool:
        return self.active_connections < MAX_CONCURRENT_CONNECTIONS
    
    def add_connection(self):
        self.active_connections += 1
        self.total_connections += 1
        self.connection_history.append({
            'timestamp': datetime.now().isoformat(),
            'action': 'connected',
            'active': self.active_connections,
            'total': self.total_connections
        })
    
    def remove_connection(self):
        self.active_connections = max(0, self.active_connections - 1)
        self.connection_history.append({
            'timestamp': datetime.now().isoformat(),
            'action': 'disconnected',
            'active': self.active_connections
        })
    

    async def get_cached_token(self):
        """Cache token to optimize performance"""
        async with self._token_lock:
            now = datetime.now()
            if self.token_cache and self.token_expiry and now < self.token_expiry:
                return self.token_cache
            token = get_access_token()
            if token:
                self.token_cache = token
                self.token_expiry = now + timedelta(minutes=50)
            return token
    
    def get_stats(self):
        uptime = datetime.now() - self.start_time
        return {
            'active_connections': self.active_connections,
            'total_connections': self.total_connections,
            'max_capacity': MAX_CONCURRENT_CONNECTIONS,
            'available_slots': MAX_CONCURRENT_CONNECTIONS - self.active_connections,
            'uptime_seconds': int(uptime.total_seconds()),
            'uptime_formatted': str(uptime).split('.')[0]
        }

manager = ConnectionManager()

async def relay_messages(ws_client: WebSocket, ws_google, access_token: str, dynamic_prompt: str):
    """Handle bidirectional message relay between client and Gemini with auto-reconnection"""
    
    # Mutable container so both coroutines can see connection swaps
    gemini = {"ws": ws_google, "swapping": False}
    
    # Store session resumption handle
    session_handle = None
    
    async def reconnect_gemini():
        """Reconnect to Gemini using session resumption handle"""
        nonlocal session_handle
        
        if not session_handle:
            logger.warning("No session handle available for reconnection")
            return None
        
        logger.info(f"🔄 Reconnecting to Gemini with session handle...")
        
        try:
            # Get fresh token
            fresh_token = await manager.get_cached_token()
            if not fresh_token:
                logger.error("Failed to get fresh token for reconnection")
                return None
            
            new_ws = await connect(
                HOST,
                extra_headers={'Authorization': f'Bearer {fresh_token}'},
                ping_interval=20,
                ping_timeout=10,
                max_size=10_000_000
            )
            
            # Send setup WITH session resumption handle to continue conversation
            reconnect_request = {
                "setup": {
                    "model": MODEL,
                    "generationConfig": {
                        "temperature": 0.7,
                        "responseModalities": ["AUDIO"],
                        "speechConfig": {
                            "voiceConfig": {
                                "prebuiltVoiceConfig": {
                                    "voiceName": "Aoede"
                                }
                            }
                        }
                    },
                    "systemInstruction": {
                        "parts": [{"text": dynamic_prompt}]
                    },
                    "input_audio_transcription": {},
                    "output_audio_transcription": {},
                    "context_window_compression": {
                        "sliding_window": {},
                        "trigger_tokens": 50000
                    },
                    "session_resumption": {
                        "handle": session_handle  # Resume from saved handle
                    }
                }
            }
            
            await new_ws.send(json.dumps(reconnect_request))
            
            # Wait for setupComplete from new connection
            setup_response = await asyncio.wait_for(new_ws.recv(), timeout=10)
            setup_data = json.loads(setup_response.decode('utf-8'))
            
            if 'setupComplete' in setup_data:
                logger.info("✅ Gemini session resumed successfully!")
                
                # Update session handle if provided in setupComplete
                if 'sessionResumptionUpdate' in setup_data:
                    update = setup_data['sessionResumptionUpdate']
                    if update.get('newHandle'):
                        session_handle = update['newHandle']
                        logger.debug("Session handle updated from setupComplete")
                
                return new_ws
            else:
                logger.warning(f"Unexpected setup response: {list(setup_data.keys())}")
                await new_ws.close()
                return None
                
        except Exception as e:
            logger.error(f"❌ Reconnection failed: {e}")
            return None
    
    async def client2server():
        """Browser → Gemini (audio + video)"""
        msg_count = 0
        audio_chunk_count = 0
        try:
            while True:
                message = await ws_client.receive_text()
                msg_count += 1
                data = json.loads(message)
                
                # Skip sending during connection swap
                if gemini["swapping"]:
                    continue
                
                # ✅ Filter out frontend-only control messages (not valid Gemini API messages)
                # Gemini only accepts: realtimeInput, clientContent, toolResponse, etc.
                if 'type' in data:
                    msg_type = data.get('type')
                    logger.info(f"Frontend control message intercepted (not forwarded to Gemini): type={msg_type}")
                    continue  # Don't forward to Gemini
                
                # Logging (only in debug mode)
                if 'realtimeInput' in data:
                    audio_chunk_count += 1  # type: ignore
                    if audio_chunk_count % 100 == 0:
                        logger.debug(f"Media chunks sent: {audio_chunk_count}")
                else:
                    logger.debug(f"Browser→Gemini message #{msg_count}")
                
                try:
                    await gemini["ws"].send(message)
                except Exception as e:
                    logger.warning(f"Send to Gemini failed (may be swapping): {e}")
        except WebSocketDisconnect:
            logger.debug("Client disconnected from relay")
        except Exception as e:
            logger.error(f"Error client2server: {e}")
    
    async def server2client():
        """Gemini → Browser with auto-reconnection on goAway"""
        nonlocal session_handle
        msg_count = 0
        
        while True:
            try:
                current_ws = gemini["ws"]
                async for message in current_ws:  # type: ignore
                    msg_count += 1  # type: ignore
                    data = json.loads(message.decode('utf-8'))
                    
                    # Handle session resumption updates
                    if 'sessionResumptionUpdate' in data:
                        update = data['sessionResumptionUpdate']
                        if update.get('resumable') and update.get('newHandle'):
                            session_handle = update['newHandle']
                            logger.debug("Session resumption handle updated")
                    
                    # Handle GoAway message — trigger reconnection
                    if 'goAway' in data:
                        time_left = data['goAway'].get('timeLeft', 'unknown')
                        logger.warning(f"⚠️ GoAway received! Connection closing in {time_left}. Reconnecting...")
                        render_log(f"GEMINI_GOAWAY time_left={time_left} reconnecting")

                        # Mark as swapping to pause client2server sends
                        gemini["swapping"] = True
                        
                        # Reconnect using session handle
                        new_ws = await reconnect_gemini()
                        
                        if new_ws:
                            old_ws = gemini["ws"]
                            gemini["ws"] = new_ws
                            gemini["swapping"] = False
                            
                            # Close old connection gracefully
                            try:
                                await old_ws.close()
                            except Exception:
                                pass
                            
                            logger.info("🔄 Connection swapped. Continuing conversation...")
                            render_log("GEMINI_RECONNECTED session resumed OK")
                            break  # Break inner loop to restart with new connection
                        else:
                            gemini["swapping"] = False
                            logger.error("Reconnection failed. Continuing with current connection...")
                            render_log("GEMINI_RECONNECT_FAIL")
                        
                        # Don't forward goAway to frontend
                        continue
                    
                    # Forward all other messages to frontend
                    if 'serverContent' in data:
                        content = data['serverContent']
                        
                        if 'modelTurn' in content:
                            logger.debug("AI Speaking")
                        
                        if 'outputTranscription' in content:
                            text = content['outputTranscription'].get('text', '')
                            logger.debug(f"AI said: {text}")
                        
                        if 'inputTranscription' in content:
                            text = content['inputTranscription'].get('text', '')
                            is_final = content['inputTranscription'].get('isFinal', False)
                            if is_final:
                                logger.debug(f"User said: {text}")
                        
                        if 'generationComplete' in content:
                            logger.debug("Generation complete")
                    
                    elif 'setupComplete' in data:
                        logger.debug("Setup complete")
                    
                    await ws_client.send_text(message.decode('utf-8'))
                    
            except Exception as e:
                # If we're swapping, this is expected — wait and retry
                if gemini["swapping"]:
                    await asyncio.sleep(0.1)
                    continue
                
                logger.error(f"Error server2client: {e}")
                break  # Exit on real errors
        
    # Set timeout for the entire connection
    try:
        await asyncio.wait_for(
            asyncio.gather(
                client2server(),
                server2client(),
                return_exceptions=True
            ),
            timeout=CONNECTION_TIMEOUT
        )
    except asyncio.TimeoutError:
        logger.warning(f"Connection timeout after {CONNECTION_TIMEOUT} seconds")

@app.get("/")
async def root():
    """API information endpoint"""
    stats = manager.get_stats()
    return {
        "status": "online",
        "service": "Voice + Video Interview Bot API",
        "version": "2.0.0",
        "model": MODEL_ID,
        "features": ["audio", "video", "transcription"],
        "websocket_endpoint": "/ws/interview",
        **stats
    }

# UI Configuration endpoint — fetches branding per session via Lambda
@app.get("/api/config")
async def get_config(user_id: str, video_token: str, debug: bool = False):
    """Get UI configuration by calling Lambda function (DocumentDB lookup)."""
    defaults = {
        "top_right_logo_url": None,
        "organization_name": "Edmyst AI",
        "assessment_id": None,
        "assessment_result_id": None,
        "conversation_id": None,
    }
    debug_info = {
        "app_env": APP_ENV,
        "lambda_url": LAMBDA_SESSION_URL,
        "lookup": {
            "video_token": video_token,
            "user_id": user_id
        },
        "lambda_status": None,
        "fallback_reason": None
    }

    try:
        data, status, err_snip = await fetch_lambda_session_response(user_id, video_token)
        debug_info["lambda_status"] = status
        if data is not None:
            defaults["assessment_id"] = data.get("assessment_id")
            defaults["assessment_result_id"] = data.get("assessment_result_id")
            cid = extract_conversation_id_from_payload(data)
            if cid:
                defaults["conversation_id"] = cid
            if data.get("organization_name"):
                defaults["organization_name"] = data["organization_name"]
            if data.get("top_right_logo_url"):
                defaults["top_right_logo_url"] = data["top_right_logo_url"]
            logger.info(
                "Config resolved via Lambda | env=%s user_id=%s org=%s",
                APP_ENV,
                user_id,
                defaults["organization_name"],
            )
            render_log(
                f"CONFIG_OK user_id={user_id} assessment_result_id={defaults.get('assessment_result_id')} "
                f"conversation_id={defaults.get('conversation_id')} org={defaults.get('organization_name')}"
            )
        elif status == 404:
            debug_info["fallback_reason"] = "session_not_found_in_lambda"
            logger.warning(
                "Config fallback: Lambda returned 404 | user_id=%s video_token=%s",
                user_id,
                video_token,
            )
        elif status is not None:
            debug_info["fallback_reason"] = f"lambda_error_{status}"
            debug_info["lambda_body"] = err_snip
            logger.error(
                "Config fallback: Lambda error %s | user_id=%s",
                status,
                user_id,
            )
        else:
            debug_info["fallback_reason"] = "lambda_unreachable"
            if err_snip:
                debug_info["error"] = err_snip
    except Exception as e:
        debug_info["fallback_reason"] = "exception"
        debug_info["error"] = str(e)
        logger.exception(
            "Failed to fetch config from Lambda | user_id=%s video_token=%s",
            user_id, video_token
        )

    if debug:
        defaults["_debug"] = debug_info

    return defaults

@app.get("/health")
async def health_check():
    """Health check for monitoring and load balancers"""
    stats = manager.get_stats()
    is_healthy = stats['active_connections'] < MAX_CONCURRENT_CONNECTIONS
    
    return {
        "status": "healthy" if is_healthy else "at_capacity",
        "video_support": True,
        "rate_limits": "unlimited",
        **stats
    }

@app.get("/stats")
async def get_stats():
    """Detailed statistics endpoint"""
    stats = manager.get_stats()
    return {
        **stats,
        "recent_activity": list(manager.connection_history)[-20:],  # type: ignore
        "configuration": {
            "max_concurrent_connections": MAX_CONCURRENT_CONNECTIONS,
            "connection_timeout": CONNECTION_TIMEOUT,
            "model": MODEL_ID,
            "location": LOCATION
        }
    }

@app.websocket("/ws/interview")
async def websocket_interview(websocket: WebSocket):
    """Main WebSocket endpoint for voice + video interview with session tracking"""
    
    # Extract query parameters from WebSocket URL
    query_params = dict(websocket.query_params)
    assessment_id = query_params.get('assessment_id')
    user_id = query_params.get('user_id')
    assessment_result_id = query_params.get('assessment_result_id')
    
    # Check capacity
    if not manager.can_accept_connection():
        await websocket.close(code=1008, reason="Server at capacity")
        logger.warning(f"Connection rejected - At capacity ({manager.active_connections}/{MAX_CONCURRENT_CONNECTIONS})")
        return
    
    await websocket.accept()
    manager.add_connection()
    
    connection_id = manager.total_connections
    
    logger.info(f"Client #{connection_id} connected | Assessment: {assessment_result_id} | User: {user_id} ({manager.active_connections}/{MAX_CONCURRENT_CONNECTIONS} active)")
    render_log(
        f"WS_OPEN conn={connection_id} user={user_id!r} assessment_result_id={assessment_result_id!r} "
        f"assessment_id={assessment_id!r} active={manager.active_connections}"
    )

    # Get cached token for better performance
    access_token = await manager.get_cached_token()
    
    if not access_token:
        logger.error("Failed to get access token")
        manager.remove_connection()
        await websocket.close(code=1011, reason="Authentication failed")
        return
    
    try:
        async with connect(
            HOST,
            extra_headers={'Authorization': f'Bearer {access_token}'},
            ping_interval=20,
            ping_timeout=10,
            max_size=10_000_000  # 10MB max message size for video
        ) as ws_google:
            # Determine current time in India (IST)
            utc_now = datetime.utcnow()
            ist_now = utc_now + timedelta(hours=5, minutes=30)
            current_time_str = ist_now.strftime("%A, %d %B %Y, %I:%M %p")
            
            # Fetch assessment data for dynamic prompt
            assessment_data = await fetch_assessment_data(assessment_id)
            base_prompt = build_interview_prompt(assessment_data)
            
            # Dynamic System Prompt with Time Context
            dynamic_prompt = base_prompt + f"\n\n[CURRENT CONTEXT]\nCurrent Time (IST): {current_time_str}\nDefault Location: India"

            # Setup with audio and video support + UNLIMITED SESSION TIME
            initial_request = {
                "setup": {
                    "model": MODEL,
                    "generationConfig": {
                        "temperature": 0.7,
                        "responseModalities": ["AUDIO"],
                        "speechConfig": {
                            "voiceConfig": {
                                "prebuiltVoiceConfig": {
                                    "voiceName": "Aoede"
                                }
                            }
                        }
                    },
                    "systemInstruction": {
                        "parts": [{"text": dynamic_prompt}]
                    },
                    "input_audio_transcription": {},
                    "output_audio_transcription": {},
                    # 🔥 CRITICAL: Enable context window compression for unlimited session time
                    "context_window_compression": {
                        "sliding_window": {},
                        "trigger_tokens": 50000  # Compress when context reaches 50K tokens
                    },
                    # 🔥 Enable session resumption for handling connection resets
                    "session_resumption": {}
                }
            }
            
            await ws_google.send(json.dumps(initial_request))
            
            logger.debug(f"Client #{connection_id} - AI initialized with video and transcription")
            
            await relay_messages(websocket, ws_google, access_token, dynamic_prompt)
            
    except WebSocketDisconnect:
        logger.info(f"Client #{connection_id} disconnected")
        render_log(f"WS_DISCONNECT conn={connection_id} reason=client_disconnect user={user_id!r}")
    except Exception as e:
        logger.error(f"Client #{connection_id} error: {e}")
        render_log(f"WS_ERROR conn={connection_id} user={user_id!r} error={e!r}")
        try:
            await websocket.close(code=1011, reason=str(e))
        except Exception:
            pass
    finally:
        manager.remove_connection()
        logger.info(f"Client #{connection_id} session ended. Active: {manager.active_connections}/{MAX_CONCURRENT_CONNECTIONS}")
        render_log(
            f"WS_CLOSE conn={connection_id} user={user_id!r} active_now={manager.active_connections}"
        )



# Network Info Endpoint for latency measurement
@app.get("/api/network-info")
async def network_info():
    """
    Returns server timestamp for client-side latency calculation.
    This endpoint is used by the frontend to measure network quality.
    """
    return {
        "timestamp": int(time.time() * 1000),  # milliseconds
        "status": "ok",
        "server_time": datetime.now().isoformat()
    }


# ============================================================
# SPEED TEST API (Business Reusable)
# ============================================================

# Store speed test results for analytics
speed_test_results = []

@app.get("/api/speed-test/download")
async def speed_test_download(bytes: int = 100000):
    """
    Serve binary data for speed test.
    Client downloads this and measures time to calculate bandwidth.
    Args:
        bytes: Size of test data (default 100KB, max 1MB)
    """
    # Limit max size to 1MB to prevent abuse
    size = min(bytes, 1000000)
    # Generate random bytes for download
    data = os.urandom(size)
    
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Length": str(size),
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "X-Speed-Test": "true"
        }
    )

@app.post("/api/speed-test/report")
async def speed_test_report(
    speed_mbps: float,
    quality: str = "unknown",
    user_agent: str = None
):
    """
    Report speed test result for analytics.
    Args:
        speed_mbps: Measured download speed in Mbps
        quality: Network quality (good/fair/poor)
        user_agent: Client user agent
    """
    result = {
        "timestamp": datetime.now().isoformat(),
        "speed_mbps": speed_mbps,
        "quality": quality,
        "user_agent": user_agent
    }
    speed_test_results.append(result)
    
    # Keep only last 1000 results in memory
    if len(speed_test_results) > 1000:
        speed_test_results.pop(0)
    
    logger.info(f"Speed test reported: {speed_mbps:.1f} Mbps ({quality})")
    return {"status": "recorded", "speed_mbps": speed_mbps}

@app.get("/api/speed-test/stats")
async def speed_test_stats():
    """
    Get speed test analytics (last 24 hours summary).
    """
    if not speed_test_results:
        return {"count": 0, "avg_speed": 0, "min_speed": 0, "max_speed": 0}
    
    speeds = [r["speed_mbps"] for r in speed_test_results]
    return {
        "count": len(speeds),
        "avg_speed": round(sum(speeds) / len(speeds), 2),
        "min_speed": round(min(speeds), 2),
        "max_speed": round(max(speeds), 2),
        "quality_distribution": {
            "good": sum(1 for r in speed_test_results if r["quality"] == "good"),
            "fair": sum(1 for r in speed_test_results if r["quality"] == "fair"),
            "poor": sum(1 for r in speed_test_results if r["quality"] == "poor")
        }
    }

# ============================================================
# RECORDING & ANALYSIS ENDPOINTS
# ============================================================

# Directory paths for recordings
RECORDINGS_DIR = Path(__file__).parent / "recordings"
AUDIO_DIR = RECORDINGS_DIR / "audio"
AUDIO_USER_DIR = AUDIO_DIR / "user"
AUDIO_COMBINED_DIR = AUDIO_DIR / "combined"
SCREEN_DIR = RECORDINGS_DIR / "screen"
TRANSCRIPTS_DIR = RECORDINGS_DIR / "transcripts"

# Ensure directories exist
for dir_path in [AUDIO_DIR, AUDIO_USER_DIR, AUDIO_COMBINED_DIR, SCREEN_DIR, TRANSCRIPTS_DIR]:
    dir_path.mkdir(parents=True, exist_ok=True)

import boto3

# AWS S3 Configuration
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
AWS_S3_BUCKET = os.getenv("AWS_S3_BUCKET", "edy-temp-videos")

# Initialize S3 Client
s3_client = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    region_name=AWS_REGION
)


class TranscriptTurnModel(BaseModel):
    role: str
    content: str


class PendingTranscriptRequest(BaseModel):
    assessment_id: str = ""
    assessment_result_id: str = ""
    user_id: str = ""
    video_token: str = ""
    messages: list[TranscriptTurnModel]


@app.post("/api/transcript/pending")
async def post_pending_live_transcript(body: PendingTranscriptRequest):
    """
    Store live interview turns (user + assistant) before uploads finish.
    Mic-audio upload triggers STT; pipeline merges with this transcript for MongoDB.
    """
    if not (body.video_token or "").strip():
        raise HTTPException(status_code=400, detail="video_token is required")
    msgs = normalize_live_messages([t.model_dump() for t in body.messages])
    render_log(
        f"TRANSCRIPT_PENDING turns={len(msgs)} assessment_result_id={(body.assessment_result_id or '').strip()!r} "
        f"video_token={(body.video_token or '')[:16]}…"
    )
    await store_pending_live_transcript(
        (body.assessment_result_id or "").strip(),
        body.video_token.strip(),
        msgs,
    )
    return {"status": "ok", "turns_stored": len(msgs)}


@app.post("/api/upload-recording")
async def upload_recording(
    file: UploadFile = File(...),
    recording_type: str = Form("audio"),
    assessment_id: str = Form(""),
    assessment_result_id: str = Form(""),
    user_id: str = Form(""),
    video_token: str = Form(""),
    client_conversation_id: str = Form("", alias="conversation_id"),
):
    """
    Upload recordings to S3.
    - Screen: (1) Tavus pipeline s3://TAVUS_BUCKET/tavus/{conversation_id}/{ts_ms}
      (2) duplicate copy on AWS_S3_BUCKET at ai_video_to_audio_nterview/screen/{assessment_result_id}_{timestamp}.webm
    - Mic / combined / rrweb: existing ai_video_to_audio_nterview/... on AWS_S3_BUCKET.
    User mic (`audio`) schedules background STT + transcript JSON + Mongo update.
    """
    session_id = str(uuid.uuid4())
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_folder = "ai_video_to_audio_nterview"

    file_bytes = await file.read()
    content_type = file.content_type or "application/octet-stream"
    render_log(
        f"UPLOAD_START type={recording_type} bytes={len(file_bytes)} "
        f"assessment_result_id={assessment_result_id!r} user_id={user_id!r}"
    )

    conversation_id: str | None = None
    target_bucket = AWS_S3_BUCKET
    s3_key: str
    filename: str
    edy_temp_screen_key: str | None = None
    edy_temp_screen_copy_error: str | None = None

    if recording_type == "screen":
        # Screen recording: simple upload to edy-temp-videos only
        subfolder = "screen"
        extension = ".webm"
        filename = f"{assessment_result_id or session_id}_{timestamp}{extension}"
        s3_key = f"{base_folder}/{subfolder}/{filename}"

    elif recording_type == "combined_audio":
        # Combined audio: primary upload to Tavus bucket (needs conversation_id)
        if not (user_id or "").strip() or not (video_token or "").strip():
            raise HTTPException(
                status_code=400,
                detail="user_id and video_token are required for combined_audio recording",
            )
        conversation_id = await resolve_conversation_id_for_screen_upload(
            user_id, video_token, assessment_result_id
        )
        if not conversation_id and ALLOW_CLIENT_CONVERSATION_ID_FOR_SCREEN_UPLOAD:
            cand = (client_conversation_id or "").strip()
            if cand and _plausible_conversation_id(cand):
                conversation_id = cand
                logger.warning(
                    "[Tavus] Using client conversation_id for combined_audio S3 key "
                    "assessment_result_id=%s",
                    assessment_result_id,
                )
                render_log(
                    f"UPLOAD_COMBINED_CLIENT_CID_FALLBACK conversation_id={conversation_id!r} "
                    f"assessment_result_id={assessment_result_id!r}"
                )
        if not conversation_id:
            logger.error(
                "[Tavus] conversation_id unresolved for combined_audio | assessment_result_id=%s video_token=%s user_id=%s",
                assessment_result_id,
                video_token,
                user_id,
            )
            render_log(
                f"UPLOAD_COMBINED_FAIL no_conversation_id assessment_result_id={assessment_result_id!r} "
                f"video_token={video_token!r}"
            )
            raise HTTPException(
                status_code=400,
                detail="conversation_id could not be resolved for combined_audio; "
                "fix Lambda session JSON, set CONVERSATION_RESOLVE_URL, or for dev only set "
                "ALLOW_CLIENT_CONVERSATION_ID_FOR_SCREEN_UPLOAD=1 and pass conversation_id in the form",
            )
        ts_ms = int(time.time() * 1000)
        s3_key = f"tavus/{conversation_id}/{ts_ms}"
        target_bucket = TAVUS_RECORDINGS_BUCKET
        filename = str(ts_ms)
        ct_out = content_type
        if "webm" not in (ct_out or "").lower():
            if file_bytes[:4] == b"\x1a\x45\xdf\xa3":
                ct_out = "audio/webm"
            elif not (ct_out or "").startswith("audio/"):
                ct_out = "audio/webm"
        content_type = ct_out

    elif recording_type == "rrweb":
        subfolder = "rrweb"
        extension = ".json"
        filename = f"{assessment_result_id or session_id}_{timestamp}{extension}"
        s3_key = f"{base_folder}/{subfolder}/{filename}"
    elif recording_type == "candidate_photo":
        subfolder = "candidate_photo"
        extension = ".jpg"
        filename = f"{assessment_result_id or session_id}_{timestamp}{extension}"
        s3_key = f"{base_folder}/{subfolder}/{filename}"
    else:
        subfolder = "user_audio"
        extension = ".webm"
        filename = f"{assessment_result_id or session_id}_{timestamp}{extension}"
        s3_key = f"{base_folder}/{subfolder}/{filename}"

    meta = {
        "assessment_id": assessment_id,
        "assessment_result_id": assessment_result_id,
        "user_id": user_id,
        "video_token": video_token,
        "recording_type": recording_type,
        "uploaded_at": datetime.now().isoformat(),
    }
    if conversation_id:
        meta["conversation_id"] = conversation_id

    try:
        s3_client.upload_fileobj(
            io.BytesIO(file_bytes),
            target_bucket,
            s3_key,
            ExtraArgs={
                "ContentType": content_type,
                "Metadata": {k: str(v) for k, v in meta.items() if v is not None},
            },
        )

        if recording_type == "combined_audio" and conversation_id:
            logger.info(
                "[Tavus] Combined audio upload OK bucket=%s key=%s conversation_id=%s assessment_result_id=%s",
                target_bucket,
                s3_key,
                conversation_id,
                assessment_result_id,
            )
            render_log(
                f"UPLOAD_OK combined_audio bucket={target_bucket} key={s3_key} conversation_id={conversation_id} "
                f"bytes={len(file_bytes)}"
            )
            # Second copy: duplicate on edy-temp-videos (human-readable path)
            edy_filename = f"{assessment_result_id or session_id}_{timestamp}.webm"
            edy_temp_combined_key = f"{base_folder}/combined_audio/{edy_filename}"
            try:
                s3_client.upload_fileobj(
                    io.BytesIO(file_bytes),
                    AWS_S3_BUCKET,
                    edy_temp_combined_key,
                    ExtraArgs={
                        "ContentType": content_type,
                        "Metadata": {k: str(v) for k, v in meta.items() if v is not None},
                    },
                )
                logger.info(
                    "[Edy] Combined audio copy OK bucket=%s key=%s",
                    AWS_S3_BUCKET,
                    edy_temp_combined_key,
                )
                render_log(
                    f"UPLOAD_OK combined_edytemp bucket={AWS_S3_BUCKET} key={edy_temp_combined_key} "
                    f"bytes={len(file_bytes)}"
                )
            except Exception as copy_exc:
                edy_temp_screen_copy_error = str(copy_exc)
                logger.error(
                    "[Edy] Combined audio copy to %s failed: %s | key=%s",
                    AWS_S3_BUCKET,
                    copy_exc,
                    edy_temp_combined_key,
                )
                render_log(f"UPLOAD_COMBINED_EDYTEMP_FAIL key={edy_temp_combined_key!r} error={copy_exc!r}")
        else:
            logger.info(
                "Uploaded to S3: s3://%s/%s | Assessment: %s",
                target_bucket,
                s3_key,
                assessment_result_id,
            )
            render_log(
                f"UPLOAD_OK type={recording_type} bucket={target_bucket} key={s3_key} bytes={len(file_bytes)}"
            )

        payload: dict = {
            "status": "success",
            "session_id": session_id,
            "filename": filename,
            "s3_bucket": target_bucket,
            "s3_key": s3_key,
            "s3_uri": f"s3://{target_bucket}/{s3_key}",
            "recording_type": recording_type,
            "metadata": {
                "assessment_id": assessment_id,
                "assessment_result_id": assessment_result_id,
                "user_id": user_id,
            },
        }
        if conversation_id:
            payload["conversation_id"] = conversation_id
            payload["tavus_recording_key"] = s3_key

        if recording_type == "combined_audio" and edy_temp_combined_key and not edy_temp_screen_copy_error:
            payload["edy_temp_videos_combined"] = {
                "s3_bucket": AWS_S3_BUCKET,
                "s3_key": edy_temp_combined_key,
                "s3_uri": f"s3://{AWS_S3_BUCKET}/{edy_temp_combined_key}",
                "https_url": (
                    f"https://{AWS_S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{edy_temp_combined_key}"
                ),
            }
        elif recording_type == "combined_audio" and edy_temp_screen_copy_error:
            payload["edy_temp_videos_combined_error"] = edy_temp_screen_copy_error

        if recording_type == "audio" and file_bytes:
            asyncio.create_task(
                run_user_audio_post_process(
                    file_bytes,
                    s3_client=s3_client,
                    s3_bucket=AWS_S3_BUCKET,
                    base_folder=base_folder,
                    assessment_id=assessment_id,
                    assessment_result_id=assessment_result_id,
                    user_id=user_id,
                    video_token=video_token,
                    audio_s3_key=s3_key,
                    content_type=content_type,
                )
            )
            payload["transcript_pipeline"] = "scheduled"
            render_log(
                f"TRANSCRIPT_PIPELINE_SCHEDULED audio_key={s3_key} assessment_result_id={assessment_result_id!r}"
            )

        return payload

    except Exception as e:
        logger.error(f"S3 Upload Error: {e}")
        render_log(f"UPLOAD_FAIL type={recording_type} error={e!r}")
        raise HTTPException(status_code=500, detail=f"S3 Upload failed: {str(e)}")


@app.post("/api/transcribe")
async def transcribe_audio(file: UploadFile = File(...)):
    """
    Transcribe audio file using Vertex AI Gemini 2.5 Flash.
    Returns text file with AI/User separated transcription with timestamps.
    """
    session_id = str(uuid.uuid4())
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Save uploaded audio temporarily
    temp_audio_path = AUDIO_DIR / f"temp_{session_id}.webm"
    content = await file.read()
    with open(temp_audio_path, "wb") as f:
        f.write(content)
    
    try:
        # Use Vertex AI Gemini 2.5 Flash for transcription
        model = GenerativeModel('gemini-2.5-flash')
        
        # Read audio file and create Part
        with open(temp_audio_path, "rb") as f:
            audio_data = f.read()
        audio_part = Part.from_data(audio_data, mime_type="audio/webm")
        
        # Request transcription with speaker separation
        prompt = """
        Transcribe this audio interview conversation.
        
        IMPORTANT: Separate the speakers as follows:
        - AI Interviewer: The AI voice asking questions
        - User: The human candidate answering questions
        
        Format each line as:
        [TIMESTAMP] SPEAKER: Text
        
        Example:
        [00:00:05] AI: Good afternoon, could you please introduce yourself?
        [00:00:12] User: Yes, my name is John and I have 5 years of experience.
        
        Provide accurate timestamps in MM:SS format.
        Transcribe the COMPLETE conversation.
        """
        
        response = model.generate_content([audio_part, prompt])
        transcript_text = response.text
        
        # Save transcript to file
        transcript_filename = f"transcript_{session_id}_{timestamp}.txt"
        transcript_path = TRANSCRIPTS_DIR / transcript_filename
        
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(f"# Interview Transcript\n")
            f.write(f"# Session ID: {session_id}\n")
            f.write(f"# Generated: {datetime.now().isoformat()}\n\n")
            f.write(transcript_text)
        
        # Extract user-only transcript for scoring
        user_lines = []
        for line in transcript_text.split('\n'):
            if 'User:' in line or 'USER:' in line or 'user:' in line:
                user_lines.append(line)
        
        user_transcript = '\n'.join(user_lines)
        
        # Cleanup temp file
        temp_audio_path.unlink(missing_ok=True)
        
        logger.info(f"Transcription complete: {transcript_filename}")
        
        return {
            "status": "success",
            "session_id": session_id,
            "transcript_file": transcript_filename,
            "transcript_path": str(transcript_path),
            "full_transcript": transcript_text,
            "user_transcript": user_transcript
        }
        
    except Exception as e:
        temp_audio_path.unlink(missing_ok=True)
        logger.error(f"Transcription error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Transcription failed: {str(e)}")


@app.post("/api/score-communication")
async def score_communication(file: UploadFile = File(...)):
    """
    Analyze user's audio for communication skills using Vertex AI.
    Scores: Pitch, Calmness, Fluency, Confidence, Clarity (0-10 total)
    """
    session_id = str(uuid.uuid4())
    
    # Save uploaded audio temporarily
    temp_audio_path = AUDIO_DIR / f"temp_comm_{session_id}.webm"
    content = await file.read()
    with open(temp_audio_path, "wb") as f:
        f.write(content)
    
    try:
        model = GenerativeModel('gemini-2.5-flash')
        
        # Read audio and create Part
        with open(temp_audio_path, "rb") as f:
            audio_data = f.read()
        audio_part = Part.from_data(audio_data, mime_type="audio/webm")
        
        prompt = """
        Analyze this interview audio for the USER/CANDIDATE's communication skills ONLY.
        Ignore the AI interviewer's voice - focus only on the human candidate.
        
        Score each category from 0-2 points:
        
        1. PITCH (0-2): Is the voice pitch appropriate, not too monotone or too varied?
        2. CALMNESS (0-2): How calm and composed does the candidate sound?
        3. FLUENCY (0-2): How smooth is the speech flow? Minimal filler words (um, uh)?
        4. CONFIDENCE (0-2): Does the candidate sound confident and assured?
        5. CLARITY (0-2): How clear and understandable is the speech?
        
        Respond in this EXACT JSON format:
        {
            "pitch": {"score": X, "feedback": "..."},
            "calmness": {"score": X, "feedback": "..."},
            "fluency": {"score": X, "feedback": "..."},
            "confidence": {"score": X, "feedback": "..."},
            "clarity": {"score": X, "feedback": "..."},
            "total_score": X,
            "overall_feedback": "..."
        }
        
        Be strict but fair in scoring.
        """
        
        response = model.generate_content([audio_part, prompt])
        
        # Parse JSON response
        response_text = response.text
        # Clean up response if wrapped in markdown
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0]
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0]
        
        score_data = json.loads(response_text.strip())
        
        # Cleanup
        temp_audio_path.unlink(missing_ok=True)
        
        logger.info(f"Communication score: {score_data.get('total_score', 'N/A')}/10")
        
        return {
            "status": "success",
            "session_id": session_id,
            "score_type": "communication",
            "scores": score_data
        }
        
    except json.JSONDecodeError as e:
        temp_audio_path.unlink(missing_ok=True)
        logger.error(f"JSON parse error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to parse score response")
    except Exception as e:
        temp_audio_path.unlink(missing_ok=True)
        logger.error(f"Communication scoring error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Scoring failed: {str(e)}")


@app.post("/api/score-technical")
async def score_technical(file: UploadFile = File(...)):
    """
    Analyze transcript text for technical skills using Vertex AI.
    Scores: Technical accuracy, Problem-solving, Relevance (0-10 total)
    """
    session_id = str(uuid.uuid4())
    
    # Read transcript text file
    content = await file.read()
    transcript_text = content.decode("utf-8")
    
    try:
        model = GenerativeModel('gemini-2.5-flash')
        
        prompt = f"""
        Analyze this interview transcript for the CANDIDATE's technical skills.
        Focus ONLY on the User/Candidate responses, not the AI questions.
        
        TRANSCRIPT:
        {transcript_text}
        
        Score each category:
        
        1. TECHNICAL ACCURACY (0-4): Are the technical answers correct and precise?
        2. PROBLEM SOLVING (0-3): Does the candidate show good problem-solving approach?
        3. RELEVANCE (0-3): Are answers relevant to the questions asked?
        
        Respond in this EXACT JSON format:
        {{
            "technical_accuracy": {{"score": X, "feedback": "..."}},
            "problem_solving": {{"score": X, "feedback": "..."}},
            "relevance": {{"score": X, "feedback": "..."}},
            "total_score": X,
            "overall_feedback": "...",
            "strengths": ["...", "..."],
            "areas_to_improve": ["...", "..."]
        }}
        
        Be strict but fair. Score based on actual technical content.
        """
        
        response = model.generate_content(prompt)
        
        # Parse JSON response
        response_text = response.text
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0]
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0]
        
        score_data = json.loads(response_text.strip())
        
        logger.info(f"Technical score: {score_data.get('total_score', 'N/A')}/10")
        
        return {
            "status": "success",
            "session_id": session_id,
            "score_type": "technical",
            "scores": score_data
        }
        
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to parse score response")
    except Exception as e:
        logger.error(f"Technical scoring error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Scoring failed: {str(e)}")


@app.get("/api/recordings/{session_id}")
async def get_recording(session_id: str, recording_type: str = "audio"):
    """
    Get recording file by session ID.
    """
    if recording_type == "screen":
        search_dir = SCREEN_DIR
    else:
        search_dir = AUDIO_DIR
    
    # Find file matching session_id
    for file_path in search_dir.iterdir():
        if session_id in file_path.name:
            return FileResponse(
                path=file_path,
                filename=file_path.name,
                media_type="audio/webm" if recording_type == "audio" else "video/webm"
            )
    
    raise HTTPException(status_code=404, detail="Recording not found")


@app.get("/api/transcript/{session_id}")
async def get_transcript(session_id: str):
    """
    Get transcript file by session ID.
    """
    for file_path in TRANSCRIPTS_DIR.iterdir():
        if session_id in file_path.name:
            return FileResponse(
                path=file_path,
                filename=file_path.name,
                media_type="text/plain"
            )
    
    raise HTTPException(status_code=404, detail="Transcript not found")


class LogEntry(BaseModel):
    level: str
    message: str
    timestamp: str
    context: dict = {}

@app.post("/api/edy/assessment-complete")
async def edy_assessment_complete_proxy(payload: dict):
    """
    Forward completion payload to Edy execute-api (same contract as Step9 / CVI ai_interview).
    Netlify static app calls this when browser CORS blocks direct POST to API Gateway.
    """
    rid = (payload.get("assessment_result_id") or "").strip()
    uid = (payload.get("user_id") or "").strip()
    vt = (payload.get("video_token") or "").strip()
    if not rid or not uid or not vt:
        raise HTTPException(
            status_code=400,
            detail="assessment_result_id, user_id, and video_token are required",
        )
    if not EDY_CANDIDATE_API_BASE:
        logger.error("[Edy] EDY_CANDIDATE_API_BASE not set — cannot proxy assessment-complete")
        raise HTTPException(
            status_code=503,
            detail="Server is not configured for Edy completion proxy (EDY_CANDIDATE_API_BASE)",
        )

    skip = {"assessment_result_id", "user_id", "video_token"}
    body = {k: v for k, v in payload.items() if k not in skip}

    url = f"{EDY_CANDIDATE_API_BASE.rstrip('/')}/update/assessment_result"
    params = {"assessment_result_id": rid, "user_id": uid, "video_token": vt}

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(url, params=params, json=body)
        render_log(f"EDY_PROXY assessment_complete status={resp.status_code} assessment_result_id={rid!r}")
        if resp.status_code >= 400:
            logger.warning(
                "[Edy] upstream error status=%s body=%s",
                resp.status_code,
                (resp.text or "")[:500],
            )
            raise HTTPException(
                status_code=502,
                detail=f"Edy API returned {resp.status_code}",
            )
        return {"status": "ok", "upstream_status": resp.status_code}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[Edy] proxy request failed")
        render_log(f"EDY_PROXY_FAIL {e!r}")
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.post("/api/log")
async def receive_frontend_log(entry: LogEntry):
    """
    Receive logs from frontend and save to frontend.log
    """
    msg = f"[{entry.level.upper()}] {entry.message} | Context: {entry.context}"
    if entry.level.lower() == "error":
        frontend_logger.error(msg)
        render_log(f"FE_ERROR {entry.message} | {entry.context}")
    elif entry.level.lower() == "warn":
        frontend_logger.warning(msg)
    else:
        frontend_logger.info(msg)
    return {"status": "logged"}



if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        workers=1,
        limit_concurrency=MAX_CONCURRENT_CONNECTIONS + 50,  # Buffer for safety
        timeout_keep_alive=75,
        ws_ping_interval=20,
        ws_ping_timeout=10
    )
    
