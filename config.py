"""
Configuration module — loads all settings from environment variables.

This is the single source of truth for every tuneable knob.  All values
are read at import time so they can be referenced as plain module-level
constants (e.g. ``config.BOT_TOKEN``).

Required env vars:
    BOT_TOKEN          – Telegram Bot API token from @BotFather.
    API_SECRET_TOKEN   – Shared secret appended as ?token= to stream URLs.
    BASE_URL           – Public-facing root URL (e.g. https://your-domain.com).

Optional env vars:
    ALLOWED_USER_ID    – Your personal Telegram numeric user ID.
                         Only this user's uploads will be accepted.
                         Set to 0 (default) to allow everyone.
    CHUNK_SIZE         – Byte size of each proxied chunk (default 512 KB).
    PORT               – Port the FastAPI server listens on (default 8000).
    HOST               – Bind address for Uvicorn (default 0.0.0.0).
    LOG_LEVEL          – Python log level name (default INFO).
    MAX_CONNECTIONS    – httpx connection-pool ceiling (default 100).
    ENABLE_REMUX       – "true" (default) to fix video flicker by remuxing
                         uploads to fast-start MP4 in the background.
                         Requires the `ffmpeg` binary to be installed.
    REMUX_CACHE_DIR    – Where fixed copies are cached (default /tmp/stream_cache).
    REMUX_CACHE_MAX_GB – Max total size of that cache before old entries
                         are evicted, in GB (default 8).
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ─────────────────────────────────────────────────────────
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
ALLOWED_USER_ID: int = int(os.getenv("ALLOWED_USER_ID", "0"))
PROXY_URL: str = os.getenv("PROXY_URL", "")

# ── Web / Streaming ─────────────────────────────────────────────────
API_SECRET_TOKEN: str = os.getenv("API_SECRET_TOKEN", "changeme")
BASE_URL: str = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")

# Proxy chunk size in bytes (512 KB default — good balance between
# memory pressure and throughput on cheap VPSes).
CHUNK_SIZE: int = int(os.getenv("CHUNK_SIZE", str(512 * 1024)))

# Server host & port
HOST: str = os.getenv("HOST", "0.0.0.0")
PORT: int = int(os.getenv("PORT", "8000"))

# ── Logging ──────────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# ── httpx connection pool ────────────────────────────────────────────
MAX_CONNECTIONS: int = int(os.getenv("MAX_CONNECTIONS", "100"))

# ── Fast-start remux (fixes the "video glitches/flashes from frame one"
# issue that affects most phone-recorded MP4s — see remux.py) ───────
ENABLE_REMUX: bool = os.getenv("ENABLE_REMUX", "true").lower() in ("1", "true", "yes")
REMUX_CACHE_DIR: str = os.getenv("REMUX_CACHE_DIR", "/tmp/stream_cache")
REMUX_CACHE_MAX_BYTES: int = int(float(os.getenv("REMUX_CACHE_MAX_GB", "8")) * 1024 ** 3)

# ── Telegram File API base ──────────────────────────────────────────
# Set this to your Local Telegram Bot API server to allow files > 20 MB.
# e.g. http://localhost:8081
TELEGRAM_API_URL: str = os.getenv("TELEGRAM_API_URL", "https://api.telegram.org").rstrip("/")
def validate() -> None:
    """Check critical config values at startup.

    Call this once from ``main.py`` before launching any services.
    Exits with code 1 if BOT_TOKEN is missing, and prints a warning
    if the API_SECRET_TOKEN is still the insecure default.
    """
    logger = logging.getLogger(__name__)

    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN environment variable is not set — aborting.")
        sys.exit(1)

    if API_SECRET_TOKEN == "changeme":
        logger.warning(
            "API_SECRET_TOKEN is set to the default 'changeme'. "
            "Anyone who knows the URL can access your streams. "
            "Set a strong random value in production."
        )

    if ALLOWED_USER_ID == 0:
        logger.warning(
            "ALLOWED_USER_ID is 0 — the bot will accept uploads from ALL users. "
            "Set this to your Telegram numeric ID for single-user mode."
        )

    if ENABLE_REMUX and shutil.which("ffmpeg") is None:
        logger.warning(
            "ENABLE_REMUX is true but the `ffmpeg` binary was not found on PATH. "
            "Videos will keep streaming with the original flicker issue until "
            "ffmpeg is installed — see deployment notes (packages.txt / Dockerfile) "
            "for your Hugging Face Space. Set ENABLE_REMUX=false to silence this."
        )
