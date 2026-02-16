import os
import logging
import re
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
    user_id: int | str | None = None,
    user_first_name: str | None = None,
    user_last_name: str | None = None,
    user_last_name2: str | None = None,
    channel: str | None = None,
    property_id: str | int | None = None,
    original_chat_id: str | None = None,
    structured_payload: dict | list | None = None,
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
        if user_id is not None and str(user_id).strip() != "":
            try:
                data["user_id"] = int(str(user_id).strip())
            except Exception:
                logging.warning("⚠️ user_id no numérico, se omite: %s", user_id)
        if user_first_name:
            data["user_first_name"] = str(user_first_name)
        if user_last_name:
            data["user_last_name"] = str(user_last_name)
        if user_last_name2:
            data["user_last_name2"] = str(user_last_name2)
        if channel:
            data["channel"] = channel
        if property_id is not None:
            data["property_id"] = property_id
        if structured_payload is not None:
            data["structured_payload"] = structured_payload

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
            if "user_id" in data:
                data.pop("user_id", None)
                retry = True
            if "user_first_name" in data:
                data.pop("user_first_name", None)
                retry = True
            if "user_last_name" in data:
                data.pop("user_last_name", None)
                retry = True
            if "user_last_name2" in data:
                data.pop("user_last_name2", None)
                retry = True
            if "property_id" in data:
                data.pop("property_id", None)
                retry = True
            if "original_chat_id" in data:
                data.pop("original_chat_id", None)
                retry = True
            if "structured_payload" in data:
                data.pop("structured_payload", None)
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
    original_chat_id: str | None = None,
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

        select_fields = "role, content, created_at, structured_payload"
        query = supabase.table(table).select(select_fields)
        if property_id is not None:
            query = query.eq("conversation_id", clean_id).eq("property_id", property_id)
        else:
            query = query.eq("conversation_id", clean_id)
        if original_chat_id:
            query = query.eq("original_chat_id", str(original_chat_id).replace("+", "").strip())
        if channel:
            query = query.eq("channel", channel)

        # Si se pasa una fecha 'since', filtramos por created_at
        if since is not None:
            query = query.gte("created_at", since.isoformat())

        try:
            response = (
                query.order("created_at", desc=False)
                .limit(limit)
                .execute()
            )
        except Exception:
            # Compatibilidad: tablas antiguas sin structured_payload.
            query = supabase.table(table).select("role, content, created_at")
            if property_id is not None:
                query = query.eq("conversation_id", clean_id).eq("property_id", property_id)
            else:
                query = query.eq("conversation_id", clean_id)
            if original_chat_id:
                query = query.eq("original_chat_id", str(original_chat_id).replace("+", "").strip())
            if channel:
                query = query.eq("channel", channel)
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


def attach_structured_payload_to_latest_message(
    *,
    conversation_id: str,
    structured_payload: dict | list,
    table: str = "chat_history",
    role: str = "bookai",
) -> bool:
    """
    Adjunta structured_payload al último mensaje de una conversación para un rol dado.
    Devuelve True si actualizó al menos una fila.
    """
    clean_id = str(conversation_id or "").replace("+", "").strip()
    if not clean_id or structured_payload is None:
        return False
    try:
        resp = (
            supabase.table(table)
            .select("id")
            .eq("conversation_id", clean_id)
            .eq("role", str(role or "bookai").strip().lower())
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            return False
        row_id = rows[0].get("id")
        if row_id is None:
            return False
        (
            supabase.table(table)
            .update({"structured_payload": structured_payload})
            .eq("id", row_id)
            .execute()
        )
        return True
    except Exception as exc:
        logging.warning("⚠️ No se pudo adjuntar structured_payload: %s", exc)
        return False


def get_last_property_id_for_conversation(
    conversation_id: str,
    *,
    table: str = "chat_history",
    limit: int = 30,
) -> str | int | None:
    """
    Recupera el último property_id asociado a una conversación (si existe).
    - conversation_id: número del usuario (sin '+')
    - limit: cantidad máxima de mensajes a revisar (orden desc).
    """
    try:
        clean_id = str(conversation_id).replace("+", "").strip()
        response = (
            supabase.table(table)
            .select("property_id, created_at")
            .eq("conversation_id", clean_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = response.data or []
        if not rows:
            return None
        for row in rows:
            prop = row.get("property_id")
            if prop is None or str(prop).strip() == "":
                continue
            return prop
        return None
    except Exception as exc:
        logging.error("⚠️ Error obteniendo property_id de historial: %s", exc, exc_info=True)
        return None


def get_last_property_id_for_original_chat(
    original_chat_id: str,
    *,
    table: str = "chat_history",
    limit: int = 30,
) -> str | int | None:
    """
    Recupera el último property_id asociado a un original_chat_id (ej. instancia:telefono).
    """
    try:
        clean_id = str(original_chat_id).replace("+", "").strip()
        response = (
            supabase.table(table)
            .select("property_id, created_at")
            .eq("original_chat_id", clean_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = response.data or []
        if not rows:
            return None
        for row in rows:
            prop = row.get("property_id")
            if prop is None or str(prop).strip() == "":
                continue
            return prop
        return None
    except Exception as exc:
        logging.error("⚠️ Error obteniendo property_id por original_chat_id: %s", exc, exc_info=True)
        return None


# ======================================================
# 📌 Reservas por chat (persistencia ligera)
# ======================================================
def _normalize_chat_id(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).strip()
    if ":" in text:
        text = text.split(":")[-1]
    groups = re.findall(r"\d{6,}", text)
    if groups:
        return groups[-1]
    cleaned = re.sub(r"\D+", "", text)
    return cleaned or text


def _normalize_date_field(value: str | None) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    # Acepta ISO (YYYY-MM-DD) o fechas con hora.
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", ""))
        return parsed.date().isoformat()
    except Exception:
        pass
    # Acepta formato DD/MM/YYYY
    try:
        parsed = datetime.strptime(raw, "%d/%m/%Y")
        return parsed.date().isoformat()
    except Exception:
        pass
    # Acepta formato DD-MM-YYYY
    try:
        parsed = datetime.strptime(raw, "%d-%m-%Y")
        return parsed.date().isoformat()
    except Exception:
        pass
    return raw


def upsert_chat_reservation(
    *,
    chat_id: str,
    folio_id: str,
    checkin: str | None = None,
    checkout: str | None = None,
    property_id: str | int | None = None,
    instance_id: str | None = None,
    original_chat_id: str | None = None,
    reservation_locator: str | None = None,
    source: str | None = None,
) -> None:
    """
    Inserta/actualiza una reserva asociada a un chat.
    Requiere folio_id.
    """
    if not chat_id or not folio_id:
        return
    folio_id = str(folio_id).strip()
    if not re.fullmatch(r"(?=.*\d)[A-Za-z0-9]{4,}", folio_id):
        logging.warning("⚠️ folio_id inválido, se omite upsert: %s", folio_id)
        return

    normalized_chat = _normalize_chat_id(chat_id)

    def _locator_conflict(chat_key: str, folio: str | None, locator: str | None) -> bool:
        if not locator:
            return False
        try:
            resp = (
                supabase.table(Settings.CHAT_RESERVATIONS_TABLE)
                .select("folio_id, reservation_locator")
                .eq("chat_id", chat_key)
                .eq("reservation_locator", locator)
                .limit(1)
                .execute()
            )
            if resp.data:
                existing_folio = str(resp.data[0].get("folio_id") or "").strip()
                return bool(existing_folio and folio and existing_folio != str(folio).strip())
        except Exception as exc:
            logging.warning("⚠️ No se pudo verificar duplicados de chat_reservation: %s", exc)
        return False
    if _locator_conflict(normalized_chat, folio_id, reservation_locator):
        logging.info(
            "🧾 chat_reservation duplicada por locator omitida chat_id=%s folio_id=%s locator=%s",
            normalized_chat,
            folio_id,
            reservation_locator,
        )
        return

    payload = {
        "chat_id": normalized_chat,
        "folio_id": folio_id,
        "updated_at": datetime.utcnow().isoformat(),
    }
    if checkin:
        payload["checkin"] = _normalize_date_field(checkin)
    if checkout:
        payload["checkout"] = _normalize_date_field(checkout)
    if property_id is not None:
        payload["property_id"] = property_id
    if instance_id:
        payload["instance_id"] = str(instance_id).strip()
    if original_chat_id:
        payload["original_chat_id"] = str(original_chat_id).strip()
    if reservation_locator:
        payload["reservation_locator"] = str(reservation_locator).strip()
    if source:
        payload["source"] = str(source).strip()

    try:
        logging.info(
            "🧾 upsert_chat_reservation table=%s payload=%s",
            Settings.CHAT_RESERVATIONS_TABLE,
            payload,
        )
        supabase.table(Settings.CHAT_RESERVATIONS_TABLE).upsert(
            payload,
            on_conflict="chat_id,folio_id",
        ).execute()
    except Exception as exc:
        logging.warning("⚠️ No se pudo upsert chat_reservation: %s", exc, exc_info=True)
        try:
            supabase.table(Settings.CHAT_RESERVATIONS_TABLE).insert(payload).execute()
            logging.info("🧾 insert_chat_reservation ok (fallback) chat_id=%s folio_id=%s", payload.get("chat_id"), payload.get("folio_id"))
        except Exception as exc2:
            logging.warning("⚠️ No se pudo insertar chat_reservation (fallback): %s", exc2, exc_info=True)


def get_active_chat_reservation(
    *,
    chat_id: str,
    property_id: str | int | None = None,
    instance_id: str | None = None,
    limit: int = 20,
) -> dict | None:
    """
    Devuelve la reserva "activa" para un chat.
    Regla: checkin más próximo >= hoy; si no, la más reciente por updated_at.
    """
    if not chat_id:
        return None

    clean_id = _normalize_chat_id(chat_id)
    try:
        query = (
            supabase.table(Settings.CHAT_RESERVATIONS_TABLE)
            .select("chat_id, folio_id, reservation_locator, checkin, checkout, property_id, instance_id, original_chat_id, source, updated_at")
            .eq("chat_id", clean_id)
            .order("updated_at", desc=True)
            .limit(limit)
        )
        if property_id is not None:
            query = query.eq("property_id", property_id)
        if instance_id:
            query = query.eq("instance_id", instance_id)
        elif isinstance(chat_id, str) and ":" in chat_id:
            # Si es chat compuesto y no hay instance_id, filtra por original_chat_id
            query = query.eq("original_chat_id", chat_id)
        resp = query.execute()
        rows = resp.data or []
    except Exception as exc:
        logging.warning("⚠️ No se pudo leer chat_reservations: %s", exc)
        return None

    if not rows:
        return None

    def _parse_date(val):
        if not val:
            return None
        try:
            return datetime.fromisoformat(str(val)).date()
        except Exception:
            for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
                try:
                    return datetime.strptime(str(val), fmt).date()
                except Exception:
                    continue
        return None

    today = datetime.utcnow().date()
    upcoming = []
    for row in rows:
        ci = _parse_date(row.get("checkin"))
        if ci and ci >= today:
            upcoming.append((ci, row))

    if upcoming:
        upcoming.sort(key=lambda item: item[0])
        return upcoming[0][1]

    # fallback: más reciente por updated_at (ya vienen ordenadas)
    return rows[0]



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
