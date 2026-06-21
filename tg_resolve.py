"""
Shared Telegram file-URL resolution + a single shared httpx client.

Previously this lived only inside server.py. It's pulled out here so
bot.py (and remux.py, indirectly) can resolve a file_id into a download
source too — needed so we can kick off a background fast-start remux
the moment a file is uploaded, instead of waiting for the first stream
request.

Raises plain exceptions (not FastAPI's HTTPException) so this module
has no FastAPI dependency and can be imported from bot.py cleanly.
server.py wraps these into HTTPException at its call sites.
"""

from __future__ import annotations

import os
import time

import httpx

import config

# ── Resolution cache (TTL = 5 minutes) ──────────────────────────────
_url_cache: dict[str, tuple[str, int, float]] = {}
_CACHE_TTL = 300  # seconds

# ── Shared async HTTP client (one pool for the whole process) ──────
_http_client: httpx.AsyncClient | None = None


async def get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0),
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=config.MAX_CONNECTIONS,
                max_keepalive_connections=20,
            ),
        )
    return _http_client


async def close_client() -> None:
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


class ResolveError(RuntimeError):
    """Raised when a file_id can't be resolved to a download source."""


async def resolve(file_id: str) -> tuple[str, int]:
    """Turn a file_id into (source, file_size).

    *source* is either an ``https://…`` download URL, or — when running
    against a Local Telegram Bot API server in --local mode — a
    ``local://`` prefixed absolute filesystem path.

    Results are cached for 5 minutes to avoid redundant getFile calls
    (e.g. repeated Range requests on the same file).
    """
    now = time.time()

    cached = _url_cache.get(file_id)
    if cached:
        url, size, cached_at = cached
        if now - cached_at < _CACHE_TTL:
            return url, size

    client = await get_client()
    api_url = f"{config.TELEGRAM_API_URL}/bot{config.BOT_TOKEN}/getFile"
    resp = await client.get(api_url, params={"file_id": file_id})
    if resp.status_code != 200:
        raise ResolveError(f"Telegram API error (status {resp.status_code}).")
    data = resp.json()
    if not data.get("ok"):
        raise ResolveError("File not found on Telegram.")
    result = data["result"]
    file_path: str = result["file_path"]
    file_size: int = result.get("file_size", 0)

    if file_path.startswith("/"):
        download_url = f"local://{file_path}"
        if os.path.isfile(file_path):
            file_size = os.path.getsize(file_path)
    else:
        download_url = f"{config.TELEGRAM_API_URL}/file/bot{config.BOT_TOKEN}/{file_path}"

    _url_cache[file_id] = (download_url, file_size, now)
    return download_url, file_size


def is_local(source: str) -> bool:
    return source.startswith("local://")


def local_path(source: str) -> str:
    return source[len("local://"):]
