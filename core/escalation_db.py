import logging
from datetime import datetime
from core.db import supabase  # ✅ reutiliza la conexión ya existente

log = logging.getLogger("EscalationsDB")

# ======================================================
# 💾 Crear o actualizar una escalación
# ======================================================
def save_escalation(escalation: dict):
    """
    Inserta o actualiza una escalación en la base de datos Supabase.
    Si ya existe (por el mismo escalation_id), se actualiza.
    """
    try:
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
        updates["updated_at"] = datetime.utcnow().isoformat()
        supabase.table("escalations").update(updates).eq("escalation_id", escalation_id).execute()
        log.info(f"🧩 Escalación {escalation_id} actualizada correctamente con {list(updates.keys())}")
    except Exception as e:
        log.error(f"⚠️ Error actualizando escalación {escalation_id}: {e}", exc_info=True)


# ======================================================
# 🧾 Listar escalaciones pendientes de confirmación
# ======================================================
def list_pending_escalations(limit: int = 20):
    """Devuelve las últimas escalaciones sin confirmar (manager_confirmed = false)."""
    try:
        res = (
            supabase.table("escalations")
            .select("*")
            .eq("manager_confirmed", False)
            .order("timestamp", desc=True)
            .limit(limit)
            .execute()
        )
        data = res.data or []
        log.info(f"📋 {len(data)} escalaciones pendientes encontradas.")
        return data
    except Exception as e:
        log.error(f"⚠️ Error listando escalaciones pendientes: {e}", exc_info=True)
        return []


# ======================================================
# 🔎 Última escalación pendiente por chat
# ======================================================
def get_latest_pending_escalation(guest_chat_id: str) -> dict | None:
    """Devuelve la escalación pendiente más reciente para un chat específico."""
    if not guest_chat_id:
        return None
    try:
        raw = str(guest_chat_id).strip()
        clean = "".join(ch for ch in raw if ch.isdigit())
        tail = raw.split(":")[-1].strip() if ":" in raw else ""
        candidates = {raw, clean, tail}
        candidates.discard("")
        like_clause = ""
        if clean:
            like_clause = f"guest_chat_id.like.%:{clean}"
        or_filters = [f"guest_chat_id.eq.{cand}" for cand in candidates]
        if like_clause:
            or_filters.append(like_clause)

        res = (
            supabase.table("escalations")
            .select("*")
            .eq("manager_confirmed", False)
            .or_(",".join(or_filters))
            .order("timestamp", desc=True)
            .limit(1)
            .execute()
        )
        data = res.data or []
        return data[0] if data else None
    except Exception as e:
        log.error(
            "⚠️ Error obteniendo escalación pendiente para %s: %s",
            guest_chat_id,
            e,
            exc_info=True,
        )
        return None


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
