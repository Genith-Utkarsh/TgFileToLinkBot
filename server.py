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
import hls
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
    if config.ENABLE_HLS:
        logger.info(
            "HLS packaging enabled — cache dir: %s, segment length: %ds",
            config.HLS_CACHE_DIR, config.HLS_SEGMENT_SECONDS,
        )
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
        "Content-Disposition",
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


def _content_disposition(disposition_type: str, filename: str) -> str:
    """Build a Content-Disposition header value that works for both old
    clients (ASCII filename) and modern ones (full UTF-8 via RFC 5987),
    with quotes/control characters stripped so the header can't break."""
    from urllib.parse import quote as _urlquote

    cleaned = "".join(ch for ch in filename if ch not in '"\r\n') or "file"
    ascii_fallback = cleaned.encode("ascii", "ignore").decode("ascii") or "file"
    encoded = _urlquote(cleaned)
    return f'{disposition_type}; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded}'


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
async def player_redirect(
    file_id: str,
    token: str = Query(default=""),
    mode: str = Query(default="auto"),
):
    """Redirect to the web player with a stream URL pre-filled.

    mode=auto (default) — prefer the HLS manifest when this file is
    HLS-eligible and HLS is enabled; mode=direct always uses the raw
    proxy link instead."""
    from urllib.parse import quote

    target_url = f"/stream/{file_id}?token={quote(token, safe='')}"
    if mode != "direct" and config.ENABLE_HLS:
        try:
            source, _ = await _resolve_telegram_url(file_id)
            if hls.is_eligible(source):
                target_url = f"/hls/{file_id}/{hls.PLAYLIST_NAME}?token={quote(token, safe='')}"
        except HTTPException:
            pass

    return Response(
        status_code=307,
        headers={"Location": f"/?url={quote(target_url, safe='')}"},
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
        "hls_url": f"/hls/{file_id}/{hls.PLAYLIST_NAME}?token={token}" if config.ENABLE_HLS else None,
    })


# ── Internal: kick off background remux + HLS packaging after upload ─
@app.post("/internal/preprocess/{file_id}")
async def preprocess(file_id: str, token: str = Query(default="")) -> JSONResponse:
    """Called by bot.py immediately after a file is received, so both
    fixes have a head start and are hopefully already cached by the
    time the user taps the link. Non-fatal if this is never called or
    fails — stream_video() and the HLS endpoints below both self-heal
    by starting their own job on first request if nothing is cached yet."""
    if config.API_SECRET_TOKEN and token != config.API_SECRET_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing token.")

    source, _ = await _resolve_telegram_url(file_id)
    started = []

    if config.ENABLE_REMUX and remux.is_eligible(source):
        asyncio.create_task(remux.ensure_remuxed(file_id, source))
        started.append("remux")

    if config.ENABLE_HLS and hls.is_eligible(source):
        asyncio.create_task(hls.ensure_packaged(file_id, source))
        started.append("hls")

    if not started:
        return JSONResponse({"status": "not_eligible"})
    return JSONResponse({"status": "started", "jobs": started, "file_id": file_id})


# ── HLS: playlist + segment delivery ────────────────────────────────
@app.get("/hls/{file_id}/" + hls.PLAYLIST_NAME)
async def hls_playlist(file_id: str, token: str = Query(default="")) -> Response:
    """Serve the cached VOD playlist, rewriting each segment line to
    carry the auth token as a query param — players resolve those URIs
    relative to this manifest's own URL, so the token has to live in
    the manifest itself rather than relying on the player to add it."""
    if config.API_SECRET_TOKEN and token != config.API_SECRET_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing token.")

    if not config.ENABLE_HLS:
        raise HTTPException(status_code=404, detail="HLS is disabled on this server.")

    playlist = hls.get_playlist_path(file_id)
    if playlist is None:
        source, _ = await _resolve_telegram_url(file_id)
        if not hls.is_eligible(source):
            raise HTTPException(status_code=404, detail="This file type can't be packaged as HLS.")
        asyncio.create_task(hls.ensure_packaged(file_id, source))
        return JSONResponse(
            status_code=202,
            content={
                "status": hls.status(file_id),
                "detail": "HLS package is being prepared. Retry in a few seconds.",
            },
        )

    raw = playlist.read_text(errors="ignore")
    lines = []
    for line in raw.splitlines():
        if line and not line.startswith("#"):
            sep = "&" if "?" in line else "?"
            line = f"{line}{sep}token={token}"
        lines.append(line)
    body = "\n".join(lines) + "\n"
    return Response(
        content=body,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/hls/{file_id}/{segment_name}")
async def hls_segment(file_id: str, segment_name: str, token: str = Query(default="")) -> FileResponse:
    """Serve a single .ts segment from the cache. segment_name is
    strictly validated by hls.get_segment_path() against path traversal
    before ever touching the filesystem."""
    if config.API_SECRET_TOKEN and token != config.API_SECRET_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing token.")
    if not config.ENABLE_HLS:
        raise HTTPException(status_code=404, detail="HLS is disabled on this server.")
    if not segment_name.endswith(".ts"):
        raise HTTPException(status_code=404, detail="Not found.")

    path = hls.get_segment_path(file_id, segment_name)
    if path is None:
        raise HTTPException(status_code=404, detail="Segment not found or no longer cached.")

    return FileResponse(
        path,
        media_type="video/mp2t",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


# ── Main streaming endpoint ─────────────────────────────────────────
# Registered as TWO routes pointing at the same implementation:
#   /stream/{file_id}/{filename}  — preferred: the filename in the path
#                                   gives browsers, Telegram's in-app
#                                   browser, and players like video.js a
#                                   real extension to key off of, instead
#                                   of guessing from a bare, extension-less
#                                   URL (which is what made links download
#                                   instead of play, and made video.js fail
#                                   to pick a playback tech at all).
#   /stream/{file_id}             — kept for backward compatibility with
#                                   already-shared links; behaves the same,
#                                   just without the filename hint.
@app.api_route("/stream/{file_id}/{filename}", methods=["GET", "HEAD"], response_model=None)
async def stream_video_named(
    file_id: str,
    filename: str,
    request: Request,
    token: str = Query(default=""),
    download: bool = Query(default=False),
) -> StreamingResponse | Response:
    return await _stream_impl(file_id, request, token, download, filename)


@app.api_route("/stream/{file_id}", methods=["GET", "HEAD"], response_model=None)
async def stream_video(
    file_id: str,
    request: Request,
    token: str = Query(default=""),
    download: bool = Query(default=False),
) -> StreamingResponse | Response:
    return await _stream_impl(file_id, request, token, download, None)


async def _stream_impl(
    file_id: str,
    request: Request,
    token: str,
    download: bool,
    filename: str | None,
) -> StreamingResponse | Response:
    """Stream a Telegram-hosted file to the browser with full Range
    request support for seeking / scrubbing.

    download=False (default): Content-Disposition: inline — plays in
    browser / embeds in <video>.
    download=True: Content-Disposition: attachment — forces a Save As
    dialog instead of trying to play it."""

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
    # The real original filename (when given via the path) is more
    # trustworthy than Telegram's internal file_path, which is sometimes
    # generic — prefer it for content-type guessing when it has a
    # recognizable extension.
    if filename:
        named_type = _guess_content_type(filename)
        if named_type != "application/octet-stream":
            content_type = named_type

    disposition_name = filename or os.path.basename(local_file_path or source.split("?", 1)[0]) or file_id
    disposition = _content_disposition("attachment" if download else "inline", disposition_name)

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
            if not filename:
                content_type = "video/mp4"
        else:
            asyncio.create_task(remux.ensure_remuxed(file_id, source))

    # ── HEAD request: return metadata only ──────────────────────────
    if request.method == "HEAD":
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": content_type,
            "Content-Disposition": disposition,
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
            "Content-Disposition": disposition,
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
        "Content-Disposition": disposition,
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
    hls_dirs = [d for d in hls.CACHE_DIR.iterdir() if d.is_dir() and not d.name.startswith(".")] if config.ENABLE_HLS else []
    return {
        "status": "ok",
        "version": "2.0.0",
        "cache_entries": len(tg_resolve._url_cache),
        "remux_enabled": config.ENABLE_REMUX,
        "remux_cached_files": len(remux_files),
        "remux_cache_bytes": sum(f.stat().st_size for f in remux_files),
        "hls_enabled": config.ENABLE_HLS,
        "hls_cached_packages": len(hls_dirs),
        "hls_cache_bytes": sum(hls._dir_size(d) for d in hls_dirs),
    }
