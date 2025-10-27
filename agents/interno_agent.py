import logging
import os
import json
import re
import requests
from fastmcp import FastMCP
from supabase import create_client
from core.config import Settings as C
from core.observability import ls_context  # 🟢 NUEVO

# =====================================================
# CONFIGURACIÓN BÁSICA
# =====================================================
log = logging.getLogger("InternoAgent")
mcp = FastMCP("InternoAgent")

supabase = None
try:
    if C.SUPABASE_URL and C.SUPABASE_KEY:
        supabase = create_client(C.SUPABASE_URL, C.SUPABASE_KEY)
        log.info("✅ Supabase inicializado correctamente.")
    else:
        log.warning("⚠️ Supabase deshabilitado (faltan credenciales).")
except Exception as e:
    log.error(f"❌ Error inicializando Supabase: {e}")

TELEGRAM_BOT_TOKEN = C.TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID = C.TELEGRAM_CHAT_ID

# =====================================================
# 📄 Expresión para detectar bloques estructurados
# =====================================================
SUPERVISOR_BLOCK_RE = re.compile(r"(?i)^estado\s*:", re.MULTILINE)


# =====================================================
# 📩 Función principal: enviar mensaje a Telegram
# =====================================================
def notify_encargado(text: str):
    """Envía un mensaje al encargado del hotel por Telegram con formato enriquecido."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("❌ Falta configuración de Telegram (TOKEN o CHAT_ID).")
        return

    formatted = None
    try:
        if SUPERVISOR_BLOCK_RE.search(text):
            formatted = (
                "🚨 *Alerta del sistema HotelAI*\n"
                "```text\n" + text.strip() + "\n```"
            )
        elif text.strip().startswith("Interno("):
            inner = text.strip()[8:-1]
            formatted = (
                "🚨 *Alerta del sistema HotelAI*\n"
                "```json\n" + inner.strip() + "\n```"
            )
        elif text.strip().startswith("{"):
            formatted = (
                "🚨 *Alerta del sistema HotelAI*\n"
                "```json\n" + text.strip() + "\n```"
            )
        else:
            formatted = (
                "🚨 *Notificación interna HotelAI*\n"
                "```\n" + text.strip() + "\n```"
            )
    except Exception:
        formatted = f"🚨 *Alerta del sistema HotelAI*\n\n{text}"

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": formatted,
        "parse_mode": "Markdown",
    }

    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            log.info("📨 Notificación enviada al encargado vía Telegram.")
        else:
            log.error(f"⚠️ Error al enviar notificación Telegram: {r.text}")
    except Exception as e:
        log.error(f"❌ Error enviando notificación a Telegram: {e}", exc_info=True)


# =====================================================
# 💾 Guardar incidencia en Supabase
# =====================================================
def save_incident(payload: str, origin: str = "Sistema"):
    """Guarda el incidente si Supabase está disponible."""
    try:
        if not supabase:
            log.warning("⚠️ [InternoAgent] Supabase no disponible, guardado omitido.")
            log.warning(f"📋 Incidente (solo log): {payload}")
            return

        if isinstance(payload, str):
            try:
                data = json.loads(payload)
            except Exception:
                data = {"raw": payload}
        else:
            data = payload

        res = (
            supabase.table("incidents")
            .insert({"origin": origin, "payload": json.dumps(data, ensure_ascii=False)})
            .execute()
        )
        log.info(f"💾 Incidente registrado en Supabase: {res.data}")
    except Exception as e:
        log.error(f"⚠️ [InternoAgent] No se pudo guardar en Supabase: {e}")
        log.warning(f"📋 Incidente logueado:\n{payload}")


# =====================================================
# 🧠 MCP Tool — Llamable desde otros agentes MCP
# =====================================================
@mcp.tool()
async def notificar_interno(payload: str):
    """Herramienta MCP oficial: recibe alertas desde otros agentes."""
    with ls_context(
        name="InternoAgent.notificar_interno",
        metadata={"payload": payload},
        tags=["interno", "alerta"],
    ):
        log.info(f"📥 InternoAgent MCP recibió alerta: {payload}")
        save_incident(payload, origin="Supervisor/MCP")
        notify_encargado(payload)
        return "✅ Alerta transmitida al encargado."


# =====================================================
# 🔗 Wrapper compatible con HotelAIHybrid
# =====================================================
async def process_tool_call(payload: str):
    """Wrapper para llamadas directas desde el HotelAIHybrid."""
    with ls_context(
        name="InternoAgent.process_tool_call",
        metadata={"payload": payload},
        tags=["interno", "wrapper"],
    ):
        try:
            log.info(f"📨 InternoAgent (wrapper) recibió: {payload}")

            cleaned = payload
            if isinstance(payload, str) and payload.strip().startswith("Interno("):
                cleaned = payload.strip()[8:-1]
                cleaned = cleaned.strip("` \n")

            save_incident(cleaned, origin="HotelAIHybrid")
            notify_encargado(cleaned)
        except Exception as e:
            log.error(f"❌ Error en process_tool_call: {e}", exc_info=True)


if __name__ == "__main__":
    print("✅ InternoAgent operativo (modo temporal sin tabla Supabase)")
    mcp.run(transport="stdio", show_banner=False)
