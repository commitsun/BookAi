#!/bin/bash
set -e

SERVICE=hotelai-app
PORT=8000
DOMAIN="bookai.predev.roomdoo.com"

echo "🛠️ Rebuilding Docker container for production..."
docker compose down --remove-orphans
docker compose build $SERVICE
docker compose up -d $SERVICE

echo ""
echo "🌍 Application running internally on port $PORT"
echo "✅ Publicly accessible at: https://$DOMAIN"