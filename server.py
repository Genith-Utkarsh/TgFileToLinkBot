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

import asyncio
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
import remux
import tg_resolve

logger = logging.getLogger(__name__)

# ── Simple in-memory rate limiter ───────────────────────────────────
_rate_buckets: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT = 120   # requests per window
_RATE_WINDOW = 60   # seconds


# ── Lifespan (replaces deprecated on_event) ────────────────────────
@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Startup / shutdown lifecycle for the FastAPI app."""
    # Startup: warm up the shared httpx connection pool
    logger.info("Initialising httpx connection pool (max=%d)…", config.MAX_CONNECTIONS)
    await tg_resolve.get_client()
    if config.ENABLE_REMUX:
        logger.info("Fast-start remux enabled — cache dir: %s", config.REMUX_CACHE_DIR)
    yield
    # Shutdown: close the shared HTTP client
    await tg_resolve.close_client()
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
    """Wrap tg_resolve.resolve() and translate failures into the
    HTTPException FastAPI expects. The actual resolution + caching logic
    lives in tg_resolve.py so bot.py can share it too (needed to kick off
    background remux right after upload, before any stream request)."""
    try:
        return await tg_resolve.resolve(file_id)
    except tg_resolve.ResolveError as exc:
        status = 404 if "not found" in str(exc).lower() else 502
        raise HTTPException(status_code=status, detail=str(exc)) from exc


def _is_local(source: str) -> bool:
    """Return True if *source* points to a local file."""
    return tg_resolve.is_local(source)


def _local_path(source: str) -> str:
    """Strip the local:// prefix and return the filesystem path."""
    return tg_resolve.local_path(source)


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
    client = await tg_resolve.get_client()
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
        client = await tg_resolve.get_client()
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


# ── Internal: kick off background remux right after upload ─────────
@app.post("/internal/preprocess/{file_id}")
async def preprocess(file_id: str, token: str = Query(default="")) -> JSONResponse:
    """Called by bot.py immediately after a file is received, so the
    fast-start fix has a head start and is hopefully already cached by
    the time the user taps the link. Non-fatal if this is never called
    or fails — stream_video() also self-heals by starting a remux job
    on first request if no cached copy exists yet."""
    if config.API_SECRET_TOKEN and token != config.API_SECRET_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing token.")

    if not config.ENABLE_REMUX:
        return JSONResponse({"status": "disabled"})

    source, _ = await _resolve_telegram_url(file_id)
    if not remux.is_eligible(source):
        return JSONResponse({"status": "not_eligible"})

    asyncio.create_task(remux.ensure_remuxed(file_id, source))
    return JSONResponse({"status": "started", "file_id": file_id})


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
        client = await tg_resolve.get_client()
        head = await client.head(source, follow_redirects=True)
        cl = head.headers.get("content-length")
        if cl:
            file_size = int(cl)

    content_type = _guess_content_type(source)

    # ── Fast-start remux ─────────────────────────────────────────────
    # If a cleaned (moov-relocated) copy already exists, switch onto it —
    # it flows through the exact same local-file Range-serving path below,
    # just decodes smoothly instead of glitching. Otherwise kick off a
    # background job so it's ready next time, and serve the original for
    # now so playback isn't blocked on the remux finishing.
    if config.ENABLE_REMUX and remux.is_eligible(local_file_path if is_local_file else source):
        cached = remux.get_cached_path(file_id)
        if cached is not None:
            is_local_file = True
            local_file_path = str(cached)
            file_size = cached.stat().st_size
            content_type = "video/mp4"
        else:
            asyncio.create_task(remux.ensure_remuxed(file_id, source))

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
            client = await tg_resolve.get_client()
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
    remux_files = list(remux.CACHE_DIR.glob("*.mp4")) if config.ENABLE_REMUX else []
    return {
        "status": "ok",
        "version": "2.0.0",
        "cache_entries": len(tg_resolve._url_cache),
        "remux_enabled": config.ENABLE_REMUX,
        "remux_cached_files": len(remux_files),
        "remux_cache_bytes": sum(f.stat().st_size for f in remux_files),
    }
