#!/bin/bash
set -e

SERVICE=hotelai-app
PORT=8000

echo "🛠️ Reconstruyendo contenedor (modo WhatsApp con ngrok)..."
docker compose down --remove-orphans
docker compose build $SERVICE
docker compose up -d $SERVICE

echo "🌍 Exponiendo webhook con ngrok..."
ngrok http $PORT
