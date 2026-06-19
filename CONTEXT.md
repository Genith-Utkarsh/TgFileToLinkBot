# Telegram Stream Proxy v2.0

## Overview

A zero-disk-cache Telegram-to-Web media streaming proxy. Users upload media files
(video, audio, photos) to a private Telegram bot and instantly receive direct HTTP
stream URLs that can be embedded in any website via `<video>`, `<audio>`, or `<img>` tags.

The server acts as a transparent byte-passthrough reverse proxy from Telegram's
file servers to the end user's browser — no local disk caching, no bandwidth waste.

## Architecture

```
  User                Telegram Bot               FastAPI Server
  ────                ────────────               ──────────────
    │                      │                           │
    ├── sends media ──────►│                           │
    │                      ├── getFile API ───────────►│ (resolves file_id)
    │◄── stream URL ───────┤                           │
    │                      │                           │
    │  (later, from browser)                           │
    ├── GET /stream/{id}?token=... ───────────────────►│
    │                      │     ┌── Telegram CDN ─────┤ (proxy bytes)
    │◄── video bytes ──────│─────┘                     │
    │  (with Range/206)    │                           │
```

## Components

| File | Purpose |
|------|---------|
| `main.py` | Entry point — runs bot + server concurrently via asyncio |
| `bot.py` | Telegram bot: /start, /help, /about, /stats, media handler |
| `server.py` | FastAPI: streaming proxy, web player, rate limiting, caching |
| `config.py` | Environment variable configuration with validation |
| `index.html` | Plyr.js-powered web player (static, served at `/`) |

## Tech Stack

- **Python 3.11+**, asyncio
- **FastAPI** + **Uvicorn** — ASGI web server
- **python-telegram-bot** v21+ — async Telegram Bot API
- **httpx** — async HTTP client (connection pooling, streaming)
- **Plyr.js** — premium media player in the browser
- **Docker** — containerised deployment

## Key Features

- **Zero-disk-cache**: No files stored on server; pure byte passthrough
- **HTTP Range requests**: Full `206 Partial Content` support for seeking/scrubbing
- **Token-based auth**: Stream URLs include a secret token query parameter
- **CORS enabled**: `<video>` embeds work from any origin
- **Built-in web player**: Plyr.js with custom dark theme at `/`
- **Rate limiting**: In-memory per-IP rate limiter (120 req/min)
- **URL resolution cache**: 5-minute TTL to avoid redundant Telegram API calls
- **Multi-format**: Video, audio, photos, voice notes, video notes
- **Deploy-ready**: Dockerfile, docker-compose, Procfile, Railway/Heroku support

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Web player |
| GET/HEAD | `/stream/{file_id}?token=` | Stream/download media |
| GET | `/play/{file_id}?token=` | Redirect to player with URL |
| GET | `/api/info/{file_id}?token=` | File metadata (JSON) |
| GET | `/health` | Health check |

## Environment Variables

See [.env.example](.env.example) and [setup.md](setup.md) for full reference.

### Required
- `BOT_TOKEN` — Telegram Bot API token
- `API_SECRET_TOKEN` — shared secret for stream URLs
- `BASE_URL` — public-facing root URL

### Optional
- `ALLOWED_USER_ID` — restrict uploads to one user (default: 0 = all)
- `HOST`, `PORT` — server bind config
- `CHUNK_SIZE` — proxy chunk size
- `MAX_CONNECTIONS` — httpx pool limit
- `LOG_LEVEL` — Python logging level
