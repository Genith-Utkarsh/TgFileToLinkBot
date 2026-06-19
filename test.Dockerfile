FROM aiogram/telegram-bot-api:latest AS bot-api
FROM python:3.11-alpine
COPY --from=bot-api /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api
RUN ldd /usr/local/bin/telegram-bot-api || true
CMD ["telegram-bot-api", "--help"]
