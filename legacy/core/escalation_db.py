import logging
from datetime import datetime
import re
from core.db import supabase  # ✅ reutiliza la conexión ya existente

log = logging.getLogger("EscalationsDB")


# Normaliza el ID de chat del huésped.
# Se usa en el flujo de persistencia y resolución de escalaciones para preparar datos, validaciones o decisiones previas.
# Recibe `value` como entrada principal según la firma.
# Devuelve un `str` con el resultado de esta operación. Sin efectos secundarios relevantes.
def _normalize_guest_chat_id(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if ":" in raw:
        left, right = raw.rsplit(":", 1)
        left_clean = re.sub(r"\D", "", left).strip() or left.strip()
        right_clean = re.sub(r"\D", "", right).strip() or right.strip()
        return f"{left_clean}:{right_clean}".strip(":")
    return re.sub(r"\D", "", raw).strip() or raw

# Inserta o actualiza una escalación en la base de datos Supabase.
# Se usa en el flujo de persistencia y resolución de escalaciones para preparar datos, validaciones o decisiones previas.
# Recibe `escalation` como entrada principal según la firma.
# No devuelve un valor de negocio; deja aplicado el cambio de estado o registro correspondiente. Puede consultar o escribir en base de datos.
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


# Recupera una escalación específica por su ID.
# Se usa en el flujo de persistencia y resolución de escalaciones para preparar datos, validaciones o decisiones previas.
# Recibe `escalation_id` como entrada principal según la firma.
# Devuelve el resultado calculado para que el siguiente paso lo consuma. Puede consultar o escribir en base de datos.
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


# Actualiza los campos de una escalación existente.
# Se usa en el flujo de persistencia y resolución de escalaciones para preparar datos, validaciones o decisiones previas.
# Recibe `escalation_id`, `updates` como entradas relevantes junto con el contexto inyectado en la firma.
# No devuelve un valor de negocio; deja aplicado el cambio de estado o registro correspondiente. Puede consultar o escribir en base de datos.
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


# Devuelve el historial de mensajes (JSONB) de una escalación.
# Se usa en el flujo de persistencia y resolución de escalaciones para preparar datos, validaciones o decisiones previas.
# Recibe `escalation_id` como entrada principal según la firma.
# Devuelve un `list[dict]` con el resultado de esta operación. Puede consultar o escribir en base de datos.
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


# Agrega un mensaje al historial de la escalación y lo persiste en DB.
# Se usa en el flujo de persistencia y resolución de escalaciones para preparar datos, validaciones o decisiones previas.
# Recibe `escalation_id`, `role`, `content`, `timestamp` como entradas relevantes junto con el contexto inyectado en la firma.
# Devuelve un `list[dict]` con el resultado de esta operación. Puede consultar o escribir en base de datos.
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


# Devuelve las últimas escalaciones sin confirmar (manager_confirmed = false).
# Se usa en el flujo de persistencia y resolución de escalaciones para preparar datos, validaciones o decisiones previas.
# Recibe `limit`, `property_id` como entradas relevantes junto con el contexto inyectado en la firma.
# Devuelve el resultado calculado para que el siguiente paso lo consuma. Puede consultar o escribir en base de datos.
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


# Resuelve chat candidates.
# Se usa en el flujo de persistencia y resolución de escalaciones para preparar datos, validaciones o decisiones previas.
# Recibe `guest_chat_id` como entrada principal según la firma.
# Devuelve un `tuple[set[str], str]` con el resultado de esta operación. Sin efectos secundarios relevantes.
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


# Resuelve la consulta de escalations.
# Se usa en el flujo de persistencia y resolución de escalaciones para preparar datos, validaciones o decisiones previas.
# Recibe `guest_chat_id`, `property_id` como entradas relevantes junto con el contexto inyectado en la firma.
# Devuelve el resultado calculado para que el siguiente paso lo consuma. Puede consultar o escribir en base de datos.
def _chat_escalations_query(guest_chat_id: str, property_id=None):
    candidates, clean = _pending_chat_candidates(guest_chat_id)
    like_clause = f"guest_chat_id.like.%:{clean}" if clean else ""
    or_filters = [f"guest_chat_id.eq.{cand}" for cand in candidates]
    if like_clause:
        or_filters.append(like_clause)
    if not or_filters:
        return None
    query = (
        supabase.table("escalations")
        .select("*")
        .or_(",".join(or_filters))
    )
    if property_id is not None:
        query = query.eq("property_id", property_id)
    return query


# Determina si escalation resolved cumple la condición necesaria en este punto del flujo.
# Se usa en el flujo de persistencia y resolución de escalaciones para preparar datos, validaciones o decisiones previas.
# Recibe `escalation` como entrada principal según la firma.
# Devuelve un booleano que gobierna la rama de ejecución siguiente. Sin efectos secundarios relevantes.
def is_escalation_resolved(escalation: dict | None) -> bool:
    if not isinstance(escalation, dict):
        return False
    status = str(escalation.get("status") or "").strip().lower()
    if status == "resolved":
        return True
    if escalation.get("resolved_at"):
        return True
    if bool(escalation.get("manager_confirmed")):
        return True
    if bool(escalation.get("sent_to_guest")):
        return True
    return False


# Recupera último escalation para chat.
# Se usa en el flujo de persistencia y resolución de escalaciones para preparar datos, validaciones o decisiones previas.
# Recibe `guest_chat_id`, `property_id` como entradas relevantes junto con el contexto inyectado en la firma.
# Devuelve un `dict | None` con el resultado de esta operación. Sin efectos secundarios relevantes.
def get_latest_escalation_for_chat(guest_chat_id: str, property_id=None) -> dict | None:
    if not guest_chat_id:
        return None
    try:
        query = _chat_escalations_query(guest_chat_id, property_id=property_id)
        if query is None:
            return None
        res = query.order("updated_at", desc=True).limit(100).execute()
        data = res.data or []
        if data:
            return data[0]
        return None
    except Exception as e:
        log.error(
            "⚠️ Error obteniendo la última escalación para %s: %s",
            guest_chat_id,
            e,
            exc_info=True,
        )
        return None


# Recupera último resolved escalation para chat.
# Se usa en el flujo de persistencia y resolución de escalaciones para preparar datos, validaciones o decisiones previas.
# Recibe `guest_chat_id`, `property_id` como entradas relevantes junto con el contexto inyectado en la firma.
# Devuelve un `dict | None` con el resultado de esta operación. Sin efectos secundarios relevantes.
def get_latest_resolved_escalation_for_chat(guest_chat_id: str, property_id=None) -> dict | None:
    if not guest_chat_id:
        return None
    try:
        query = _chat_escalations_query(guest_chat_id, property_id=property_id)
        if query is None:
            return None
        res = query.order("updated_at", desc=True).limit(100).execute()
        data = res.data or []
        for row in data:
            if is_escalation_resolved(row):
                return row
        return None
    except Exception as e:
        log.error(
            "⚠️ Error obteniendo resolución de escalación para %s: %s",
            guest_chat_id,
            e,
            exc_info=True,
        )
        return None


# Devuelve TODAS las escalaciones pendientes para un chat, ordenadas por antigüedad.
# Se usa en el flujo de persistencia y resolución de escalaciones para preparar datos, validaciones o decisiones previas.
# Recibe `guest_chat_id`, `limit`, `property_id` como entradas relevantes junto con el contexto inyectado en la firma.
# Devuelve un `list[dict]` con el resultado de esta operación. Puede consultar o escribir en base de datos.
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


# Devuelve la escalación pendiente más reciente para un chat específico.
# Se usa en el flujo de persistencia y resolución de escalaciones para preparar datos, validaciones o decisiones previas.
# Recibe `guest_chat_id`, `property_id` como entradas relevantes junto con el contexto inyectado en la firma.
# Devuelve un `dict | None` con el resultado de esta operación. Puede consultar o escribir en base de datos.
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


# Resuelve escalation con resolution.
# Se usa en el flujo de persistencia y resolución de escalaciones para preparar datos, validaciones o decisiones previas.
# Recibe `escalation_id`, `property_id`, `resolution_medium`, `resolution_notes`, ... como entradas relevantes junto con el contexto inyectado en la firma.
# Devuelve un `dict | None` con el resultado de esta operación. Puede consultar o escribir en base de datos.
def resolve_escalation_with_resolution(
    escalation_id: str,
    *,
    property_id=None,
    resolution_medium: str | None = None,
    resolution_notes: str | None = None,
    resolved_at: str | None = None,
    resolved_by: str | int | None = None,
    resolved_by_name: str | None = None,
    resolved_by_email: str | None = None,
) -> dict | None:
    if not escalation_id:
        return None
    updates: dict = {
        "manager_confirmed": True,
        "sent_to_guest": True,
        "resolved_at": resolved_at or datetime.utcnow().isoformat(),
        "resolution_medium": resolution_medium,
        "resolution_notes": (resolution_notes or "") if resolution_notes is not None else "",
        "updated_at": datetime.utcnow().isoformat(),
    }
    if property_id is not None:
        updates["property_id"] = property_id
    if resolved_by is not None and str(resolved_by).strip() != "":
        updates["resolved_by"] = str(resolved_by).strip()
    if resolved_by_name is not None and str(resolved_by_name).strip() != "":
        updates["resolved_by_name"] = str(resolved_by_name).strip()
    if resolved_by_email is not None and str(resolved_by_email).strip() != "":
        updates["resolved_by_email"] = str(resolved_by_email).strip()
    try:
        supabase.table("escalations").update(updates).eq("escalation_id", escalation_id).execute()
        return get_escalation(escalation_id)
    except Exception as e:
        log.error(
            "⚠️ Error resolviendo escalación %s con metadata de resolución: %s",
            escalation_id,
            e,
            exc_info=True,
        )
        return None


# Marca como resueltas TODAS las escalaciones pendientes para un chat.
# Se usa en el flujo de persistencia y resolución de escalaciones para preparar datos, validaciones o decisiones previas.
# Recibe `guest_chat_id`, `final_response`, `property_id` como entradas relevantes junto con el contexto inyectado en la firma.
# Devuelve un `list[str]` con el resultado de esta operación. Puede consultar o escribir en base de datos.
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


# Marca como resuelta la escalación pendiente más reciente para un chat.
# Se usa en el flujo de persistencia y resolución de escalaciones para preparar datos, validaciones o decisiones previas.
# Recibe `guest_chat_id`, `final_response` como entradas relevantes junto con el contexto inyectado en la firma.
# Devuelve un `str | None` con el resultado de esta operación. Puede consultar o escribir en base de datos.
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


# Elimina una escalación específica (por depuración o pruebas).
# Se usa en el flujo de persistencia y resolución de escalaciones para preparar datos, validaciones o decisiones previas.
# Recibe `escalation_id` como entrada principal según la firma.
# No devuelve un valor de negocio; deja aplicado el cambio de estado o registro correspondiente. Puede consultar o escribir en base de datos.
def delete_escalation(escalation_id: str):
    """Elimina una escalación específica (por depuración o pruebas)."""
    try:
        supabase.table("escalations").delete().eq("escalation_id", escalation_id).execute()
        log.info(f"🗑️ Escalación {escalation_id} eliminada correctamente.")
    except Exception as e:
        log.error(f"⚠️ Error eliminando escalación {escalation_id}: {e}", exc_info=True)
