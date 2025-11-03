#!/bin/bash

echo ""
echo "👋 Bienvenido al entorno de BookAI"
echo "------------------------------------"

# Ruta del archivo .env
ENV_FILE="/home/ec2-user/BookAI/.env"
PROJECT_DIR="/home/ec2-user/BookAI"

# Asegurarse de que el .env existe
if [ ! -f "$ENV_FILE" ]; then
  touch "$ENV_FILE"
fi

# Mostrar quién está conectado
CURRENT_USER=$(whoami)
echo "🧑 Usuario actual: $CURRENT_USER"

# Leer el chat ID actual (si existe)
EXISTING_ID=$(grep '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | cut -d'=' -f2)

if [ -n "$EXISTING_ID" ]; then
  echo "ℹ️ Chat ID actual: $EXISTING_ID"
fi

# Pedir nuevo ID
read -p "💬 Introduce tu nuevo 
    echo "✅ Manteniendo Chat ID existente: $CHAT_ID"
  else
    echo "⚠️ No se ha introducido ningún Chat ID. Saliendo..."
    exit 1
  fi
else
  echo "💾 Actualizando Chat ID a: $CHAT_ID"
  sed -i '/^TELEGRAM_CHAT_ID=/d' "$ENV_FILE"
  echo "TELEGRAM_CHAT_ID=$CHAT_ID" >> "$ENV_FILE"

  echo "♻️ Reiniciando contenedor para aplicar cambios..."
  cd "$PROJECT_DIR/commands"

  # Detener y reiniciar el contenedor
  docker compose down || true
  docker compose up -d

  echo "✅ Contenedor reiniciado con éxito."
fi

echo "------------------------------------"
echo ""
