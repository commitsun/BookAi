# main_mcp.py
"""
MCP Server - Protocolo Real (BookAI)
------------------------------------
Servidor compatible con el cliente langchain_mcp_adapters.
Expone la tool "knowledge_base" conectada a Supabase.
"""

import logging
from mcp.server.fastmcp import FastMCP
from core.config import supabase_client, openai_client, MODEL_EMBEDDING

log = logging.getLogger("bookai_mcp")
mcp = FastMCP("BookAI MCP Server")


@mcp.tool()
async def knowledge_base(
    query: str,
    match_count: int = 7,
    match_threshold: float = 0.75
):
    """
    Tool: Consulta la base de conocimientos vectorizada de Alda Ponferrada.
    """
    try:
        if not query.strip():
            return {"error": "La consulta no puede estar vacía"}

        log.info(f"🧠 Consulta KB: {query}")

        # 1️⃣ Generar embedding
        embedding_response = openai_client.embeddings.create(
            model=MODEL_EMBEDDING,
            input=query,
        )
        query_embedding = embedding_response.data[0].embedding

        # 2️⃣ Llamar RPC en Supabase
        rpc_response = supabase_client.rpc(
            "match_documents",
            {
                "query_embedding": query_embedding,
                "match_threshold": match_threshold,
                "match_count": match_count,
                "filter": None,
            },
        ).execute()

        documents = rpc_response.data or []
        return {
            "success": True,
            "results_count": len(documents),
            "data": documents,
        }

    except Exception as e:
        log.error(f"❌ Error en knowledge_base: {e}", exc_info=True)
        return {"error": str(e)}


if __name__ == "__main__":
    log.info("🚀 Iniciando MCP Server (Protocolo MCP) en puerto 8001 ...")
    mcp.run()
