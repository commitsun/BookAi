#!/bin/bash

# =====================================================
# 🚀 rebuild_container.sh
# Script para limpiar, reconstruir y levantar el MCP Server
# =====================================================

CONTAINER_NAME="bookai_mcp_server"

echo "🧹 Deteniendo contenedor '${CONTAINER_NAME}'..."
docker stop ${CONTAINER_NAME} >/dev/null 2>&1 || true

echo "🧨 Eliminando contenedor antiguo..."
docker rm ${CONTAINER_NAME} >/dev/null 2>&1 || true

echo "🧼 Limpiando imagen anterior (solo esta)..."
docker rmi $(docker images -q ${CONTAINER_NAME}) >/dev/null 2>&1 || true

echo "🏗️ Reconstruyendo imagen desde cero..."
docker compose build --no-cache

echo "🚀 Levantando nuevo contenedor..."
docker compose up -d

echo "✅ Contenedor '${CONTAINER_NAME}' levantado con éxito."
echo "🌍 Accede al servidor en: http://localhost:8001/health"
echo "📜 Logs en tiempo real: docker logs -f ${CONTAINER_NAME}"
