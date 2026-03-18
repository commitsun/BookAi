"""
Gestor global de tracking de escalaciones
=========================================
Centraliza el mapeo message_id ↔ escalation_id, y además lo sincroniza
con la base de datos Supabase para evitar pérdida tras reinicios.

Usado por:
- tools/interno_tool.py → register_escalation()
- main.py (webhook Telegram) → get_escalation()
"""

import logging
import threading
from datetime import datetime
from core.db import supabase  # ✅ Ajusta si tu cliente se llama distinto

log = logging.getLogger("EscalationManager")

# =============================================================
# ALMACENAMIENTO EN MEMORIA
# =============================================================

ESCALATION_TRACKING = {}
_lock = threading.Lock()

# =============================================================
# FUNCIONES PÚBLICAS
# =============================================================

# Registra una nueva relación entre el mensaje de Telegram y la escalación interna.
# Se usa en el flujo de tracking en memoria de escalaciones por mensaje para preparar datos, validaciones o decisiones previas.
# Recibe `message_id`, `escalation_id` como entradas relevantes junto con el contexto inyectado en la firma.
# No devuelve un valor de negocio; deja aplicado el cambio de estado o registro correspondiente. Puede consultar o escribir en base de datos.
def register_escalation(message_id: str, escalation_id: str):
    """
    Registra una nueva relación entre el mensaje de Telegram y la escalación interna.
    Se guarda en memoria y también en Supabase (columna telegram_message_id).
    """
    message_id = str(message_id)
    escalation_id = str(escalation_id)

    with _lock:
        ESCALATION_TRACKING[message_id] = escalation_id

    log.info(f"📍 Escalation registrada en memoria: message_id={message_id} → escalation_id={escalation_id}")

    # Guardar también en Supabase
    try:
        supabase.table("escalations").update({
            "telegram_message_id": message_id,
            "updated_at": datetime.utcnow().isoformat()
        }).eq("escalation_id", escalation_id).execute()
        log.info(f"💾 Vinculado en DB: {message_id} → {escalation_id}")
    except Exception as e:
        log.error(f"❌ Error guardando vinculación en DB: {e}")


# Recupera el escalation_id asociado a un message_id de Telegram.
# Se usa en el flujo de tracking en memoria de escalaciones por mensaje para preparar datos, validaciones o decisiones previas.
# Recibe `message_id` como entrada principal según la firma.
# Devuelve un `str | None` con el resultado de esta operación. Puede consultar o escribir en base de datos.
def get_escalation(message_id: str) -> str | None:
    """
    Recupera el escalation_id asociado a un message_id de Telegram.
    Primero busca en memoria, luego en Supabase si no lo encuentra.
    """
    message_id = str(message_id)

    # 🔎 Intentar primero desde memoria
    with _lock:
        esc_id = ESCALATION_TRACKING.get(message_id)
    if esc_id:
        return esc_id

    # 🔎 Buscar en Supabase si no está en memoria
    try:
        res = supabase.table("escalations")\
            .select("escalation_id")\
            .eq("telegram_message_id", message_id)\
            .limit(1)\
            .execute()
        data = res.data or []
        if data:
            esc_id = data[0]["escalation_id"]
            # Guardar en memoria para acceso rápido
            with _lock:
                ESCALATION_TRACKING[message_id] = esc_id
            log.info(f"🔁 Recuperado de DB y cacheado: {message_id} → {esc_id}")
            return esc_id
    except Exception as e:
        log.error(f"❌ Error buscando escalation en DB: {e}")

    return None


# Devuelve una copia del diccionario actual (debug o persistencia).
# Se usa en el flujo de tracking en memoria de escalaciones por mensaje para preparar datos, validaciones o decisiones previas.
# No recibe parámetros externos; trabaja con estado capturado por el cierre o atributos de instancia.
# Devuelve un `dict` con el resultado de esta operación. Sin efectos secundarios relevantes.
def get_all_trackings() -> dict:
    """Devuelve una copia del diccionario actual (debug o persistencia)."""
    with _lock:
        return dict(ESCALATION_TRACKING)


# Elimina un tracking específico o limpia todos.
# Se usa en el flujo de tracking en memoria de escalaciones por mensaje para preparar datos, validaciones o decisiones previas.
# Recibe `message_id` como entrada principal según la firma.
# No devuelve un valor de negocio; deja aplicado el cambio de estado o registro correspondiente. Sin efectos secundarios relevantes.
def clear_tracking(message_id: str | None = None):
    """Elimina un tracking específico o limpia todos."""
    with _lock:
        if message_id:
            ESCALATION_TRACKING.pop(str(message_id), None)
            log.info(f"🧹 Tracking eliminado: message_id={message_id}")
        else:
            ESCALATION_TRACKING.clear()
            log.info("🧹 Todos los trackings eliminados")
