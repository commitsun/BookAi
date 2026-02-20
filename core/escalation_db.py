import logging
from datetime import datetime
import re
from core.db import supabase  # ✅ reutiliza la conexión ya existente

log = logging.getLogger("EscalationsDB")


def _normalize_guest_chat_id(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if ":" in raw:
        left, right = raw.split(":", 1)
        left_clean = re.sub(r"\D", "", left).strip() or left.strip()
        right_clean = re.sub(r"\D", "", right).strip() or right.strip()
        return f"{left_clean}:{right_clean}".strip(":")
    return re.sub(r"\D", "", raw).strip() or raw

# ======================================================
# 💾 Crear o actualizar una escalación
# ======================================================
def save_escalation(escalation: dict):
    """
    Inserta o actualiza una escalación en la base de datos Supabase.
    Si ya existe (por el mismo escalation_id), se actualiza.
    """
    try:
        if isinstance(escalation, dict) and escalation.get("guest_chat_id"):
            escalation["guest_chat_id"] = _normalize_guest_chat_id(escalation.get("guest_chat_id"))
        # Garantiza que siempre haya un timestamp
        escalation["updated_at"] = datetime.utcnow().isoformat()
        supabase.table("escalations").upsert(escalation).execute()
        log.info(f"💾 Escalación {escalation.get('escalation_id')} guardada/actualizada correctamente.")
    except Exception as e:
        log.error(f"⚠️ Error guardando escalación {escalation.get('escalation_id')}: {e}", exc_info=True)


# ======================================================
# 🔍 Obtener una escalación por ID
# ======================================================
def get_escalation(escalation_id: str):
    """Recupera una escalación específica por su ID."""
    try:
        result = (
            supabase.table("escalations")
            .select("*")
            .eq("escalation_id", escalation_id)
            .single()
            .execute()
        )
        data = result.data
        if not data:
            log.warning(f"⚠️ Escalación {escalation_id} no encontrada en la base de datos.")
        return data
    except Exception as e:
        log.error(f"⚠️ Error obteniendo escalación {escalation_id}: {e}", exc_info=True)
        return None


# ======================================================
# ✏️ Actualizar campos de una escalación existente
# ======================================================
def update_escalation(escalation_id: str, updates: dict):
    """
    Actualiza los campos de una escalación existente.
    Ejemplo:
        update_escalation("esc_34683527049_1762168364", {"draft_response": "Texto actualizado"})
    """
    try:
        if isinstance(updates, dict) and updates.get("guest_chat_id"):
            updates["guest_chat_id"] = _normalize_guest_chat_id(updates.get("guest_chat_id"))
        updates["updated_at"] = datetime.utcnow().isoformat()
        supabase.table("escalations").update(updates).eq("escalation_id", escalation_id).execute()
        log.info(f"🧩 Escalación {escalation_id} actualizada correctamente con {list(updates.keys())}")
    except Exception as e:
        log.error(f"⚠️ Error actualizando escalación {escalation_id}: {e}", exc_info=True)


# ======================================================
# 💬 Historial de mensajes de escalación
# ======================================================
def get_escalation_messages(escalation_id: str) -> list[dict]:
    """Devuelve el historial de mensajes (JSONB) de una escalación."""
    if not escalation_id:
        return []
    try:
        result = (
            supabase.table("escalations")
            .select("messages")
            .eq("escalation_id", escalation_id)
            .single()
            .execute()
        )
        data = result.data or {}
        messages = data.get("messages") or []
        return messages if isinstance(messages, list) else []
    except Exception as e:
        log.error(
            "⚠️ Error obteniendo mensajes de escalación %s: %s",
            escalation_id,
            e,
            exc_info=True,
        )
        return []


def append_escalation_message(
    escalation_id: str,
    role: str,
    content: str,
    timestamp: str | None = None,
) -> list[dict]:
    """Agrega un mensaje al historial de la escalación y lo persiste en DB."""
    if not escalation_id or not content:
        return []
    messages = get_escalation_messages(escalation_id)
    messages.append(
        {
            "role": role,
            "content": content,
            "timestamp": timestamp or datetime.utcnow().isoformat(),
        }
    )
    try:
        update_escalation(escalation_id, {"messages": messages})
    except Exception:
        # update_escalation ya loguea el error
        pass
    return messages


# ======================================================
# 🧾 Listar escalaciones pendientes de confirmación
# ======================================================
def list_pending_escalations(limit: int = 20, property_id=None):
    """Devuelve las últimas escalaciones sin confirmar (manager_confirmed = false)."""
    try:
        query = (
            supabase.table("escalations")
            .select("*")
            .eq("manager_confirmed", False)
        )
        if property_id is not None:
            query = query.eq("property_id", property_id)
        res = query.order("timestamp", desc=True).limit(limit).execute()
        data = [row for row in (res.data or []) if not bool((row or {}).get("sent_to_guest"))]
        log.info(f"📋 {len(data)} escalaciones pendientes encontradas.")
        return data
    except Exception as e:
        log.error(f"⚠️ Error listando escalaciones pendientes: {e}", exc_info=True)
        return []


def _pending_chat_candidates(guest_chat_id: str) -> tuple[set[str], str]:
    raw = str(guest_chat_id or "").strip()
    normalized = _normalize_guest_chat_id(raw)
    tail = raw.split(":")[-1].strip() if ":" in raw else raw
    tail_clean = re.sub(r"\D", "", tail).strip() or tail
    clean = tail_clean
    candidates = {raw, normalized, tail, tail_clean}
    if ":" in normalized:
        candidates.add(normalized)
    candidates.discard("")
    return candidates, clean


def list_pending_escalations_for_chat(guest_chat_id: str, limit: int = 20, property_id=None) -> list[dict]:
    """Devuelve TODAS las escalaciones pendientes para un chat, ordenadas por antigüedad."""
    if not guest_chat_id:
        return []
    try:
        candidates, clean = _pending_chat_candidates(guest_chat_id)
        like_clause = f"guest_chat_id.like.%:{clean}" if clean else ""
        or_filters = [f"guest_chat_id.eq.{cand}" for cand in candidates]
        if like_clause:
            or_filters.append(like_clause)
        if not or_filters:
            return []
        query = (
            supabase.table("escalations")
            .select("*")
            .eq("manager_confirmed", False)
            .or_(",".join(or_filters))
        )
        if property_id is not None:
            query = query.eq("property_id", property_id)
        res = query.order("timestamp", desc=False).limit(max(limit, 50)).execute()
        data = [row for row in (res.data or []) if not bool((row or {}).get("sent_to_guest"))]
        return data[:limit]
    except Exception as e:
        log.error(
            "⚠️ Error listando escalaciones pendientes para %s: %s",
            guest_chat_id,
            e,
            exc_info=True,
        )
        return []


# ======================================================
# 🔎 Última escalación pendiente por chat
# ======================================================
def get_latest_pending_escalation(guest_chat_id: str, property_id=None) -> dict | None:
    """Devuelve la escalación pendiente más reciente para un chat específico."""
    if not guest_chat_id:
        return None
    try:
        candidates, clean = _pending_chat_candidates(guest_chat_id)
        like_clause = f"guest_chat_id.like.%:{clean}" if clean else ""
        or_filters = [f"guest_chat_id.eq.{cand}" for cand in candidates]
        if like_clause:
            or_filters.append(like_clause)

        query = (
            supabase.table("escalations")
            .select("*")
            .eq("manager_confirmed", False)
            .or_(",".join(or_filters))
        )
        if property_id is not None:
            query = query.eq("property_id", property_id)
        res = query.order("timestamp", desc=True).limit(20).execute()
        data = res.data or []
        for row in data:
            if not bool((row or {}).get("sent_to_guest")):
                return row
        return None
    except Exception as e:
        log.error(
            "⚠️ Error obteniendo escalación pendiente para %s: %s",
            guest_chat_id,
            e,
            exc_info=True,
        )
        return None


def resolve_pending_escalations_for_chat(
    guest_chat_id: str,
    final_response: str | None = None,
    property_id=None,
) -> list[str]:
    """Marca como resueltas TODAS las escalaciones pendientes para un chat."""
    pending = list_pending_escalations_for_chat(guest_chat_id, limit=100, property_id=property_id)
    resolved_ids: list[str] = []
    for esc in pending:
        escalation_id = str(esc.get("escalation_id") or "").strip()
        if not escalation_id:
            continue
        updates = {
            "manager_confirmed": True,
            "sent_to_guest": True,
        }
        if final_response:
            updates["final_response"] = final_response
        update_escalation(escalation_id, updates)
        resolved_ids.append(escalation_id)
    return resolved_ids


# ======================================================
# ✅ Resolver escalación pendiente por chat
# ======================================================
def resolve_latest_pending_escalation(guest_chat_id: str, final_response: str | None = None) -> str | None:
    """Marca como resuelta la escalación pendiente más reciente para un chat."""
    esc = get_latest_pending_escalation(guest_chat_id)
    if not esc:
        return None
    escalation_id = esc.get("escalation_id")
    if not escalation_id:
        return None
    updates = {
        "manager_confirmed": True,
        "sent_to_guest": True,
    }
    if final_response:
        updates["final_response"] = final_response
    update_escalation(str(escalation_id), updates)
    return str(escalation_id)


# ======================================================
# 🧹 Borrar una escalación (opcional, útil para debug)
# ======================================================
def delete_escalation(escalation_id: str):
    """Elimina una escalación específica (por depuración o pruebas)."""
    try:
        supabase.table("escalations").delete().eq("escalation_id", escalation_id).execute()
        log.info(f"🗑️ Escalación {escalation_id} eliminada correctamente.")
    except Exception as e:
        log.error(f"⚠️ Error eliminando escalación {escalation_id}: {e}", exc_info=True)
