import os
import logging
import re
import json
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


def _is_internal_non_persistable_message(content: str) -> bool:
    text = (content or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered.startswith("salida modelo:"):
        return True
    if "api debug" in lowered:
        return True
    if "sender (api):" in lowered and "chat id:" in lowered:
        return True
    return False


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
        if _is_internal_non_persistable_message(content):
            logging.info("🧹 Mensaje interno omitido (no persistido en chat_history).")
            return

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
            state_query = supabase.table(table).select("archived_at, hidden_at")
            state_rows = []
            if property_id is not None:
                state_rows = (
                    state_query
                    .eq("conversation_id", clean_id)
                    .eq("property_id", property_id)
                    .order("created_at", desc=True)
                    .limit(1)
                    .execute()
                    .data
                    or []
                )
            if not state_rows and original_clean:
                state_rows = (
                    supabase.table(table)
                    .select("archived_at, hidden_at")
                    .eq("original_chat_id", original_clean)
                    .order("created_at", desc=True)
                    .limit(1)
                    .execute()
                    .data
                    or []
                )
            if not state_rows:
                state_rows = (
                    supabase.table(table)
                    .select("archived_at, hidden_at")
                    .eq("conversation_id", clean_id)
                    .order("created_at", desc=True)
                    .limit(1)
                    .execute()
                    .data
                    or []
                )
            if state_rows:
                archived_at = state_rows[0].get("archived_at")
                hidden_at = state_rows[0].get("hidden_at")
                if archived_at is not None:
                    data["archived_at"] = archived_at
                if hidden_at is not None:
                    data["hidden_at"] = hidden_at
        except Exception:
            pass

        try:
            supabase.table(table).insert(data).execute()
        except Exception as exc:
            err = str(exc).lower()
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
            # Conservar property_id salvo error explícito de esa columna.
            if "property_id" in data and "property_id" in err:
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


def is_chat_visible_in_list(
    conversation_id: str,
    *,
    property_id: str | int | None,
    channel: str | None = None,
    original_chat_id: str | None = None,
) -> bool:
    """
    Replica la lógica efectiva del listado de chats:
    - Debe existir fila en chat_last_message para conversation_id+property_id+channel
    - No puede haber historial marcado como archived/hidden para ese chat/property
    """
    clean_id = str(conversation_id or "").replace("+", "").strip()
    if not clean_id or property_id is None:
        return False

    current_channel = str(channel or "whatsapp").strip() or "whatsapp"
    original_clean = str(original_chat_id or "").replace("+", "").strip()

    try:
        summary_rows = (
            supabase.table("chat_last_message")
            .select("conversation_id, original_chat_id")
            .eq("conversation_id", clean_id)
            .eq("property_id", property_id)
            .eq("channel", current_channel)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
        if not summary_rows and original_clean:
            summary_rows = (
                supabase.table("chat_last_message")
                .select("conversation_id, original_chat_id")
                .eq("original_chat_id", original_clean)
                .eq("property_id", property_id)
                .eq("channel", current_channel)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
                .data
                or []
            )
        if not summary_rows:
            return False

        summary_row = summary_rows[0] or {}
        effective_original = str(summary_row.get("original_chat_id") or original_clean or "").replace("+", "").strip()

        hidden_by_chat = (
            supabase.table("chat_history")
            .select("conversation_id")
            .eq("conversation_id", clean_id)
            .eq("property_id", property_id)
            .eq("channel", current_channel)
            .or_("archived_at.not.is.null,hidden_at.not.is.null")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
        if hidden_by_chat:
            return False

        if effective_original:
            hidden_by_original = (
                supabase.table("chat_history")
                .select("original_chat_id")
                .eq("original_chat_id", effective_original)
                .eq("property_id", property_id)
                .eq("channel", current_channel)
                .or_("archived_at.not.is.null,hidden_at.not.is.null")
                .order("created_at", desc=True)
                .limit(1)
                .execute()
                .data
                or []
            )
            if hidden_by_original:
                return False

        return True
    except Exception as exc:
        logging.debug("No se pudo resolver visibilidad en listado para %s/%s: %s", clean_id, property_id, exc)
        return False


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


def _normalize_chat_id(value: str) -> str:
    raw = str(value or "").replace("+", "").strip()
    digits = re.sub(r"\D", "", raw)
    return digits or raw


def _load_structured_payload(raw_payload):
    if isinstance(raw_payload, str):
        try:
            return json.loads(raw_payload)
        except Exception:
            return raw_payload
    return raw_payload


def _structured_payload_container(raw_payload) -> dict:
    parsed = _load_structured_payload(raw_payload)
    if isinstance(parsed, dict):
        return dict(parsed)
    if parsed in (None, ""):
        return {}
    return {"legacy_structured_payload": parsed}


def _get_meta_whatsapp_payload(raw_payload) -> dict:
    container = _structured_payload_container(raw_payload)
    nested = container.get("meta_whatsapp")
    if isinstance(nested, dict):
        return dict(nested)
    if str(container.get("provider") or "").strip().lower() == "meta_whatsapp":
        return dict(container)
    return {}


def _upsert_meta_whatsapp_payload(raw_payload, meta_payload: dict) -> dict:
    container = _structured_payload_container(raw_payload)
    existing = _get_meta_whatsapp_payload(container)
    merged = dict(existing)
    merged.update(meta_payload or {})
    container["meta_whatsapp"] = merged
    return container


def _normalize_storage_role(role: str) -> str:
    normalized = str(role or "").strip().lower()
    if normalized in {"assistant", "system", "tool"}:
        return "bookai"
    if normalized in {"guest", "user", "bookai"}:
        return normalized
    return "bookai"


def _collect_meta_whatsapp_wamids(meta_payload) -> list[str]:
    if not isinstance(meta_payload, dict):
        return []
    values: list[str] = []
    single = str(meta_payload.get("wamid") or "").strip()
    if single:
        values.append(single)
    raw_many = meta_payload.get("wamids")
    if isinstance(raw_many, (list, tuple)):
        for item in raw_many:
            text = str(item or "").strip()
            if text:
                values.append(text)
    receipts = meta_payload.get("delivery_receipts")
    if isinstance(receipts, dict):
        for key in receipts.keys():
            text = str(key or "").strip()
            if text:
                values.append(text)
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def _aggregate_meta_whatsapp_status(receipts: dict) -> str:
    if not isinstance(receipts, dict) or not receipts:
        return "pending"
    statuses = {
        str((item or {}).get("delivery_status") or "").strip().lower()
        for item in receipts.values()
        if isinstance(item, dict)
    }
    if "failed" in statuses:
        return "failed"
    if "read" in statuses:
        return "read"
    if "delivered" in statuses:
        return "delivered"
    if "sent" in statuses:
        return "sent"
    return "pending"


def _normalize_meta_whatsapp_results(provider_result) -> list[dict]:
    if provider_result is None:
        return []
    if isinstance(provider_result, dict):
        return [provider_result]
    if isinstance(provider_result, list):
        return [item for item in provider_result if isinstance(item, dict)]
    return []


def _is_no_whatsapp_details(details: str | None) -> bool:
    text = str(details or "").strip().lower()
    return "phone number not on whatsapp" in text


def _is_internal_chatter_content(content: str | None) -> bool:
    text = str(content or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered.startswith("[superintendente]"):
        return True
    if lowered.startswith("contexto de propiedad actualizado"):
        return True
    if lowered.startswith("[template_sent]"):
        return True
    if lowered.startswith("salida modelo:"):
        return True
    if "api debug" in lowered:
        return True
    if "sender (api):" in lowered and "chat id:" in lowered:
        return True
    return False


def is_meta_whatsapp_outbound_hidden(
    structured_payload,
    *,
    channel: str | None = None,
) -> bool:
    current_channel = str(channel or "whatsapp").strip().lower() or "whatsapp"
    if current_channel != "whatsapp":
        return False
    wa_payload = _get_meta_whatsapp_payload(structured_payload)
    if not wa_payload:
        return False
    return wa_payload.get("chatter_visible") is False


def _select_recent_chat_history_rows(
    *,
    conversation_id: str,
    channel: str = "whatsapp",
    original_chat_id: str | None = None,
    property_id: str | int | None = None,
    table: str = "chat_history",
    limit: int = 100,
) -> list[dict]:
    clean_id = _normalize_chat_id(conversation_id)
    original_clean = str(original_chat_id or "").replace("+", "").strip()
    if not clean_id and not original_clean:
        return []

    select_fields = (
        "id, conversation_id, original_chat_id, property_id, role, content, created_at, "
        "channel, client_name, read_status, structured_payload"
    )

    def _run_query(use_original: bool) -> list[dict]:
        query = (
            supabase.table(table)
            .select(select_fields)
            .eq("channel", str(channel or "whatsapp").strip() or "whatsapp")
        )
        if property_id is not None:
            query = query.eq("property_id", property_id)
        if use_original and original_clean:
            query = query.eq("original_chat_id", original_clean)
        else:
            query = query.eq("conversation_id", clean_id)
        return (
            query.order("created_at", desc=True)
            .limit(limit)
            .execute()
            .data
            or []
        )

    try:
        rows = _run_query(use_original=bool(original_clean))
        if not rows and original_clean and clean_id:
            rows = _run_query(use_original=False)
        return rows
    except Exception as exc:
        logging.warning("⚠️ No se pudo leer historial reciente para WA visibility: %s", exc)
        return []


def _find_latest_chat_history_row(
    *,
    conversation_id: str,
    content: str | None = None,
    role: str = "bookai",
    channel: str = "whatsapp",
    original_chat_id: str | None = None,
    table: str = "chat_history",
    limit: int = 25,
) -> dict | None:
    rows = _select_recent_chat_history_rows(
        conversation_id=conversation_id,
        channel=channel,
        original_chat_id=original_chat_id,
        table=table,
        limit=limit,
    )
    if not rows:
        return None

    role_norm = _normalize_storage_role(role)
    content_text = str(content or "").strip()
    candidate_rows = []
    for row in rows:
        row_role = _normalize_storage_role(row.get("role") or "")
        if row_role != role_norm:
            continue
        if content_text and str(row.get("content") or "").strip() != content_text:
            continue
        candidate_rows.append(row)

    if not candidate_rows:
        for row in rows:
            row_role = _normalize_storage_role(row.get("role") or "")
            if row_role == role_norm:
                candidate_rows.append(row)
                break

    return candidate_rows[0] if candidate_rows else None


def set_meta_whatsapp_pending_and_hidden(
    *,
    conversation_id: str,
    content: str | None = None,
    role: str = "bookai",
    channel: str = "whatsapp",
    original_chat_id: str | None = None,
    table: str = "chat_history",
) -> dict:
    result = {"updated": False, "row": None}
    target = _find_latest_chat_history_row(
        conversation_id=conversation_id,
        content=content,
        role=role,
        channel=channel,
        original_chat_id=original_chat_id,
        table=table,
    )
    if not target:
        return result

    row_id = target.get("id")
    if row_id is None:
        return result

    current_payload = target.get("structured_payload")
    wa_payload = _get_meta_whatsapp_payload(current_payload)
    now_iso = datetime.utcnow().isoformat()
    current_status = str(wa_payload.get("delivery_status") or "").strip().lower() or "pending"
    merged_payload = _upsert_meta_whatsapp_payload(
        current_payload,
        {
            "provider": "meta_whatsapp",
            "delivery_status": current_status,
            "chatter_visible": False,
            "updated_at": now_iso,
        },
    )

    try:
        (
            supabase.table(table)
            .update({"structured_payload": merged_payload})
            .eq("id", row_id)
            .execute()
        )
        target = dict(target)
        target["structured_payload"] = merged_payload
        result["updated"] = True
        result["row"] = target
        return result
    except Exception as exc:
        logging.warning("⚠️ No se pudo marcar outbound WA como pending/hidden: %s", exc)
        return result


def attach_meta_whatsapp_outbound_to_latest_message(
    *,
    conversation_id: str,
    provider_result,
    content: str | None = None,
    role: str = "bookai",
    channel: str = "whatsapp",
    original_chat_id: str | None = None,
    table: str = "chat_history",
) -> bool:
    clean_id = _normalize_chat_id(conversation_id)
    results = _normalize_meta_whatsapp_results(provider_result)
    if not clean_id or not results:
        return False

    wamids = [
        str(item.get("wamid") or "").strip()
        for item in results
        if str(item.get("wamid") or "").strip()
    ]
    if not wamids:
        return False

    target = _find_latest_chat_history_row(
        conversation_id=clean_id,
        content=content,
        role=role,
        channel=channel,
        original_chat_id=original_chat_id,
        table=table,
    )
    if not target:
        return False
    row_id = target.get("id")
    if row_id is None:
        return False

    current_payload = target.get("structured_payload")
    wa_payload = _get_meta_whatsapp_payload(current_payload)
    current_wamids = _collect_meta_whatsapp_wamids(wa_payload)
    receipts = dict(wa_payload.get("delivery_receipts") or {}) if isinstance(wa_payload.get("delivery_receipts"), dict) else {}
    now_iso = datetime.utcnow().isoformat()

    for item in results:
        wamid = str(item.get("wamid") or "").strip()
        if not wamid:
            continue
        if wamid not in current_wamids:
            current_wamids.append(wamid)
        receipt = dict(receipts.get(wamid) or {})
        receipt.update(
            {
                "delivery_status": str(item.get("delivery_status") or receipt.get("delivery_status") or "pending").strip().lower() or "pending",
                "message_type": str(item.get("message_type") or receipt.get("message_type") or "").strip() or None,
                "recipient_id": str(item.get("recipient_id") or receipt.get("recipient_id") or clean_id).strip() or clean_id,
                "updated_at": now_iso,
            }
        )
        receipts[wamid] = {key: value for key, value in receipt.items() if value is not None}

    merged_payload = _upsert_meta_whatsapp_payload(
        current_payload,
        {
            "provider": "meta_whatsapp",
            "wamid": current_wamids[0] if current_wamids else None,
            "wamids": current_wamids,
            "delivery_status": _aggregate_meta_whatsapp_status(receipts),
            "delivery_receipts": receipts,
            "chatter_visible": wa_payload.get("chatter_visible") if wa_payload.get("chatter_visible") is not None else False,
            "updated_at": now_iso,
        },
    )

    try:
        (
            supabase.table(table)
            .update({"structured_payload": merged_payload})
            .eq("id", row_id)
            .execute()
        )
        return True
    except Exception as exc:
        logging.warning("⚠️ No se pudo adjuntar metadata WA al mensaje: %s", exc)
        return False


def update_meta_whatsapp_status_by_wamid(
    *,
    wamid: str,
    recipient_id: str | None = None,
    delivery_status: str,
    error_code: str | int | None = None,
    error_details: str | None = None,
    channel: str = "whatsapp",
    table: str = "chat_history",
) -> dict:
    result = {
        "updated": False,
        "conversation_id": _normalize_chat_id(recipient_id),
        "original_chat_id": None,
        "no_whatsapp": _is_no_whatsapp_details(error_details),
    }

    wamid_text = str(wamid or "").strip()
    clean_id = _normalize_chat_id(recipient_id)
    status_text = str(delivery_status or "").strip().lower() or "pending"
    if not wamid_text or not clean_id:
        return result

    try:
        rows = (
            supabase.table(table)
            .select("id, role, original_chat_id, structured_payload")
            .eq("conversation_id", clean_id)
            .eq("channel", channel)
            .order("created_at", desc=True)
            .limit(100)
            .execute()
            .data
            or []
        )
    except Exception as exc:
        logging.warning("⚠️ No se pudo leer historial para actualizar status WA: %s", exc)
        return result

    target = None
    for row in rows:
        wa_payload = _get_meta_whatsapp_payload(row.get("structured_payload"))
        if wamid_text in _collect_meta_whatsapp_wamids(wa_payload):
            target = row
            break

    if target is None and rows and result["no_whatsapp"]:
        for row in rows:
            if _normalize_storage_role(row.get("role") or "") in {"bookai", "user"}:
                target = row
                break

    if target is None:
        return result

    row_id = target.get("id")
    if row_id is None:
        return result

    current_payload = target.get("structured_payload")
    wa_payload = _get_meta_whatsapp_payload(current_payload)
    current_wamids = _collect_meta_whatsapp_wamids(wa_payload)
    if wamid_text not in current_wamids:
        current_wamids.append(wamid_text)
    receipts = dict(wa_payload.get("delivery_receipts") or {}) if isinstance(wa_payload.get("delivery_receipts"), dict) else {}
    now_iso = datetime.utcnow().isoformat()

    receipt = dict(receipts.get(wamid_text) or {})
    receipt.update(
        {
            "delivery_status": status_text,
            "recipient_id": clean_id,
            "updated_at": now_iso,
        }
    )
    if error_code is not None or error_details:
        receipt["delivery_error"] = {
            "code": str(error_code or "").strip() or None,
            "details": str(error_details or "").strip() or None,
        }
    receipts[wamid_text] = {key: value for key, value in receipt.items() if value is not None}

    meta_update = {
        "provider": "meta_whatsapp",
        "wamid": current_wamids[0] if current_wamids else wamid_text,
        "wamids": current_wamids,
        "delivery_status": _aggregate_meta_whatsapp_status(receipts),
        "delivery_receipts": receipts,
        "updated_at": now_iso,
    }
    if error_code is not None or error_details:
        meta_update["delivery_error"] = {
            "code": str(error_code or "").strip() or None,
            "details": str(error_details or "").strip() or None,
        }
    if result["no_whatsapp"]:
        meta_update["no_whatsapp"] = True
        meta_update["no_whatsapp_detected_at"] = now_iso

    merged_payload = _upsert_meta_whatsapp_payload(current_payload, meta_update)

    try:
        (
            supabase.table(table)
            .update({"structured_payload": merged_payload})
            .eq("id", row_id)
            .execute()
        )
        result["updated"] = True
        result["original_chat_id"] = str(target.get("original_chat_id") or "").strip() or None
        return result
    except Exception as exc:
        logging.warning("⚠️ No se pudo actualizar delivery status WA: %s", exc)
        return result


def set_meta_whatsapp_visible_by_wamid_if_hidden(
    *,
    wamid: str,
    recipient_id: str | None = None,
    channel: str = "whatsapp",
    table: str = "chat_history",
) -> dict:
    result = {
        "changed": False,
        "row": None,
        "conversation_id": _normalize_chat_id(recipient_id),
        "original_chat_id": None,
    }

    wamid_text = str(wamid or "").strip()
    clean_id = _normalize_chat_id(recipient_id)
    if not wamid_text:
        return result

    rows: list[dict]
    if clean_id:
        rows = _select_recent_chat_history_rows(
            conversation_id=clean_id,
            channel=channel,
            table=table,
            limit=100,
        )
    else:
        try:
            rows = (
                supabase.table(table)
                .select(
                    "id, conversation_id, original_chat_id, property_id, role, content, created_at, "
                    "channel, client_name, read_status, structured_payload"
                )
                .eq("channel", str(channel or "whatsapp").strip() or "whatsapp")
                .order("created_at", desc=True)
                .limit(200)
                .execute()
                .data
                or []
            )
        except Exception as exc:
            logging.warning("⚠️ No se pudo leer historial para liberar mensaje WA: %s", exc)
            return result

    target = None
    for row in rows:
        wa_payload = _get_meta_whatsapp_payload(row.get("structured_payload"))
        if wamid_text in _collect_meta_whatsapp_wamids(wa_payload):
            target = row
            break

    if target is None:
        return result

    result["row"] = dict(target)
    result["conversation_id"] = str(target.get("conversation_id") or "").strip() or clean_id
    result["original_chat_id"] = str(target.get("original_chat_id") or "").strip() or None

    row_id = target.get("id")
    if row_id is None:
        return result

    current_payload = target.get("structured_payload")
    wa_payload = _get_meta_whatsapp_payload(current_payload)
    if wa_payload.get("chatter_visible") is not False:
        return result

    now_iso = datetime.utcnow().isoformat()
    merged_payload = _upsert_meta_whatsapp_payload(
        current_payload,
        {
            "provider": "meta_whatsapp",
            "chatter_visible": True,
            "released_to_chatter_at": now_iso,
            "updated_at": now_iso,
        },
    )

    try:
        (
            supabase.table(table)
            .update({"structured_payload": merged_payload})
            .eq("id", row_id)
            .execute()
        )
        updated_row = dict(target)
        updated_row["structured_payload"] = merged_payload
        result["changed"] = True
        result["row"] = updated_row
        return result
    except Exception as exc:
        logging.warning("⚠️ No se pudo liberar mensaje WA al chatter: %s", exc)
        return result


def query_last_visible_message_for_chat(
    *,
    conversation_id: str,
    property_id: str | int | None = None,
    original_chat_id: str | None = None,
    channel: str = "whatsapp",
    table: str = "chat_history",
    limit: int = 100,
) -> dict:
    result = {"row": None, "latest_row_hidden": False}
    rows = _select_recent_chat_history_rows(
        conversation_id=conversation_id,
        property_id=property_id,
        original_chat_id=original_chat_id,
        channel=channel,
        table=table,
        limit=limit,
    )
    if not rows:
        return result

    for index, row in enumerate(rows):
        is_hidden = is_meta_whatsapp_outbound_hidden(
            row.get("structured_payload"),
            channel=row.get("channel") or channel,
        ) or _is_internal_chatter_content(row.get("content"))
        if index == 0 and is_hidden:
            result["latest_row_hidden"] = True
        if is_hidden:
            continue
        result["row"] = row
        return result

    return result


def is_whatsapp_number_marked_no_whatsapp(
    conversation_id: str,
    *,
    channel: str = "whatsapp",
    table: str = "chat_history",
) -> bool:
    clean_id = _normalize_chat_id(conversation_id)
    if not clean_id:
        return False
    try:
        rows = (
            supabase.table(table)
            .select("structured_payload")
            .eq("conversation_id", clean_id)
            .eq("channel", channel)
            .order("created_at", desc=True)
            .limit(50)
            .execute()
            .data
            or []
        )
    except Exception as exc:
        logging.warning("⚠️ No se pudo comprobar bloqueo no_whatsapp: %s", exc)
        return False

    for row in rows:
        wa_payload = _get_meta_whatsapp_payload(row.get("structured_payload"))
        if not wa_payload:
            continue
        if bool(wa_payload.get("no_whatsapp")):
            return True
        delivery_error = wa_payload.get("delivery_error")
        if isinstance(delivery_error, dict) and _is_no_whatsapp_details(delivery_error.get("details")):
            return True
        receipts = wa_payload.get("delivery_receipts")
        if isinstance(receipts, dict):
            for item in receipts.values():
                if not isinstance(item, dict):
                    continue
                error_payload = item.get("delivery_error")
                if isinstance(error_payload, dict) and _is_no_whatsapp_details(error_payload.get("details")):
                    return True
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
    client_name: str | None = None,
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
    if client_name:
        payload["client_name"] = str(client_name).strip()
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
        if "client_name" in payload and "client_name" in str(exc):
            logging.warning("⚠️ Columna client_name no disponible en chat_reservations; reintentando sin client_name.")
            payload.pop("client_name", None)
            try:
                supabase.table(Settings.CHAT_RESERVATIONS_TABLE).upsert(
                    payload,
                    on_conflict="chat_id,folio_id",
                ).execute()
                return
            except Exception:
                pass
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
            .select("chat_id, folio_id, reservation_locator, client_name, checkin, checkout, property_id, instance_id, original_chat_id, source, updated_at")
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
