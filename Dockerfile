FROM aiogram/telegram-bot-api:latest

USER root

# Install Python, pip, supervisord, and ffmpeg.
# This base image is Alpine-based (note `apk`, not `apt-get`) — ffmpeg
# is installed here, not via a separate apt-get step further down,
# because apt-get doesn't exist on Alpine and that step would just
# fail the build outright.
RUN apk add --no-cache python3 py3-pip curl supervisor ffmpeg

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir -r requirements.txt --break-system-packages

# Copy the rest of the application
COPY . .

# Create data directory for telegram-bot-api
RUN mkdir -p /app/telegram-bot-api-data && chmod 777 /app/telegram-bot-api-data

# ── Local Bot API server credentials ────────────────────────────────
# api_id / api_hash come from https://my.telegram.org/apps. They're
# tied to a personal Telegram account rather than to this bot, but
# they still shouldn't sit in a Dockerfile that might land in a public
# repo (HF Spaces repos can be public). Prefer setting
# TELEGRAM_API_ID / TELEGRAM_API_HASH as Variables/Secrets in your
# Space's settings — those override the ENV defaults below at runtime.
# Treat the values currently baked in here as exposed and rotate them
# if this Space (or its repo) is or becomes public.
ENV TELEGRAM_API_URL="http://127.0.0.1:8081"
ENV TELEGRAM_API_ID="6"
ENV TELEGRAM_API_HASH="eb06d4abfb49dc3eeb1aeb98ae0f581e"
ENV PORT=8000
ENV HOST=0.0.0.0

EXPOSE 8000
EXPOSE 8081

# Basic container healthcheck against the FastAPI app's /health route.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# Clear the base image entrypoint so supervisord can run
ENTRYPOINT []

# Use supervisord to run both processes
CMD ["supervisord", "-c", "/app/supervisord.conf"]
