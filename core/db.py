import os
import logging
from supabase import create_client, Client

# ======================================================
# ⚙️ Conexión a Supabase
# ======================================================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("❌ Faltan variables SUPABASE_URL o SUPABASE_KEY en el archivo .env")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
logging.info("✅ Conexión con Supabase inicializada correctamente.")


# ======================================================
# 💾 Guardar mensaje (sin embeddings)
# ======================================================
def save_message(
    conversation_id: str,
    role: str,
    content: str,
    escalation_id: str | None = None,
    client_name: str | None = None,
    channel: str | None = None,
    property_id: str | int | None = None,
    original_chat_id: str | None = None,
) -> None:
    """
    Guarda un mensaje en la base de datos relacional de Supabase.
    - conversation_id: número del usuario sin '+'
    - property_id: id de property (opcional)
    - original_chat_id: id original usado en memoria (opcional)
    - role: 'user' o 'assistant'
    - content: texto del mensaje
    """
    try:
        clean_id = str(conversation_id).replace("+", "").strip()
        original_clean = str(original_chat_id).replace("+", "").strip() if original_chat_id else clean_id

        data = {
            "conversation_id": clean_id,
            "role": role,
            "content": content,
            "read_status": False,
            "original_chat_id": original_clean,
        }
        if escalation_id:
            data["escalation_id"] = escalation_id
        if client_name:
            data["client_name"] = client_name
        if channel:
            data["channel"] = channel
        if property_id is not None:
            data["property_id"] = property_id

        try:
            supabase.table("chat_history").insert(data).execute()
        except Exception:
            retry = False
            if "escalation_id" in data:
                data.pop("escalation_id", None)
                retry = True
            if "client_name" in data:
                data.pop("client_name", None)
                retry = True
            if "property_id" in data:
                data.pop("property_id", None)
                retry = True
            if "original_chat_id" in data:
                data.pop("original_chat_id", None)
                retry = True
            if retry:
                supabase.table("chat_history").insert(data).execute()
            else:
                raise
        logging.info(f"💾 Mensaje guardado correctamente en conversación {clean_id}")

    except Exception as e:
        logging.error(f"⚠️ Error guardando mensaje en Supabase: {e}", exc_info=True)


# ======================================================
# 🧠 Obtener historial de conversación
# ======================================================
def get_conversation_history(
    conversation_id: str,
    limit: int = 10,
    since=None,
    property_id: str | int | None = None,
):
    """
    Recupera los últimos mensajes de una conversación, ordenados por fecha.
    - conversation_id: número del usuario (sin '+')
    - property_id: id de property (opcional)
    - limit: cantidad máxima de mensajes a devolver
    - since: datetime opcional (solo mensajes posteriores a esa fecha)
    """
    try:
        clean_id = str(conversation_id).replace("+", "").strip()

        query = supabase.table("chat_history").select("role, content, created_at")
        if property_id is not None:
            query = query.eq("conversation_id", clean_id).eq("property_id", property_id)
        else:
            query = query.eq("conversation_id", clean_id)

        # Si se pasa una fecha 'since', filtramos por created_at
        if since is not None:
            query = query.gte("created_at", since.isoformat())

        response = (
            query.order("created_at", desc=False)
            .limit(limit)
            .execute()
        )

        messages = response.data or []
        logging.info(f"🧩 Historial recuperado ({len(messages)} mensajes) para {clean_id}")
        return messages

    except Exception as e:
        logging.error(f"⚠️ Error obteniendo historial: {e}", exc_info=True)
        return []



# ======================================================
# 🧹 Borrar historial (útil para pruebas o depuración)
# ======================================================
def clear_conversation(conversation_id: str, property_id: str | int | None = None) -> None:
    """
    Elimina todos los mensajes de una conversación.
    """
    try:
        clean_id = str(conversation_id).replace("+", "").strip()
        query = supabase.table("chat_history").delete().eq("conversation_id", clean_id)
        if property_id is not None:
            query = query.eq("property_id", property_id)
        query.execute()
        logging.info(f"🧹 Conversación {clean_id} eliminada correctamente.")
    except Exception as e:
        logging.error(f"⚠️ Error eliminando conversación {conversation_id}: {e}", exc_info=True)
