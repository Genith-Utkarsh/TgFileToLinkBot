# 📡 Telegram Stream Proxy — Setup Guide

Complete guide to set up, run, and deploy the Telegram Stream Proxy bot.

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [1. Create a Telegram Bot](#1-create-a-telegram-bot)
- [2. Get Your Telegram User ID](#2-get-your-telegram-user-id)
- [3. Clone & Configure](#3-clone--configure)
- [4. Run Locally](#4-run-locally)
- [5. Deploy with Docker](#5-deploy-with-docker)
- [6. Deploy to Railway](#6-deploy-to-railway)
- [7. Deploy to Heroku](#7-deploy-to-heroku)
- [8. Deploy to a VPS](#8-deploy-to-a-vps)
- [9. Environment Variables Reference](#9-environment-variables-reference)
- [10. API Endpoints](#10-api-endpoints)
- [11. Bot Commands](#11-bot-commands)
- [12. Embedding in Websites](#12-embedding-in-websites)
- [13. Troubleshooting](#13-troubleshooting)

---

## Prerequisites

- **Python 3.10+** (3.11 recommended)
- **A Telegram account**
- **pip** (Python package manager)
- **Git** (for cloning)
- **Docker** (optional, for containerized deployment)

---

## 1. Create a Telegram Bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the prompts
3. Choose a name (e.g., `My Stream Bot`) and username (e.g., `my_stream_bot`)
4. **Copy the bot token** — it looks like `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`
5. Keep this token secret!

---

## 2. Get Your Telegram User ID

1. Search for **@userinfobot** on Telegram
2. Send it any message
3. It will reply with your numeric **User ID** (e.g., `123456789`)
4. This is your `ALLOWED_USER_ID`

> **Tip:** Set `ALLOWED_USER_ID=0` to allow all users (not recommended for production).

---

## 3. Clone & Configure

```bash
# Clone the repository
git clone https://github.com/your-username/TgFileToLinkBot.git
cd TgFileToLinkBot

# Create your environment file from the template
cp .env.example .env
```

Edit `.env` with your credentials:

```env
# Required — paste your bot token from @BotFather
BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz

# Your Telegram numeric user ID (0 = allow all users)
ALLOWED_USER_ID=123456789

# Generate a strong random secret (used in stream URLs)
API_SECRET_TOKEN=my-super-secret-token-here

# Your public-facing URL (change for production)
BASE_URL=http://localhost:8000
```

### Generate a Strong API Secret

```bash
# Linux/macOS
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# Or use openssl
openssl rand -base64 32
```

---

## 4. Run Locally

### Option A: Direct Python

```bash
# Create a virtual environment
python3 -m venv venv
source venv/bin/activate    # Linux/macOS
# venv\Scripts\activate     # Windows

# Install dependencies
pip install -r requirements.txt

# Run the bot + server
python main.py
```

You should see:

```
╔══════════════════════════════════════════════════════════╗
║           Telegram Stream Proxy  v2.0                    ║
║           Zero-cache media streaming                     ║
╚══════════════════════════════════════════════════════════╝

🚀 Starting Telegram Stream Proxy v2.0
   ├── Server:  http://0.0.0.0:8000
   ├── Public:  http://localhost:8000
   ├── Player:  http://localhost:8000/
   ├── Health:  http://localhost:8000/health
   └── Log level: INFO
🤖 Telegram bot is polling …
```

### Option B: Docker

```bash
# Build and run
docker compose up --build

# Or run in background
docker compose up -d --build
```

### Verify it's working

- Open http://localhost:8000 — you should see the web player
- Open http://localhost:8000/health — should return `{"status": "ok"}`
- Send a video to your bot on Telegram — it should reply with stream links

---

## 5. Deploy with Docker

### Build the image

```bash
docker build -t tg-stream-proxy .
```

### Run the container

```bash
docker run -d \
  --name tg-stream-proxy \
  --env-file .env \
  -p 8000:8000 \
  --restart unless-stopped \
  tg-stream-proxy
```

### Using Docker Compose

```bash
# Start
docker compose up -d

# View logs
docker compose logs -f

# Stop
docker compose down

# Rebuild after code changes
docker compose up -d --build
```

---

## 6. Deploy to Railway

[Railway](https://railway.app) offers free-tier hosting with automatic deployments.

1. **Push your code to GitHub** (ensure `.env` is in `.gitignore`)
2. Go to [railway.app](https://railway.app) and create a new project
3. Select **"Deploy from GitHub repo"**
4. Add environment variables in the Railway dashboard:
   - `BOT_TOKEN`
   - `ALLOWED_USER_ID`
   - `API_SECRET_TOKEN`
   - `BASE_URL` → set to your Railway public URL (e.g., `https://your-app.up.railway.app`)
   - `PORT` → Railway sets this automatically
5. Railway auto-detects the `Procfile` and `runtime.txt`
6. Deploy!

> **Important:** Set `BASE_URL` to your Railway domain so the bot generates correct stream URLs.

---

## 7. Deploy to Koyeb (100% Free)

[Koyeb](https://www.koyeb.com/) offers a generous free tier (Eco instance) that runs Docker containers perfectly.

1. Push your code to GitHub.
2. Sign up on Koyeb and click **Create App**.
3. Choose **GitHub** and select your repository.
4. Koyeb will automatically detect the Dockerfile.
5. In the **Environment Variables** section, add `BOT_TOKEN`, `API_SECRET_TOKEN`, `BASE_URL`, and `ALLOWED_USER_ID`.
6. Click **Deploy**. Koyeb will give you a free `.koyeb.app` domain. Update your `BASE_URL` to match it.

---

## 8. Deploy to Hugging Face Spaces (Free)

Hugging Face Spaces provides free Docker hosting with a massive 16GB of RAM.

1. Go to [Hugging Face Spaces](https://huggingface.co/spaces) and click **Create new Space**.
2. Set the License, choose **Docker** as the SDK, and select **Blank**.
3. Set the Space hardware to **Free**.
4. In your Space settings, go to **Variables and secrets**. Add your environment variables (`BOT_TOKEN`, etc.) as **Secrets**.
5. Upload all the files from this repository to the Space (or clone via Git).
6. The Dockerfile will automatically build and run the proxy.

## 9. Deploy to Heroku

1. Install the [Heroku CLI](https://devcenter.heroku.com/articles/heroku-cli)

```bash
# Login to Heroku
heroku login

# Create a new app
heroku create your-app-name

# Set environment variables
heroku config:set BOT_TOKEN="your-bot-token"
heroku config:set ALLOWED_USER_ID="your-user-id"
heroku config:set API_SECRET_TOKEN="your-secret"
heroku config:set BASE_URL="https://your-app-name.herokuapp.com"

# Deploy
git push heroku main

# Check logs
heroku logs --tail
```

---

## 10. Deploy to a VPS

### Using systemd (Ubuntu/Debian)

```bash
# Clone to server
cd /opt
git clone https://github.com/your-username/TgFileToLinkBot.git
cd TgFileToLinkBot

# Setup Python environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure .env
cp .env.example .env
nano .env   # Edit with your credentials
```

Create a systemd service file:

```bash
sudo nano /etc/systemd/system/tg-stream.service
```

```ini
[Unit]
Description=Telegram Stream Proxy
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/TgFileToLinkBot
EnvironmentFile=/opt/TgFileToLinkBot/.env
ExecStart=/opt/TgFileToLinkBot/venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable tg-stream
sudo systemctl start tg-stream

# Check status
sudo systemctl status tg-stream

# View logs
journalctl -u tg-stream -f
```

### Reverse Proxy with Nginx (optional)

If you want HTTPS with a custom domain:

```nginx
server {
    listen 80;
    server_name stream.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Important for streaming
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
    }
}
```

Then use **Certbot** for free SSL:

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d stream.yourdomain.com
```

---

## 11. Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `BOT_TOKEN` | ✅ | — | Telegram Bot API token from @BotFather |
| `ALLOWED_USER_ID` | ❌ | `0` | Your Telegram user ID. `0` = allow all users |
| `API_SECRET_TOKEN` | ✅ | `changeme` | Secret token appended to stream URLs |
| `BASE_URL` | ✅ | `http://localhost:8000` | Public-facing URL root |
| `HOST` | ❌ | `0.0.0.0` | Server bind address |
| `PORT` | ❌ | `8000` | Server port |
| `CHUNK_SIZE` | ❌ | `524288` | Proxy chunk size in bytes (512 KB) |
| `MAX_CONNECTIONS` | ❌ | `100` | httpx connection pool size |
| `LOG_LEVEL` | ❌ | `INFO` | Logging level: DEBUG, INFO, WARNING, ERROR |
| `TELEGRAM_API_URL`| ❌ | `https://api.telegram.org` | Custom local Bot API server URL for >20MB support |

---

## 12. API Endpoints

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| `GET` | `/` | ❌ | Web player (Plyr.js-powered) |
| `GET` | `/stream/{file_id}?token=...` | ✅ | Stream media with range support |
| `HEAD` | `/stream/{file_id}?token=...` | ✅ | Get file metadata headers |
| `GET` | `/play/{file_id}?token=...` | ❌ | Redirect to player with URL pre-filled |
| `GET` | `/api/info/{file_id}?token=...` | ✅ | JSON file metadata |
| `GET` | `/health` | ❌ | Health check |

### Example: Stream a video

```bash
curl -H "Range: bytes=0-1023" \
  "http://localhost:8000/stream/FILE_ID?token=YOUR_SECRET"
```

### Example: Get file info

```bash
curl "http://localhost:8000/api/info/FILE_ID?token=YOUR_SECRET"
```

Response:
```json
{
  "file_id": "...",
  "file_size": 15728640,
  "content_type": "video/mp4",
  "stream_url": "/stream/FILE_ID?token=YOUR_SECRET"
}
```

---

## 13. Bot Commands

| Command | Description | Access |
|---------|-------------|--------|
| `/start` | Welcome message with supported formats | All users |
| `/help` | Usage guide and tips | All users |
| `/about` | Bot version and tech stack info | All users |
| `/stats` | Uptime, files served count | Admin only |

### Supported Media Types

- **Video:** .mp4, .mkv, .webm, .mov, .avi, .flv, .wmv
- **Audio:** .mp3, .flac, .wav, .ogg, .aac, .m4a, .opus
- **Photos:** .jpg, .png, .gif, .webp, .bmp
- **Voice messages** and **video notes** are also supported

---

## 14. Embedding in Websites

The bot sends you a stream URL that can be used directly in HTML:

### Video embed

```html
<video src="https://your-domain.com/stream/FILE_ID?token=SECRET" controls>
</video>
```

### Audio embed

```html
<audio src="https://your-domain.com/stream/FILE_ID?token=SECRET" controls>
</audio>
```

### Image embed

```html
<img src="https://your-domain.com/stream/FILE_ID?token=SECRET" />
```

### Using the built-in player

Share this URL and it auto-loads in the web player:

```
https://your-domain.com/?url=/stream/FILE_ID?token=SECRET
```

Or use the convenience redirect:

```
https://your-domain.com/play/FILE_ID?token=SECRET
```

---

## 15. Troubleshooting

### Bot not responding

- Verify `BOT_TOKEN` is correct
- Check the bot is running: `curl http://localhost:8000/health`
- Check logs: `docker compose logs -f` or `journalctl -u tg-stream -f`

### "Telegram refused the file" / Files > 20 MB failing

- The public Telegram Bot API (`api.telegram.org`) has a hard **20 MB** download limit for bots via `getFile`.
- **To stream files up to 2 GB:** You must run a [Local Telegram Bot API Server](https://github.com/tdlib/telegram-bot-api) alongside this proxy.
- Once your local bot API is running (e.g., on port 8081), set the `TELEGRAM_API_URL` environment variable:
  ```env
  TELEGRAM_API_URL=http://localhost:8081
  ```
- The proxy will automatically switch to `local_mode` and stream the full 2 GB files.

### Stream URL returns 403

- Your `API_SECRET_TOKEN` in the URL doesn't match the server's config
- Double-check the token in your `.env`

### Video won't seek / scrub

- Ensure CORS headers are working (check browser console)
- The server must return `206 Partial Content` for Range requests
- Test with: `curl -I -H "Range: bytes=0-1" "http://localhost:8000/stream/FILE_ID?token=SECRET"`

### Connection refused on deploy

- Ensure `HOST=0.0.0.0` (not `127.0.0.1`)
- Check your cloud provider's port mapping
- Verify `PORT` matches what your platform expects

### Rate limited (429)

- The server has a built-in rate limiter (120 requests/minute per IP)
- For production with heavy traffic, consider putting a CDN (Cloudflare) in front

---

## Project Structure

```
TgFileToLinkBot/
├── main.py              # Entry point — runs bot + server concurrently
├── bot.py               # Telegram bot handlers & commands
├── server.py            # FastAPI streaming server with middleware
├── config.py            # Environment variable configuration
├── index.html           # Plyr.js web player (static)
├── requirements.txt     # Python dependencies
├── .env.example         # Environment template
├── .env                 # Your local config (git-ignored)
├── Dockerfile           # Docker image definition
├── docker-compose.yml   # Docker Compose orchestration
├── Procfile             # Railway/Heroku process definition
├── runtime.txt          # Python version for PaaS platforms
├── .gitignore           # Git ignore rules
├── .dockerignore        # Docker build context exclusions
├── CONTEXT.md           # Architecture documentation
└── setup.md             # This file
```

---

## License

This project is provided as-is for personal use. Modify and deploy as needed.
