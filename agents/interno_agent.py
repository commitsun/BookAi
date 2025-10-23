# agents/interno_agent.py
import re
import logging
import requests
from fastmcp import FastMCP
from supabase import create_client
from langchain_openai import ChatOpenAI
from core.config import Settings as C

log = logging.getLogger("InternoAgent")
mcp = FastMCP("InternoAgent")

# Base de datos y modelo
supabase = create_client(C.SUPABASE_URL, C.SUPABASE_KEY)
llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.3)

# =====================================================
# 💾 Funciones auxiliares (Supabase)
# =====================================================
def save_pending_query(conversation_id: str, question: str):
    """Guarda una nueva consulta pendiente en Supabase."""
    try:
        existing = (
            supabase.table("pending_queries")
            .select("id")
            .eq("conversation_id", conversation_id)
            .eq("question", question)
            .eq("status", "pending")
            .execute()
        )
        if existing.data:
            log.info(f"⚠️ Consulta pendiente ya existente para {conversation_id}")
            return existing.data[0]["id"]

        res = (
            supabase.table("pending_queries")
            .insert({"conversation_id": conversation_id, "question": question, "status": "pending"})
            .execute()
        )
        inserted_id = res.data[0]["id"]
        log.info(f"💾 Consulta guardada (ID {inserted_id}) para {conversation_id}")
        return inserted_id
    except Exception as e:
        log.error(f"❌ Error guardando pregunta: {e}", exc_info=True)
        return None


def mark_query_as_answered(conversation_id: str, answer: str):
    """Marca como respondida la última pregunta pendiente."""
    try:
        query = (
            supabase.table("pending_queries")
            .select("id")
            .eq("conversation_id", conversation_id)
            .eq("status", "pending")
            .order("id", desc=True)
            .limit(1)
            .execute()
        )
        if not query.data:
            log.warning(f"⚠️ No hay consultas pendientes para {conversation_id}")
            return None
        query_id = query.data[0]["id"]
        supabase.table("pending_queries").update(
            {"answer": answer, "status": "answered"}
        ).eq("id", query_id).execute()
        log.info(f"✅ Consulta {query_id} marcada como respondida.")
        return query_id
    except Exception as e:
        log.error(f"❌ Error actualizando respuesta: {e}", exc_info=True)
        return None

# =====================================================
# 📲 Comunicación con Telegram
# =====================================================
def send_to_encargado(conversation_id: str, message: str):
    """Envía la pregunta del cliente al encargado por Telegram."""
    text = (
        f"👤 *Nueva consulta del cliente*\n"
        f"🆔 ID: `{conversation_id}`\n"
        f"❓ *Pregunta:* {message}\n\n"
        f"Por favor, responde con el formato:\n"
        f"`RESPUESTA {conversation_id}: <tu respuesta>`"
    )
    try:
        url = f"https://api.telegram.org/bot{C.TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": C.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
        res = requests.post(url, json=payload)
        if res.status_code == 200:
            log.info(f"📨 Enviado al encargado (cliente {conversation_id})")
        else:
            log.error(f"⚠️ Error enviando a Telegram: {res.text}")
    except Exception as e:
        log.error(f"❌ Error enviando a Telegram: {e}", exc_info=True)


def send_to_client(conversation_id: str, message: str):
    """Simula envío de mensaje al cliente (por ahora solo log)."""
    log.info(f"📤 Respuesta enviada al cliente {conversation_id}: {message}")

# =====================================================
# 🧠 Herramientas MCP
# =====================================================
@mcp.tool()
async def escalate_to_encargado(mensaje: str, conversation_id: str) -> str:
    """Escala la consulta al encargado vía Telegram."""
    try:
        save_pending_query(conversation_id, mensaje)
        send_to_encargado(conversation_id, mensaje)
        return (
            "He contactado con el encargado del hotel para confirmar esa información. "
            "En cuanto tenga respuesta te la haré llegar. 🕐"
        )
    except Exception as e:
        log.error(f"❌ Error en escalate_to_encargado: {e}", exc_info=True)
        return "No pude contactar con el encargado. Inténtalo más tarde."

@mcp.tool()
async def process_encargado_reply(raw_text: str) -> str:
    """Procesa respuestas del encargado recibidas por Telegram."""
    match = re.match(r"RESPUESTA\s+(\+?\d+):\s*(.*)", raw_text.strip(), re.IGNORECASE)
    if not match:
        return "Formato incorrecto. Usa: RESPUESTA <id_cliente>: <texto>"

    conversation_id, answer = match.groups()
    mark_query_as_answered(conversation_id, answer)

    # Reformula la respuesta con el modelo
    try:
        prompt = (
            "Eres el asistente del hotel. Reformula el mensaje del encargado "
            "para el cliente en tono amable y profesional, sin mencionar al encargado."
        )
        llm_reply = llm.invoke([
            {"role": "system", "content": prompt},
            {"role": "user", "content": answer},
        ])
        friendly = llm_reply.content.strip()
    except Exception as e:
        log.error(f"⚠️ Error reformulando: {e}")
        friendly = answer

    send_to_client(conversation_id, friendly)
    return f"✅ Respuesta enviada al cliente {conversation_id}."

# =====================================================
# 🚀 Inicio del agente
# =====================================================
if __name__ == "__main__":
    print("✅ InternoAgent operativo con Supabase + Telegram")
    mcp.run(transport="stdio", show_banner=False)
