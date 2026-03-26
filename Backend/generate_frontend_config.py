#!/usr/bin/env python3
"""Reads Backend/.env and writes Frontend/interview-config.js for the static HTML app."""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent
REPO_ROOT = BACKEND_DIR.parent

load_dotenv(BACKEND_DIR / ".env")

DEFAULT_API = "http://127.0.0.1:8000"

api_base = (os.getenv("PUBLIC_API_BASE") or DEFAULT_API).rstrip("/")
ws_url = (os.getenv("PUBLIC_WS_URL") or "").strip()
if not ws_url:
    ws_url = (
        api_base.replace("https://", "wss://", 1)
        .replace("http://", "ws://", 1)
        + "/ws/interview"
    )
health_url = f"{api_base}/health"
proctor_base = (os.getenv("PROCTORING_API_BASE") or "http://127.0.0.1:8010").rstrip("/")
proctor_key = os.getenv("PROCTORING_API_KEY") or ""

out_path = REPO_ROOT / "Frontend" / "interview-config.js"
cfg = {
    "API_BASE": api_base,
    "WS_URL": ws_url,
    "HEALTH_URL": health_url,
    "PROCTORING_API_BASE": proctor_base,
    "PROCTORING_API_KEY": proctor_key,
}
content = (
    "// Auto-generated from Backend/.env — run: python generate_frontend_config.py\n"
    f"window.INTERVIEW_CONFIG = {json.dumps(cfg, indent=2)};\n"
)
out_path.write_text(content, encoding="utf-8")
print(f"Wrote {out_path}")
