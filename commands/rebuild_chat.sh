#!/bin/bash
set -e

SERVICE=hotelai

echo "🛠️ Reconstruyendo imagen y contenedor..."
docker compose down --remove-orphans
docker compose build --no-cache $SERVICE
docker compose up -d $SERVICE

echo "🚀 Ejecutando chat..."
docker exec -it $SERVICE python chat_cli.py
