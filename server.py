"""
FastAPI streaming server — acts as a zero-disk-cache reverse proxy between
Telegram's file servers and the end user's browser.

Key responsibilities:
  • CORS headers for cross-origin <video> embedding.
  • HTTP Range request support (206 Partial Content) for seek/scrub.
  • Token-based access control.
  • Built-in web player served at GET /.
  • File metadata API at /api/info/{file_id}.
  • TTL cache for Telegram URL resolution.
  • Request timing and simple rate limiting middleware.
"""

from __future__ import annotations

import logging
import os
import time

import aiofiles
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

import config

logger = logging.getLogger(__name__)

# ── Telegram URL resolution cache (TTL = 5 minutes) ────────────────
_url_cache: dict[str, tuple[str, int, float]] = {}
_CACHE_TTL = 300  # seconds

# ── Simple in-memory rate limiter ───────────────────────────────────
_rate_buckets: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT = 120   # requests per window
_RATE_WINDOW = 60   # seconds


# ── Shared async HTTP client ───────────────────────────────────────
_http_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
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


# ── Lifespan (replaces deprecated on_event) ────────────────────────
@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Startup / shutdown lifecycle for the FastAPI app."""
    # Startup: warm up the HTTP client
    logger.info("Initialising httpx connection pool (max=%d)…", config.MAX_CONNECTIONS)
    await _get_client()
    yield
    # Shutdown: close the HTTP client
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None
        logger.info("httpx client closed.")


# ── FastAPI app ──────────────────────────────────────────────────────
app = FastAPI(
    title="Telegram Video Stream Proxy",
    description="Zero-cache byte-passthrough proxy for Telegram-hosted media.",
    version="2.0.0",
    docs_url=None,
    redoc_url=None,
    lifespan=_lifespan,
)

# ── CORS — allow every origin so <video> embeds work anywhere ───────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["Range", "Content-Type"],
    expose_headers=[
        "Content-Range",
        "Accept-Ranges",
        "Content-Length",
        "Content-Type",
    ],
)


# ── Request timing middleware ──────────────────────────────────────
@app.middleware("http")
async def timing_middleware(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = (time.perf_counter() - start) * 1000
    response.headers["X-Response-Time"] = f"{elapsed:.1f}ms"
    if elapsed > 500:  # only log slow requests
        logger.info(
            "%s %s → %d (%.1fms)",
            request.method, request.url.path, response.status_code, elapsed,
        )
    return response


# ── Simple rate limiting middleware ────────────────────────────────
@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    # Skip rate limiting for health checks
    if request.url.path == "/health":
        return await call_next(request)

    client_ip = request.client.host if request.client else "unknown"
    now = time.time()

    # Prune old entries
    bucket = _rate_buckets[client_ip]
    _rate_buckets[client_ip] = [t for t in bucket if now - t < _RATE_WINDOW]

    if len(_rate_buckets[client_ip]) >= _RATE_LIMIT:
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many requests. Please slow down."},
            headers={"Retry-After": str(_RATE_WINDOW)},
        )

    _rate_buckets[client_ip].append(now)
    return await call_next(request)


# ── Custom exception handlers ─────────────────────────────────────
@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=404,
        content={"detail": "Resource not found.", "path": str(request.url.path)},
    )


@app.exception_handler(500)
async def server_error_handler(request: Request, exc: Exception):
    logger.error("Internal error on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error."},
    )


# ── Helper: resolve a file_id → download URL or local path ─────────
async def _resolve_telegram_url(file_id: str) -> tuple[str, int]:
    """Call the Telegram Bot API to turn a file_id into a download source
    and return (source, file_size).

    If the Local Bot API is running in --local mode, file_path will be
    an absolute filesystem path (e.g. /app/.../file.mp4).  In that case
    we return the path prefixed with ``local://`` so the streaming
    endpoint knows to read directly from disk.

    Results are cached for 5 minutes to avoid redundant API calls
    (e.g. during seek / range requests on the same file).
    """
    now = time.time()

    # Check cache
    if file_id in _url_cache:
        url, size, cached_at = _url_cache[file_id]
        if now - cached_at < _CACHE_TTL:
            return url, size

    client = await _get_client()
    api_url = f"{config.TELEGRAM_API_URL}/bot{config.BOT_TOKEN}/getFile"
    resp = await client.get(api_url, params={"file_id": file_id})
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Telegram API error.")
    data = resp.json()
    if not data.get("ok"):
        raise HTTPException(status_code=404, detail="File not found on Telegram.")
    result = data["result"]
    file_path: str = result["file_path"]
    file_size: int = result.get("file_size", 0)

    # Local Bot API in --local mode returns absolute filesystem paths
    if file_path.startswith("/"):
        download_url = f"local://{file_path}"
        # Get accurate file size from disk
        if os.path.isfile(file_path):
            file_size = os.path.getsize(file_path)
    else:
        download_url = f"{config.TELEGRAM_API_URL}/file/bot{config.BOT_TOKEN}/{file_path}"

    # Store in cache
    _url_cache[file_id] = (download_url, file_size, now)
    return download_url, file_size


def _is_local(source: str) -> bool:
    """Return True if *source* points to a local file."""
    return source.startswith("local://")


def _local_path(source: str) -> str:
    """Strip the local:// prefix and return the filesystem path."""
    return source[len("local://"):]


# ── Helper: stream bytes from a LOCAL file in chunks ───────────────
async def _stream_local_chunks(
    path: str,
    start: int,
    end: int,
) -> AsyncGenerator[bytes, None]:
    """Read the byte range [start, end] from a local file and yield chunks."""
    remaining = end - start + 1
    async with aiofiles.open(path, "rb") as f:
        await f.seek(start)
        while remaining > 0:
            chunk_size = min(config.CHUNK_SIZE, remaining)
            chunk = await f.read(chunk_size)
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


# ── Helper: stream bytes from a REMOTE URL in chunks ───────────────
async def _stream_remote_chunks(
    url: str,
    start: int,
    end: int,
) -> AsyncGenerator[bytes, None]:
    """Fetch the byte range [start, end] from *url* and yield chunks."""
    client = await _get_client()
    headers = {"Range": f"bytes={start}-{end}"}
    async with client.stream("GET", url, headers=headers) as resp:
        if resp.status_code in (200, 206):
            bytes_sent = 0
            target = end - start + 1
            async for chunk in resp.aiter_bytes(chunk_size=config.CHUNK_SIZE):
                remaining = target - bytes_sent
                if remaining <= 0:
                    break
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
                bytes_sent += len(chunk)
                yield chunk
        else:
            logger.error("Telegram returned %s for %s", resp.status_code, url)
            raise HTTPException(status_code=502, detail="Upstream fetch failed.")


# ── Guess content type from the Telegram file_path extension ────────
def _guess_content_type(file_path: str) -> str:
    ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
    return {
        # Video
        "mp4": "video/mp4",
        "mkv": "video/x-matroska",
        "webm": "video/webm",
        "mov": "video/quicktime",
        "avi": "video/x-msvideo",
        "flv": "video/x-flv",
        "wmv": "video/x-ms-wmv",
        # Audio
        "mp3": "audio/mpeg",
        "flac": "audio/flac",
        "wav": "audio/wav",
        "ogg": "audio/ogg",
        "aac": "audio/aac",
        "m4a": "audio/mp4",
        "opus": "audio/opus",
        # Image
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "webp": "image/webp",
        "bmp": "image/bmp",
    }.get(ext, "application/octet-stream")


# ── Root: serve the static player page ──────────────────────────────
_INDEX_PATH = Path(__file__).parent / "index.html"


@app.get("/", response_class=FileResponse)
async def root():
    """Serve the built-in Plyr-based web player."""
    if _INDEX_PATH.exists():
        return FileResponse(_INDEX_PATH, media_type="text/html")
    return JSONResponse({"status": "alive", "service": "Telegram Stream Proxy v2.0"})


# ── Player redirect (convenience) ──────────────────────────────────
@app.get("/play/{file_id}")
async def player_redirect(file_id: str, token: str = Query(default="")):
    """Redirect to the web player with the stream URL pre-filled."""
    from urllib.parse import quote
    stream_url = f"/stream/{file_id}?token={quote(token, safe='')}"
    return Response(
        status_code=307,
        headers={"Location": f"/?url={quote(stream_url, safe='')}"},
    )


# ── File info API ──────────────────────────────────────────────────
@app.get("/api/info/{file_id}")
async def file_info(
    file_id: str,
    token: str = Query(default=""),
) -> JSONResponse:
    """Return file metadata from the Telegram Bot API as JSON."""
    if config.API_SECRET_TOKEN and token != config.API_SECRET_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing token.")

    source, file_size = await _resolve_telegram_url(file_id)
    content_type = _guess_content_type(source)

    # Try HEAD request if file_size is 0 and source is remote
    if file_size == 0 and not _is_local(source):
        client = await _get_client()
        head = await client.head(source, follow_redirects=True)
        cl = head.headers.get("content-length")
        if cl:
            file_size = int(cl)

    return JSONResponse({
        "file_id": file_id,
        "file_size": file_size,
        "content_type": content_type,
        "stream_url": f"/stream/{file_id}?token={token}",
    })


# ── Main streaming endpoint ─────────────────────────────────────────
@app.api_route("/stream/{file_id}", methods=["GET", "HEAD"], response_model=None)
async def stream_video(
    file_id: str,
    request: Request,
    token: str = Query(default=""),
) -> StreamingResponse | Response:
    """Stream a Telegram-hosted file to the browser with full Range
    request support for seeking / scrubbing."""

    # ── Auth check ──────────────────────────────────────────────────
    if config.API_SECRET_TOKEN and token != config.API_SECRET_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing token.")

    # ── Resolve source (local path or remote URL) + file size ──────
    source, file_size = await _resolve_telegram_url(file_id)
    is_local_file = _is_local(source)
    local_file_path = _local_path(source) if is_local_file else ""

    if is_local_file:
        # Verify the file actually exists on disk
        if not os.path.isfile(local_file_path):
            raise HTTPException(status_code=404, detail="File not found on disk.")
        file_size = os.path.getsize(local_file_path)
    elif file_size == 0:
        # Fallback: do a HEAD request to get Content-Length from Telegram.
        client = await _get_client()
        head = await client.head(source, follow_redirects=True)
        cl = head.headers.get("content-length")
        if cl:
            file_size = int(cl)

    content_type = _guess_content_type(source)

    # ── HEAD request: return metadata only ──────────────────────────
    if request.method == "HEAD":
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": content_type,
            "Cache-Control": "no-cache",
        }
        if file_size > 0:
            headers["Content-Length"] = str(file_size)
        return Response(status_code=200, headers=headers)

    # ── Parse Range header ──────────────────────────────────────────
    range_header = request.headers.get("range")

    if range_header and file_size > 0:
        # Expected format: "bytes=START-END" or "bytes=START-"
        try:
            range_spec = range_header.replace("bytes=", "").strip()
            parts = range_spec.split("-", 1)
            start = int(parts[0]) if parts[0] else 0
            end = int(parts[1]) if parts[1] else file_size - 1
        except (ValueError, IndexError):
            start, end = 0, file_size - 1

        # Clamp boundaries
        start = max(0, start)
        end = min(end, file_size - 1)
        content_length = end - start + 1

        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
            "Content-Type": content_type,
            "Cache-Control": "no-cache",
        }

        # Choose the right chunk generator
        if is_local_file:
            generator = _stream_local_chunks(local_file_path, start, end)
        else:
            generator = _stream_remote_chunks(source, start, end)

        return StreamingResponse(
            generator,
            status_code=206,
            headers=headers,
            media_type=content_type,
        )

    # ── No Range header (or unknown size) → stream entire file ──────
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": content_type,
        "Cache-Control": "no-cache",
    }
    if file_size > 0:
        headers["Content-Length"] = str(file_size)

    if is_local_file:
        generator = _stream_local_chunks(local_file_path, 0, file_size - 1 if file_size > 0 else 0)
    else:
        async def _full_stream() -> AsyncGenerator[bytes, None]:
            client = await _get_client()
            async with client.stream("GET", source) as resp:
                async for chunk in resp.aiter_bytes(chunk_size=config.CHUNK_SIZE):
                    yield chunk
        generator = _full_stream()

    return StreamingResponse(
        generator,
        status_code=200,
        headers=headers,
        media_type=content_type,
    )


# ── Health check ─────────────────────────────────────────────────────
@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "version": "2.0.0",
        "cache_entries": len(_url_cache),
    }
