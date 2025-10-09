import os
import logging
import requests
from fastmcp import FastMCP
from supabase import create_client
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

# =====================================================
# ⚙️ Configuración inicial
# =====================================================
load_dotenv()
logging.basicConfig(level=logging.INFO)

mcp = FastMCP("InternoAgent")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_ENCARGADO_CHAT_ID")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.3)

# =====================================================
# 💾 Funciones auxiliares — interacción con Supabase
# =====================================================
def save_pending_query(conversation_id: str, question: str):
    """
    Guarda una nueva pregunta del cliente en la tabla pending_queries.
    Si ya hay una pendiente igual, no la duplica.
    """
    try:
        # Evitar duplicados exactos (misma pregunta + cliente pendiente)
        existing = (
            supabase.table("pending_queries")
            .select("id")
            .eq("conversation_id", conversation_id)
            .eq("question", question)
            .eq("status", "pending")
            .execute()
        )
        if existing.data:
            logging.info(f"⚠️ Ya existe una consulta pendiente igual para {conversation_id}")
            return existing.data[0]["id"]

        res = (
            supabase.table("pending_queries")
            .insert({"conversation_id": conversation_id, "question": question, "status": "pending"})
            .execute()
        )
        inserted_id = res.data[0]["id"]
        logging.info(f"💾 Nueva consulta guardada (ID {inserted_id}) para {conversation_id}")
        return inserted_id

    except Exception as e:
        logging.error(f"❌ Error guardando pregunta: {e}", exc_info=True)
        return None


def mark_query_as_answered(conversation_id: str, answer: str):
    """
    Marca como respondida la última pregunta pendiente del cliente.
    """
    try:
        # Obtener la última pregunta pendiente de ese cliente
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
            logging.warning(f"⚠️ No se encontró consulta pendiente para {conversation_id}")
            return None

        query_id = query.data[0]["id"]

        # Actualizar con la respuesta
        supabase.table("pending_queries").update(
            {"answer": answer, "status": "answered"}
        ).eq("id", query_id).execute()

        logging.info(f"✅ Consulta {query_id} ({conversation_id}) marcada como respondida.")
        return query_id

    except Exception as e:
        logging.error(f"❌ Error al actualizar respuesta: {e}", exc_info=True)
        return None


# =====================================================
# 📩 Funciones de comunicación con Telegram
# =====================================================
def send_to_encargado(conversation_id: str, message: str):
    """
    Envía la pregunta del cliente al encargado por Telegram.
    """
    text = (
        f"👤 *Nueva consulta del cliente*\n"
        f"🆔 ID: `{conversation_id}`\n"
        f"❓ *Pregunta:* {message}\n\n"
        f"Por favor, responde con el formato:\n"
        f"`RESPUESTA {conversation_id}: <tu respuesta>`"
    )

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
        res = requests.post(url, json=payload)
        if res.status_code == 200:
            logging.info(f"📨 Consulta enviada al encargado por Telegram ({conversation_id})")
        else:
            logging.error(f"⚠️ Error enviando a Telegram: {res.text}")
    except Exception as e:
        logging.error(f"❌ Error enviando a Telegram: {e}", exc_info=True)


def send_to_client(conversation_id: str, message: str):
    """
    Aquí deberías reenviar el mensaje al cliente original (WhatsApp, Web, etc.).
    Por ahora, solo lo registra.
    """
    logging.info(f"📤 (Simulado) Enviando al cliente {conversation_id}: {message}")


# =====================================================
# 🧠 Herramientas MCP del InternoAgent
# =====================================================
@mcp.tool()
async def escalate_to_encargado(mensaje: str, conversation_id: str) -> str:
    """
    Se activa cuando falta información o hay un error.
    Guarda la consulta en Supabase y la envía al encargado vía Telegram.
    """
    try:
        save_pending_query(conversation_id, mensaje)
        send_to_encargado(conversation_id, mensaje)

        return (
            "He contactado con el encargado del hotel para confirmar esa información. "
            "En cuanto tenga respuesta te la haré llegar. 🕐"
        )
    except Exception as e:
        logging.error(f"❌ Error en escalate_to_encargado: {e}", exc_info=True)
        return "He intentado contactar con el encargado, pero hubo un problema. Inténtalo más tarde."


@mcp.tool()
async def process_encargado_reply(raw_text: str) -> str:
    """
    Procesa la respuesta del encargado recibida por Telegram.
    Formato esperado: RESPUESTA <conversation_id>: <mensaje>
    """
    import re
    match = re.match(r"RESPUESTA\s+(\+?\d+):\s*(.*)", raw_text.strip(), re.IGNORECASE)
    if not match:
        return "Formato incorrecto. Usa: RESPUESTA <id_cliente>: <texto>"

    conversation_id, answer = match.groups()
    mark_query_as_answered(conversation_id, answer)

    # Reformular la respuesta para el cliente
    try:
        prompt = (
            "Eres el asistente del hotel. Reformula el siguiente mensaje del encargado "
            "para enviárselo al cliente de forma amable y profesional, "
            "sin mencionar que proviene del encargado."
        )
        llm_reply = llm.invoke([
            {"role": "system", "content": prompt},
            {"role": "user", "content": answer}
        ])
        friendly = llm_reply.content.strip()
    except Exception as e:
        logging.error(f"⚠️ Error reformulando respuesta: {e}")
        friendly = answer

    send_to_client(conversation_id, friendly)
    return f"✅ Respuesta enviada al cliente {conversation_id}."


# =====================================================
# 🚀 Inicio del agente
# =====================================================
if __name__ == "__main__":
    print("✅ InternoAgent operativo con Telegram + Supabase")
    mcp.run(transport="stdio", show_banner=False)
