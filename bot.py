"""
Telegram bot handler — listens for media uploads from the
authorised user and replies with ready-to-use stream / player URLs.

Supports: videos, audio, photos, and documents with video/audio MIME types.
"""

from __future__ import annotations

import logging
import time
from mimetypes import guess_type
from urllib.parse import quote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config

logger = logging.getLogger(__name__)

# ── Runtime statistics (in-memory) ──────────────────────────────────
_start_time: float = time.time()
_files_served: int = 0

# ── Supported extensions ────────────────────────────────────────────
_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".wmv"}
_AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".ogg", ".aac", ".m4a", ".opus"}
_PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

# ── Media type → emoji mapping ──────────────────────────────────────
_TYPE_EMOJI = {
    "video": "🎬",
    "audio": "🎵",
    "photo": "🖼️",
    "document": "📄",
}


# ── Helpers ─────────────────────────────────────────────────────────
def _is_allowed(user_id: int) -> bool:
    """Return True if the message sender is the authorised user.
    If ALLOWED_USER_ID is 0, allow everyone (useful for initial setup)."""
    return config.ALLOWED_USER_ID == 0 or user_id == config.ALLOWED_USER_ID


def _build_stream_url(file_id: str) -> str:
    """Construct the raw streaming URL for a given Telegram file_id."""
    return f"{config.BASE_URL}/stream/{file_id}?token={config.API_SECRET_TOKEN}"


def _build_player_url(file_id: str) -> str:
    """Construct a browser player URL that auto-loads the stream."""
    stream = _build_stream_url(file_id)
    return f"{config.BASE_URL}/?url={quote(stream, safe='')}"


def _format_size(size_bytes: int | None) -> str:
    """Return a human-readable file size string."""
    if not size_bytes:
        return "unknown"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}" if unit != "B" else f"{size_bytes} {unit}"
        size_bytes /= 1024  # type: ignore[assignment]
    return f"{size_bytes:.1f} PB"


def _format_uptime(seconds: float) -> str:
    """Return a human-readable uptime string."""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def _detect_file_ext(filename: str) -> str:
    """Extract lowercase extension from a filename."""
    return ("." + filename.rsplit(".", 1)[-1]).lower() if "." in filename else ""


# ── /start command ──────────────────────────────────────────────────
async def _start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _is_allowed(update.effective_user.id):
        return
    text = (
        "👋 <b>Welcome to Telegram Stream Bot!</b>\n\n"
        "Send me a media file and I'll give you a direct stream link "
        "you can embed anywhere.\n\n"
        "📁 <b>Supported formats:</b>\n"
        "  🎬 Videos — .mp4 .mkv .webm .mov .avi\n"
        "  🎵 Audio — .mp3 .flac .wav .ogg .aac\n"
        "  🖼️ Photos — .jpg .png .gif .webp\n\n"
        "⚡ Just send a file to get started!\n\n"
        "<b>Commands:</b>\n"
        "/start — This message\n"
        "/help  — Usage guide\n"
        "/about — Bot information\n"
        "/stats — Statistics (admin)"
    )
    await update.message.reply_text(text, parse_mode="HTML")  # type: ignore[union-attr]


# ── /help command ───────────────────────────────────────────────────
async def _help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _is_allowed(update.effective_user.id):
        return
    text = (
        "📖 <b>How to use this bot:</b>\n\n"
        "1️⃣  Send me any video, audio, or photo file\n"
        "2️⃣  I'll give you a <b>stream URL</b> and a <b>player link</b>\n"
        "3️⃣  Share the link or embed it in your website\n\n"
        "💡 <b>Tips:</b>\n"
        "• Stream URLs work directly in <code>&lt;video&gt;</code> / "
        "<code>&lt;audio&gt;</code> tags\n"
        "• The player link opens a built-in web player with Plyr.js\n"
        "• Full seeking / scrubbing is supported via HTTP Range requests\n"
        "• Bot API file size limit is 20 MB (50 MB with Telegram Premium)"
    )
    await update.message.reply_text(text, parse_mode="HTML")  # type: ignore[union-attr]


# ── /about command ──────────────────────────────────────────────────
async def _about(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _is_allowed(update.effective_user.id):
        return
    text = (
        "📡 <b>Telegram Stream Bot</b> v2.0\n\n"
        "A zero-storage video/audio streaming proxy.\n"
        "Files are streamed directly from Telegram servers — "
        "no disk cache, no bandwidth waste.\n\n"
        "🛠 <b>Stack:</b> Python • FastAPI • python-telegram-bot\n"
        "🌐 <b>Player:</b> Plyr.js with range-request seeking\n"
        "🔒 <b>Auth:</b> Token-based stream URL protection"
    )
    await update.message.reply_text(text, parse_mode="HTML")  # type: ignore[union-attr]


# ── /stats command (admin only) ─────────────────────────────────────
async def _stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    # Only allow the configured admin (or if ALLOWED_USER_ID is 0, anyone)
    if config.ALLOWED_USER_ID != 0 and user.id != config.ALLOWED_USER_ID:
        await update.message.reply_text(  # type: ignore[union-attr]
            "⛔ This command is restricted to the bot admin."
        )
        return
    uptime = time.time() - _start_time
    text = (
        "📊 <b>Bot Statistics</b>\n\n"
        f"⏱ <b>Uptime:</b> {_format_uptime(uptime)}\n"
        f"📁 <b>Files served:</b> {_files_served}\n"
        f"🌐 <b>Base URL:</b> <code>{config.BASE_URL}</code>\n"
        f"🔑 <b>Auth:</b> {'Token protected' if config.API_SECRET_TOKEN != 'changeme' else '⚠️ Default token!'}"
    )
    await update.message.reply_text(text, parse_mode="HTML")  # type: ignore[union-attr]


# ── Media handler ──────────────────────────────────────────────────
async def _handle_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming video, audio, photo, or document messages."""
    global _files_served

    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return
    if not _is_allowed(user.id):
        logger.warning("Rejected upload from unauthorised user %s", user.id)
        await msg.reply_text("⛔ You are not authorised to use this bot.")
        return

    # Determine the file object and media type
    tg_file_obj = None
    filename: str = "file"
    media_type: str = "document"

    if msg.video:
        tg_file_obj = msg.video
        filename = msg.video.file_name or f"video_{msg.video.file_unique_id}.mp4"
        media_type = "video"
    elif msg.audio:
        tg_file_obj = msg.audio
        filename = msg.audio.file_name or f"audio_{msg.audio.file_unique_id}.mp3"
        media_type = "audio"
    elif msg.voice:
        tg_file_obj = msg.voice
        filename = f"voice_{msg.voice.file_unique_id}.ogg"
        media_type = "audio"
    elif msg.video_note:
        tg_file_obj = msg.video_note
        filename = f"videonote_{msg.video_note.file_unique_id}.mp4"
        media_type = "video"
    elif msg.photo:
        tg_file_obj = msg.photo[-1]  # highest resolution
        filename = f"photo_{tg_file_obj.file_unique_id}.jpg"
        media_type = "photo"
    elif msg.document:
        doc = msg.document
        mime = doc.mime_type or ""
        fname = doc.file_name or ""
        ext = _detect_file_ext(fname)

        if mime.startswith("video/") or ext in _VIDEO_EXTENSIONS:
            media_type = "video"
        elif mime.startswith("audio/") or ext in _AUDIO_EXTENSIONS:
            media_type = "audio"
        elif mime.startswith("image/") or ext in _PHOTO_EXTENSIONS:
            media_type = "photo"
        else:
            media_type = "document"

        tg_file_obj = doc
        filename = fname or f"file_{doc.file_unique_id}"
    else:
        await msg.reply_text("❌ Please send a media file (video, audio, or photo).")
        return

    # Fetch the Telegram file metadata
    try:
        tg_file = await ctx.bot.get_file(tg_file_obj.file_id)
    except Exception as exc:
        logger.error("Failed to get file from Telegram: %s", exc)
        await msg.reply_text(
            "⚠️ Telegram refused the file — it may exceed the size limit "
            "(20 MB for bots, 50 MB with Premium)."
        )
        return

    file_id = tg_file_obj.file_id
    stream_url = _build_stream_url(file_id)
    player_url = _build_player_url(file_id)
    size_str = _format_size(tg_file.file_size)
    emoji = _TYPE_EMOJI.get(media_type, "📄")

    # Guess MIME for the informational reply
    mime_guess = guess_type(filename)[0] or "application/octet-stream"

    reply = (
        f"✅ <b>{emoji} Media ready for streaming!</b>\n\n"
        f"📄 <b>File:</b> <code>{filename}</code>\n"
        f"🎞 <b>MIME:</b> <code>{mime_guess}</code>\n"
        f"📦 <b>Size:</b> {size_str}\n"
        f"🏷 <b>Type:</b> {media_type.capitalize()}\n\n"
        f"🔗 <b>Stream URL:</b>\n<code>{stream_url}</code>\n\n"
        f"📋 <b>Embed:</b>\n"
    )

    if media_type == "video":
        reply += f'<code>&lt;video src="{stream_url}" controls&gt;&lt;/video&gt;</code>'
    elif media_type == "audio":
        reply += f'<code>&lt;audio src="{stream_url}" controls&gt;&lt;/audio&gt;</code>'
    elif media_type == "photo":
        reply += f'<code>&lt;img src="{stream_url}" /&gt;</code>'

    # Inline keyboard with buttons
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔗 Stream Direct", url=stream_url),
                InlineKeyboardButton("▶️ Play in Browser", url=player_url),
            ]
        ]
    )

    await msg.reply_text(reply, parse_mode="HTML", reply_markup=keyboard)
    _files_served += 1
    logger.info(
        "Served stream link for file_id=%s (%s, %s, %s)",
        file_id, filename, media_type, size_str,
    )


# ── Global error handler ───────────────────────────────────────────
async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log unhandled exceptions from handlers."""
    logger.error("Unhandled exception: %s", context.error, exc_info=context.error)


# ── Build the Application object (used by main.py) ─────────────────
def build_application():
    """Create and return the configured telegram Application instance."""
    app = (
        ApplicationBuilder()
        .token(config.BOT_TOKEN)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start", _start))
    app.add_handler(CommandHandler("help", _help))
    app.add_handler(CommandHandler("about", _about))
    app.add_handler(CommandHandler("stats", _stats))

    # Media handler — accept all common media types
    app.add_handler(
        MessageHandler(
            filters.VIDEO
            | filters.AUDIO
            | filters.VOICE
            | filters.VIDEO_NOTE
            | filters.PHOTO
            | filters.Document.ALL,
            _handle_media,
        )
    )

    # Error handler
    app.add_error_handler(_error_handler)

    return app
