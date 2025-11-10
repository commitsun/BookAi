# mcp_server.py

"""
MCP Server - BookAI
-------------------
Servidor FastAPI que expone tools para agentes de IA.
Por ahora:
  - POST /tools/knowledge_base
Sin autenticación, como n8n en tu configuración actual.
"""

import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.logging import audit_middleware
from tools import knowledge_base

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("MCPServer")

app = FastAPI(
    title="BookAI MCP Server",
    description="Servidor MCP para base de conocimientos vectorizada (Supabase + OpenAI)",
    version="1.0.0",
)

# CORS abierto (como n8n)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auditoría simple
app.middleware("http")(audit_middleware)

# Registrar tools
app.include_router(knowledge_base.router, prefix="/tools", tags=["Knowledge Base"])


@app.get("/health")
async def health_check():
    return {"status": "ok", "message": "MCP Server activo", "version": "1.0.0"}


if __name__ == "__main__":
    import uvicorn

    log.info("🚀 Iniciando MCP Server en http://localhost:8001 ...")
    uvicorn.run(app, host="0.0.0.0", port=8001, reload=True)
