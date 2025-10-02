#!/bin/bash
set -e

SERVICE=hotelai

echo "🛠️ Reconstruyendo contenedor (modo WhatsApp)..."
docker compose build $SERVICE
docker compose up -d $SERVICE

echo "🚀 Arrancando webhook con FastAPI en contenedor..."
docker exec -d $SERVICE uvicorn channels_wrapper.channels.whatsapp_meta:fastapi_app --host 0.0.0.0 --port 8000

echo "🌍 Arrancando Cloudflare Tunnel en host..."
cloudflared tunnel --url http://localhost:8000
