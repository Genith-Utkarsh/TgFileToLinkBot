FROM aiogram/telegram-bot-api:latest
USER root
RUN apk add --no-cache python3 py3-pip curl supervisor
RUN python3 -m pip install fastapi uvicorn httpx python-telegram-bot --break-system-packages
