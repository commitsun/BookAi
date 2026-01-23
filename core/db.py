import os
import logging
from datetime import datetime

import pytz

from core.config import Settings
from core.utils.time_context import DEFAULT_TZ
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


def _get_day_key(timezone: str = DEFAULT_TZ) -> str:
    tz = pytz.timezone(timezone)
    now = datetime.now(tz)
    return now.strftime("%Y-%m-%d")


def add_kb_daily_cache(
    *,
    property_id: str | int | None = None,
    kb_name: str | None = None,
    property_name: str | None = None,
    topic: str | None = None,
    category: str | None = None,
    content: str | None = None,
    source_type: str | None = None,
    day_key: str | None = None,
    timezone: str = DEFAULT_TZ,
) -> None:
    """Guarda una entrada temporal de KB para el dia actual."""
    if not (content and str(content).strip()):
        return

    payload = {
        "day_key": day_key or _get_day_key(timezone),
        "content": str(content).strip(),
    }
    if property_id is not None:
        payload["property_id"] = property_id
    if kb_name:
        payload["kb_name"] = str(kb_name).strip()
    if property_name:
        payload["property_name"] = str(property_name).strip()
    if topic:
        payload["topic"] = str(topic).strip()
    if category:
        payload["category"] = str(category).strip()
    if source_type:
        payload["source_type"] = str(source_type).strip()

    try:
        supabase.table(Settings.TEMP_KB_TABLE).insert(payload).execute()
    except Exception as exc:
        logging.warning("⚠️ No se pudo guardar cache temporal KB: %s", exc)


def fetch_kb_daily_cache(
    *,
    property_id: str | int | None = None,
    kb_name: str | None = None,
    property_name: str | None = None,
    day_key: str | None = None,
    timezone: str = DEFAULT_TZ,
    limit: int = 25,
) -> list[dict]:
    """Recupera entradas temporales de KB para el dia actual."""
    if property_id is None and not kb_name and not property_name:
        return []

    key = day_key or _get_day_key(timezone)
    try:
        query = supabase.table(Settings.TEMP_KB_TABLE).select(
            "topic, category, content, created_at, property_id, kb_name, property_name"
        )
        query = query.eq("day_key", key)
        if property_id is not None:
            query = query.eq("property_id", property_id)
        elif kb_name:
            query = query.eq("kb_name", kb_name)
        else:
            query = query.eq("property_name", property_name)
        response = query.order("created_at", desc=False).limit(limit).execute()
        return response.data or []
    except Exception as exc:
        logging.warning("⚠️ No se pudo leer cache temporal KB: %s", exc)
        return []


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
    table: str = "chat_history",
) -> None:
    """
    Guarda un mensaje en la base de datos relacional de Supabase.
    - conversation_id: número del usuario sin '+'
    - property_id: id de property (opcional)
    - original_chat_id: id original usado en memoria (opcional)
    - role: 'user'/'assistant' o alias (ej. 'guest'/'bookai')
    - content: texto del mensaje
    - table: tabla destino en Supabase
    """
    try:
        normalized_role = (role or "").strip().lower()
        if normalized_role in {"assistant", "system", "tool"}:
            normalized_role = "bookai"
        if normalized_role not in {"guest", "user", "bookai"}:
            normalized_role = "bookai"

        clean_id = str(conversation_id).replace("+", "").strip()
        original_clean = str(original_chat_id).replace("+", "").strip() if original_chat_id else clean_id

        data = {
            "conversation_id": clean_id,
            "role": normalized_role or "bookai",
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
            supabase.table(table).insert(data).execute()
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
                supabase.table(table).insert(data).execute()
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
    table: str = "chat_history",
    channel: str | None = None,
):
    """
    Recupera los últimos mensajes de una conversación, ordenados por fecha.
    - conversation_id: número del usuario (sin '+')
    - property_id: id de property (opcional)
    - limit: cantidad máxima de mensajes a devolver
    - since: datetime opcional (solo mensajes posteriores a esa fecha)
    - table: tabla origen en Supabase
    """
    try:
        clean_id = str(conversation_id).replace("+", "").strip()

        query = supabase.table(table).select("role, content, created_at")
        if property_id is not None:
            query = query.eq("conversation_id", clean_id).eq("property_id", property_id)
        else:
            query = query.eq("conversation_id", clean_id)
        if channel:
            query = query.eq("channel", channel)

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
def clear_conversation(
    conversation_id: str,
    property_id: str | int | None = None,
    table: str = "chat_history",
) -> None:
    """
    Elimina todos los mensajes de una conversación.
    - table: tabla destino en Supabase
    """
    try:
        clean_id = str(conversation_id).replace("+", "").strip()
        query = supabase.table(table).delete().eq("conversation_id", clean_id)
        if property_id is not None:
            query = query.eq("property_id", property_id)
        query.execute()
        logging.info(f"🧹 Conversación {clean_id} eliminada correctamente.")
    except Exception as e:
        logging.error(f"⚠️ Error eliminando conversación {conversation_id}: {e}", exc_info=True)
