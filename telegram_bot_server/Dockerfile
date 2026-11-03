FROM python:3.12-slim

WORKDIR /app
COPY backend/src /app/backend/src
COPY telegram_bot_server /app/telegram_bot_server
COPY telegram_bot_server/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

ENV PYTHONPATH=/app/backend/src
EXPOSE 8090
CMD ["uvicorn", "telegram_bot_server.server:app", "--host", "0.0.0.0", "--port", "8090"]
