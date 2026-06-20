"""
Background "fast-start" remux helper.

Phone-recorded videos sent through Telegram almost always store the
``moov`` atom (the box containing the keyframe/sample index the browser
needs to decode the stream) at the END of the file instead of the front.
Progressively streaming such a file — exactly what the Range-request
proxy in server.py does — hands the browser raw, un-indexed media data
first, which is what shows up as visual glitching/flashing right from
frame one.

This module fixes that the cheap way: the first time a file is
requested, it kicks off a background ``ffmpeg -c copy -movflags
+faststart`` job (pure container repackage, no re-encoding — fast and
nearly free on CPU) and caches the result on disk. Once cached, the
existing byte-range streaming code in server.py serves the clean file
directly, with full Range/seek support, exactly as before.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import config

logger = logging.getLogger(__name__)

CACHE_DIR = Path(config.REMUX_CACHE_DIR)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Containers that actually suffer from the "moov-at-end" problem in a way
# that a simple stream-copy + faststart fixes. (.mkv/.webm use a different
# index structure and don't have this specific failure mode.)
_REMUX_EXTENSIONS = {".mp4", ".mov", ".m4v"}

_jobs: dict[str, asyncio.Task] = {}


def _cache_path(file_id: str) -> Path:
    return CACHE_DIR / f"{file_id}.mp4"


def is_eligible(source_or_filename: str) -> bool:
    """True if this file's container can suffer from / be fixed by a
    faststart remux. Strips query strings so it works on raw URLs too."""
    ext = Path(source_or_filename.split("?", 1)[0]).suffix.lower()
    return ext in _REMUX_EXTENSIONS


def get_cached_path(file_id: str) -> Path | None:
    """Return the cached, fast-start-fixed copy if one exists and is
    non-empty. Touches mtime so the LRU eviction below treats it as
    recently used."""
    path = _cache_path(file_id)
    if path.exists() and path.stat().st_size > 0:
        try:
            os.utime(path, None)
        except OSError:
            pass
        return path
    return None


def status(file_id: str) -> str:
    if get_cached_path(file_id) is not None:
        return "ready"
    job = _jobs.get(file_id)
    if job and not job.done():
        return "processing"
    return "idle"


async def ensure_remuxed(file_id: str, source: str) -> None:
    """Start a background remux job for *file_id* unless one is already
    running or a cached copy already exists. Safe to call repeatedly /
    concurrently — only ever one job per file_id at a time."""
    if get_cached_path(file_id) is not None:
        return
    existing = _jobs.get(file_id)
    if existing and not existing.done():
        return
    _jobs[file_id] = asyncio.create_task(_run_remux(file_id, source))


async def _run_remux(file_id: str, source: str) -> None:
    final_path = _cache_path(file_id)
    tmp_path = final_path.with_suffix(".mp4.tmp")
    _evict_if_needed()

    # "local://" is this codebase's own internal marker for a Local Bot
    # API filesystem path (see tg_resolve.py) — ffmpeg has no idea what
    # that scheme is, so unwrap it to a real path before invoking ffmpeg.
    ffmpeg_input = source[len("local://"):] if source.startswith("local://") else source

    cmd = [
        "ffmpeg", "-y",
        "-i", ffmpeg_input,
        "-c", "copy",
        "-movflags", "+faststart",
        "-f", "mp4",
        str(tmp_path),
    ]
    logger.info("Remuxing %s for fast-start playback…", file_id)
    started = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(
                "ffmpeg remux failed for %s (exit %s): %s",
                file_id, proc.returncode, stderr.decode(errors="ignore")[-2000:],
            )
            tmp_path.unlink(missing_ok=True)
            return
        os.replace(tmp_path, final_path)
        logger.info(
            "Remux finished for %s in %.1fs (%s)",
            file_id, time.time() - started, _human_size(final_path.stat().st_size),
        )
    except FileNotFoundError:
        logger.error(
            "ffmpeg binary not found on PATH — install it (see deployment notes) "
            "to enable smooth playback. Falling back to direct streaming."
        )
        tmp_path.unlink(missing_ok=True)
    except Exception as exc:
        logger.error("Unexpected remux error for %s: %s", file_id, exc)
        tmp_path.unlink(missing_ok=True)
    finally:
        _jobs.pop(file_id, None)


def _human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _evict_if_needed() -> None:
    """Simple LRU eviction by file mtime, keeping the cache under
    config.REMUX_CACHE_MAX_BYTES so a steady stream of large videos
    can't fill the disk on a small VPS / HF Space."""
    try:
        files = sorted(CACHE_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    total = sum(f.stat().st_size for f in files)
    limit = config.REMUX_CACHE_MAX_BYTES
    i = 0
    while total > limit and i < len(files):
        f = files[i]
        try:
            size = f.stat().st_size
            f.unlink()
            total -= size
            logger.info("Evicted cached remux %s to stay under cache limit.", f.name)
        except OSError:
            pass
        i += 1
