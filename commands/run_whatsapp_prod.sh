#!/bin/bash
set -e

SERVICE=hotelai-app
PORT=8000
DOMAIN="bookai.predev.roomdoo.com"
WORKERS="${UVICORN_WORKERS:-2}"

export UVICORN_WORKERS="$WORKERS"

echo "🛠️ Rebuilding Docker container for production..."
docker compose down --remove-orphans
docker compose build $SERVICE
docker compose up -d $SERVICE

echo ""
echo "🌍 Application running internally on port $PORT"
echo "⚙️ Uvicorn workers: $WORKERS"
echo "✅ Publicly accessible at: https://$DOMAIN"
