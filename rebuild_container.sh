#!/bin/bash

# =====================================================
# 🚀 rebuild_container.sh
# Script para limpiar, reconstruir y levantar el MCP Server (aislado)
# =====================================================

PROJECT_NAME="mcpserver"
CONTAINER_NAME="bookai_mcp_server"

echo "🧹 Deteniendo contenedor '${CONTAINER_NAME}'..."
docker compose -p ${PROJECT_NAME} stop ${CONTAINER_NAME} >/dev/null 2>&1 || true

echo "🧨 Eliminando contenedor antiguo..."
docker compose -p ${PROJECT_NAME} rm -f ${CONTAINER_NAME} >/dev/null 2>&1 || true

echo "🧼 Limpiando imagen anterior (solo esta)..."
IMAGE_ID=$(docker images -q ${CONTAINER_NAME})
if [ -n "$IMAGE_ID" ]; then
  docker rmi $IMAGE_ID >/dev/null 2>&1 || true
fi

echo "🏗️ Reconstruyendo imagen desde cero (proyecto: ${PROJECT_NAME})..."
docker compose -p ${PROJECT_NAME} build --no-cache

echo "🚀 Levantando nuevo contenedor..."
docker compose -p ${PROJECT_NAME} up -d

echo "✅ Contenedor '${CONTAINER_NAME}' levantado con éxito."
echo "🌍 Accede al servidor en: http://localhost:8001/health"
echo "📜 Logs en tiempo real: docker logs -f ${CONTAINER_NAME}"
