#!/bin/bash
set -e

echo "🔧 Aplicando parche a FastMCP para silenciar logs de arranque..."

# Detectar ruta de instalación de fastmcp dentro del contenedor
FASTMCP_PATH=$(python -c "import fastmcp, os; print(os.path.dirname(fastmcp.__file__))")

# Ejecutar el parche con sed
sed -i 's/logger.info(f"Starting MCP server /# logger.info(f"Starting MCP server /' $FASTMCP_PATH/server/server.py

echo "✅ Parche aplicado en: $FASTMCP_PATH/server/server.py"
