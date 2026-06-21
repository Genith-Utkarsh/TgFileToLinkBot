"""
Background HLS (HTTP Live Streaming) packaging.

Converts a Telegram-hosted file into a real VOD HLS package — an
.m3u8 playlist plus a set of .ts segments — using ffmpeg. Video is
stream-copied (no re-encode, so it's fast and cheap on CPU); audio is
re-encoded to AAC, which is the one re-encode worth always doing since
it guarantees the result is playable inside an MPEG-TS container
regardless of the source's original audio codec, at negligible CPU
cost compared to video.

Unlike a raw progressive MP4 (see remux.py for that problem), MPEG-TS
segments are self-contained — there's no front/back metadata position
to get wrong — so this sidesteps the flicker issue entirely rather than
just patching around it, and adds proper segment-level seeking and
adaptive playback behavior for free via the player (hls.js / native
Safari).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

import config

logger = logging.getLogger(__name__)

CACHE_DIR = Path(config.HLS_CACHE_DIR)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

PLAYLIST_NAME = "playlist.m3u8"

_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".m4v", ".webm", ".avi", ".flv", ".wmv"}
_AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".ogg", ".aac", ".m4a", ".opus"}

_jobs: dict[str, asyncio.Task] = {}


def _dir_for(file_id: str) -> Path:
    return CACHE_DIR / file_id


def _ext_of(source_or_filename: str) -> str:
    return Path(source_or_filename.split("?", 1)[0]).suffix.lower()


def is_eligible(source_or_filename: str) -> bool:
    """Both video and audio files can be packaged as HLS; photos/docs can't."""
    ext = _ext_of(source_or_filename)
    return ext in _VIDEO_EXTENSIONS or ext in _AUDIO_EXTENSIONS


def _is_audio_only(source_or_filename: str, media_type: str | None) -> bool:
    if media_type is not None:
        return media_type == "audio"
    return _ext_of(source_or_filename) in _AUDIO_EXTENSIONS


def get_playlist_path(file_id: str) -> Path | None:
    """Return the cached playlist only once it's actually COMPLETE
    (ffmpeg writes #EXT-X-ENDLIST only after the very last segment is
    finalized) — never hand a player a manifest that's still growing."""
    p = _dir_for(file_id) / PLAYLIST_NAME
    if not p.is_file():
        return None
    try:
        if "#EXT-X-ENDLIST" in p.read_text(errors="ignore"):
            try:
                os.utime(_dir_for(file_id), None)
            except OSError:
                pass
            return p
    except OSError:
        pass
    return None


def get_segment_path(file_id: str, segment_name: str) -> Path | None:
    """Resolve a segment filename strictly inside file_id's own cache
    directory. Rejects anything with a path separator or '..' so a
    crafted segment_name can never escape CACHE_DIR."""
    if not segment_name or "/" in segment_name or "\\" in segment_name or ".." in segment_name:
        return None
    base = _dir_for(file_id).resolve()
    candidate = (base / segment_name).resolve()
    if candidate.parent != base:
        return None
    if not candidate.is_file():
        return None
    return candidate


def status(file_id: str) -> str:
    if get_playlist_path(file_id) is not None:
        return "ready"
    job = _jobs.get(file_id)
    if job and not job.done():
        return "processing"
    return "idle"


async def ensure_packaged(file_id: str, source: str, media_type: str | None = None) -> None:
    """Start a background HLS packaging job for *file_id* unless one is
    already running or a complete package already exists."""
    if get_playlist_path(file_id) is not None:
        return
    existing = _jobs.get(file_id)
    if existing and not existing.done():
        return
    _jobs[file_id] = asyncio.create_task(_run_package(file_id, source, media_type))


async def _run_package(file_id: str, source: str, media_type: str | None) -> None:
    out_dir = _dir_for(file_id)
    tmp_dir = CACHE_DIR / f".{file_id}.tmp"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    _evict_if_needed()

    # "local://" is tg_resolve.py's internal marker for a Local Bot API
    # filesystem path — ffmpeg has no idea what that scheme is.
    ffmpeg_input = source[len("local://"):] if source.startswith("local://") else source
    playlist_path = tmp_dir / PLAYLIST_NAME
    segment_pattern = str(tmp_dir / "seg_%05d.ts")

    audio_only = _is_audio_only(source, media_type)
    cmd = ["ffmpeg", "-y", "-i", ffmpeg_input]
    if audio_only:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "160k"]
    else:
        cmd += [
            "-map", "0:v:0", "-c:v", "copy",
            "-map", "0:a:0?", "-c:a", "aac", "-b:a", "160k",
        ]
    cmd += [
        "-f", "hls",
        "-hls_time", str(config.HLS_SEGMENT_SECONDS),
        "-hls_playlist_type", "vod",
        "-hls_flags", "independent_segments",
        "-hls_segment_filename", segment_pattern,
        str(playlist_path),
    ]

    logger.info("Packaging %s into HLS…", file_id)
    started = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not playlist_path.exists():
            logger.warning(
                "ffmpeg HLS packaging failed for %s (exit %s): %s",
                file_id, proc.returncode, stderr.decode(errors="ignore")[-2000:],
            )
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return

        shutil.rmtree(out_dir, ignore_errors=True)
        os.replace(tmp_dir, out_dir)
        n_segments = len(list(out_dir.glob("*.ts")))
        logger.info(
            "HLS packaging finished for %s in %.1fs (%d segments)",
            file_id, time.time() - started, n_segments,
        )
    except FileNotFoundError:
        logger.error(
            "ffmpeg binary not found on PATH — HLS playback unavailable until "
            "it's installed (see deployment notes)."
        )
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception as exc:
        logger.error("Unexpected HLS packaging error for %s: %s", file_id, exc)
        shutil.rmtree(tmp_dir, ignore_errors=True)
    finally:
        _jobs.pop(file_id, None)


def _dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _evict_if_needed() -> None:
    """LRU eviction by directory mtime, whole-package at a time, keeping
    total cache under config.HLS_CACHE_MAX_BYTES."""
    try:
        dirs = [d for d in CACHE_DIR.iterdir() if d.is_dir() and not d.name.startswith(".")]
    except OSError:
        return
    dirs.sort(key=lambda d: d.stat().st_mtime)
    total = sum(_dir_size(d) for d in dirs)
    limit = config.HLS_CACHE_MAX_BYTES
    i = 0
    while total > limit and i < len(dirs):
        d = dirs[i]
        try:
            size = _dir_size(d)
            shutil.rmtree(d)
            total -= size
            logger.info("Evicted cached HLS package %s to stay under cache limit.", d.name)
        except OSError:
            pass
        i += 1
