from __future__ import annotations
import json, os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"
VOICE_PATH = ROOT / "voice.json"

def _load(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

CONFIG = _load(CONFIG_PATH)
VOICE = _load(VOICE_PATH)

def env(name: str, required=True, default=None):
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment secret: {name}")
    return value

def secret_config():
    return {
        "gemini_api_key": env("GEMINI_API_KEY"),
        "youtube_client_id": env("YOUTUBE_CLIENT_ID"),
        "youtube_client_secret": env("YOUTUBE_CLIENT_SECRET"),
        "youtube_refresh_token": env("YOUTUBE_REFRESH_TOKEN"),
        "google_drive_credentials": env("GOOGLE_DRIVE_CREDENTIALS"),
    }

def path_from_config(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else ROOT / p
