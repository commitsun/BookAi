# =====================================================
# mcp_server.py
# =====================================================
"""
Servidor MCP (Model Context Protocol)
-------------------------------------
Versión inicial sin autenticación ni restricciones,
idéntica al comportamiento de n8n.

Expone:
  POST /tools/knowledge_base
"""

import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Tools
from tools import knowledge_base

# =====================================================
# ⚙️ CONFIGURACIÓN BASE
# =====================================================

app = FastAPI(
    title="BookAI MCP Server",
    description="Servidor MCP compatible con n8n para base de conocimientos vectorizada",
    version="1.0.0",
)

# CORS libre (para permitir conexiones desde cualquier cliente)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Logger básico
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("MCPServer")

# =====================================================
# 🔌 REGISTRO DE ENDPOINTS
# =====================================================

app.include_router(knowledge_base.router, prefix="/tools", tags=["Knowledge Base"])

# =====================================================
# 🩺 HEALTH CHECK
# =====================================================

@app.get("/health")
async def health_check():
    return {"status": "ok", "message": "MCP Server activo", "version": "1.0.0"}

# =====================================================
# ▶️ EJECUCIÓN LOCAL
# =====================================================

if __name__ == "__main__":
    import uvicorn
    log.info("🚀 Iniciando MCP Server en http://localhost:8001 ...")
    uvicorn.run(app, host="0.0.0.0", port=8001, reload=True)
