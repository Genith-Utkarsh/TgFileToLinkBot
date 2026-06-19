FROM aiogram/telegram-bot-api:latest

USER root

# Install Python, pip, and supervisord
RUN apk add --no-cache python3 py3-pip curl supervisor

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir -r requirements.txt --break-system-packages

# Copy the rest of the application
COPY . .

# Create data directory for telegram-bot-api
RUN mkdir -p /app/telegram-bot-api-data && chmod 777 /app/telegram-bot-api-data

# Set environment variables for the Local Bot API server
ENV TELEGRAM_API_URL="http://127.0.0.1:8081"
ENV PORT=8000
ENV HOST=0.0.0.0

EXPOSE 8000
EXPOSE 8081

# Use supervisord to run both processes
CMD ["supervisord", "-c", "/app/supervisord.conf"]
