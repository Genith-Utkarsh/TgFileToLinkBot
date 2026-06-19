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
"""

from __future__ import annotations

import logging
import os
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

# ── Telegram File API base ──────────────────────────────────────────
# All Telegram bot file downloads go through this root.
TELEGRAM_FILE_API: str = f"https://api.telegram.org/file/bot{BOT_TOKEN}"


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
