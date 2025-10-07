#!/bin/bash
set -e

SERVICE=hotelai-app

echo "🚀 Ejecutando chat en el contenedor existente..."
docker compose up -d $SERVICE
docker exec -it $SERVICE python chat_cli.py
