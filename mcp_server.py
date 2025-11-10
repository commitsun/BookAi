# =====================================================
# mcp_server.py
# =====================================================
"""
BookAI MCP Server (HTTP API)
------------------------------------
Servidor REST compatible con `langchain_mcp_adapters`
y con n8n.

Expone el endpoint:
  POST /tools/knowledge_base
para consultar la base de conocimientos vectorizada.
"""

import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from core.logging import audit_middleware
from tools import knowledge_base

# =====================================================
# ⚙️ CONFIGURACIÓN BASE
# =====================================================

app = FastAPI(
    title="BookAI MCP Server",
    description="Servidor MCP HTTP compatible con n8n / LangChain streamable_http",
    version="1.0.0",
)

# CORS abierto para compatibilidad
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Middleware de auditoría simple
app.middleware("http")(audit_middleware)

# Logger global
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("MCPServer")

# =====================================================
# 🔌 ENDPOINTS PRINCIPALES
# =====================================================

# Tool principal (base de conocimientos)
app.include_router(knowledge_base.router, prefix="/tools", tags=["Knowledge Base"])

# =====================================================
# 🩺 HEALTH CHECK
# =====================================================

@app.get("/health")
async def health_check():
    """Verifica el estado del servidor MCP."""
    return {
        "status": "ok",
        "message": "MCP Server activo",
        "version": "1.0.0"
    }

# =====================================================
# ▶️ EJECUCIÓN LOCAL
# =====================================================

if __name__ == "__main__":
    import uvicorn
    log.info("🚀 Iniciando MCP Server HTTP en http://localhost:8001 ...")
    uvicorn.run(app, host="0.0.0.0", port=8001)
