# =====================================================
# mcp_server.py
# =====================================================
"""
BookAI MCP Server (HTTP API)
------------------------------------
Servidor REST compatible con `langchain_mcp_adapters`
y con n8n.

Expone los endpoints:
  POST /tools/knowledge_base       → Consulta base de conocimientos vectorizada
  POST /tools/availability_pricing → Consulta disponibilidad y precios en Roomdoo
"""

import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from tools import knowledge_base, availability_pricing

# =====================================================
# ⚙️ CONFIGURACIÓN BASE
# =====================================================

app = FastAPI(
    title="BookAI MCP Server",
    description="Servidor MCP HTTP compatible con n8n / LangChain streamable_http",
    version="1.0.0",
)

# -----------------------------------------------------
# 🌐 Configuración CORS
# -----------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # CORS abierto para compatibilidad
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------------------------------
# 🪵 Logger global
# -----------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("MCPServer")

# =====================================================
# 🔌 ENDPOINTS PRINCIPALES
# =====================================================

# Tool 1️⃣: Base de Conocimientos
app.include_router(knowledge_base.router, prefix="/tools", tags=["Knowledge Base"])

# Tool 2️⃣: Disponibilidad y Precios
app.include_router(availability_pricing.router, prefix="/tools", tags=["Availability & Pricing"])

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
