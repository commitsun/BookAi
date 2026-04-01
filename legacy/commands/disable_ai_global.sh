#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env}"
SERVICE="${SERVICE:-hotelai-app}"

if [ ! -f "$ENV_FILE" ]; then
  echo "No existe $ENV_FILE en $(pwd)"
  exit 1
fi

if grep -q '^BOOKAI_GLOBAL_ENABLED=' "$ENV_FILE"; then
  sed -i 's/^BOOKAI_GLOBAL_ENABLED=.*/BOOKAI_GLOBAL_ENABLED=false/' "$ENV_FILE"
else
  echo "BOOKAI_GLOBAL_ENABLED=false" >> "$ENV_FILE"
fi

echo "BOOKAI_GLOBAL_ENABLED=false aplicado en $ENV_FILE"
echo "Reiniciando servicio $SERVICE..."
cd "$PROJECT_DIR"
docker compose up -d --build "$SERVICE"
echo "BookAI desactivado globalmente."
