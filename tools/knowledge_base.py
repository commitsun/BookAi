# tools/knowledge_base.py

"""
Tool: Base de Conocimientos (idéntico a n8n actual)
---------------------------------------------------
- Recibe una consulta del usuario.
- Genera el embedding con OpenAI (text-embedding-3-small).
- Llama a la función RPC 'match_documents' en Supabase
  con los parámetros: filter, match_count, query_embedding.
- Devuelve los resultados de la búsqueda semántica.
"""

import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from core.config import supabase_client, openai_client, MODEL_EMBEDDING

router = APIRouter()
log = logging.getLogger("knowledge_base")


class KnowledgeBaseInput(BaseModel):
    query: str = Field(..., description="Consulta del usuario")
    match_count: int = Field(default=7, description="Número de resultados a devolver")


@router.post("/knowledge_base")
async def knowledge_base_tool(input_data: KnowledgeBaseInput):
    """
    Endpoint que replica exactamente la tool de n8n:
    - Input: { "query": "...", "match_count": 7 }
    - Output: { "success": true, "data": [...], "results_count": n }
    """
    try:
        query_text = input_data.query.strip()
        if not query_text:
            raise HTTPException(status_code=400, detail="La consulta no puede estar vacía")

        log.info(f"🧠 Consulta KB: '{query_text}'")

        # 1️⃣ Generar embedding (modelo igual que en n8n)
        embedding_response = openai_client.embeddings.create(
            model=MODEL_EMBEDDING,
            input=query_text,
        )
        query_embedding = embedding_response.data[0].embedding

        # 2️⃣ Llamar RPC de Supabase (solo con los 3 parámetros que existen)
        rpc_response = supabase_client.rpc(
            "match_documents",
            {
                "query_embedding": query_embedding,
                "match_count": input_data.match_count,
                "filter": None,  # 'filter IS NULL OR metadata @> filter'
            },
        ).execute()

        documents = rpc_response.data or []

        # 3️⃣ Devolver formato igual que n8n
        return {
            "success": True,
            "data": documents,
            "results_count": len(documents),
        }

    except Exception as e:
        log.error(f"❌ Error en knowledge_base: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
