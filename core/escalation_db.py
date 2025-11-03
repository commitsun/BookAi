import logging
from datetime import datetime
from core.db import supabase  # 👈 reutiliza la conexión ya existente

log = logging.getLogger("EscalationsRepo")

# ======================================================
# 💾 Crear o actualizar una escalación
# ======================================================
def save_escalation(escalation: dict):
    """
    Inserta o actualiza una escalación en la base de datos Supabase.
    Si ya existe (por el mismo escalation_id), se actualiza.
    """
    try:
        supabase.table("escalations").upsert(escalation).execute()
        log.info(f"💾 Escalación {escalation['escalation_id']} guardada/actualizada correctamente.")
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
        return result.data
    except Exception as e:
        log.error(f"⚠️ Error obteniendo escalación {escalation_id}: {e}", exc_info=True)
        return None


# ======================================================
# ✏️ Actualizar campos de una escalación existente
# ======================================================
def update_escalation(escalation_id: str, updates: dict):
    """
    Actualiza los campos de una escalación.
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
# 🧹 Borrar una escalación (opcional, útil para debug)
# ======================================================
def delete_escalation(escalation_id: str):
    """Elimina una escalación específica (por depuración o pruebas)."""
    try:
        supabase.table("escalations").delete().eq("escalation_id", escalation_id).execute()
        log.info(f"🗑️ Escalación {escalation_id} eliminada correctamente.")
    except Exception as e:
        log.error(f"⚠️ Error eliminando escalación {escalation_id}: {e}", exc_info=True)
