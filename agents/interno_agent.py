"""
📞 Interno Agent v3 - Agente de Escalación (Telegram ↔ WhatsApp)
=================================================================
Agente especializado en escalar consultas al encargado del hotel vía Telegram.

CARACTERÍSTICAS:
----------------
✅ Envía notificaciones formateadas al encargado (Telegram)
✅ Registra vínculo entre mensaje Telegram ↔ chat huésped
✅ Permite que la respuesta del encargado (Reply) se reenvíe al huésped (WhatsApp)
✅ Guarda incidencias en Supabase (opcional)
✅ Totalmente compatible con el orquestador principal (FastAPI + LangGraph)
"""

import logging
import os
import json
import re
import requests
from typing import Optional
from datetime import datetime
from supabase import create_client

log = logging.getLogger("InternoAgent")

# =============================================================
# ⚙️ CONFIGURACIÓN GLOBAL
# =============================================================
try:
    from core.config import Settings as C
    TELEGRAM_BOT_TOKEN = C.TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID = C.TELEGRAM_CHAT_ID
    SUPABASE_URL = C.SUPABASE_URL
    SUPABASE_KEY = C.SUPABASE_KEY
except Exception:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_BOT_TOKEN")
    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY")


# =============================================================
# 🧠 CLASE PRINCIPAL
# =============================================================
class InternoAgent:
    """
    Maneja escalaciones al encargado humano por Telegram y opcionalmente guarda registros en Supabase.
    """

    def __init__(self):
        self.telegram_token = TELEGRAM_BOT_TOKEN
        self.telegram_chat_id = TELEGRAM_CHAT_ID
        self.supabase = None

        try:
            if SUPABASE_URL and SUPABASE_KEY:
                self.supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
                log.info("✅ Supabase inicializado en InternoAgent")
            else:
                log.warning("⚠️ Supabase no configurado (credenciales faltantes)")
        except Exception as e:
            log.warning(f"⚠️ No se pudo inicializar Supabase: {e}")

        log.info("✅ InternoAgent inicializado correctamente")

    # ---------------------------------------------------------
    def _get_timestamp(self) -> str:
        """Devuelve timestamp ISO actual (UTC)."""
        return datetime.utcnow().isoformat()

    # ---------------------------------------------------------
    def _register_escalation(self, message_id: int, chat_id: str):
        """
        Vincula el message_id del mensaje de Telegram con el chat_id del huésped.
        Esto permite reenviar las respuestas del encargado al huésped correcto.
        """
        try:
            from main import PENDING_ESCALATIONS  # import dinámico para evitar ciclos
            if isinstance(PENDING_ESCALATIONS, dict):
                PENDING_ESCALATIONS[message_id] = chat_id
                log.info(f"🧩 Escalación registrada: Telegram({message_id}) ↔ WhatsApp({chat_id})")
        except Exception as e:
            log.warning(f"⚠️ No se pudo registrar escalación en buffer: {e}")

    # ---------------------------------------------------------
    def _format_telegram_message(self, message: str, chat_id: str = "", context: dict = None) -> str:
        """
        Da formato al mensaje que se enviará al encargado.
        """
        try:
            text = "🔔 *NUEVA CONSULTA ESCALADA*\n\n"
            text += f"📱 *Chat ID:* `{chat_id}`\n\n"

            if message.strip().startswith("{"):
                text += f"```json\n{message.strip()}\n```"
            elif re.search(r"(?i)^estado\s*:", message):
                text += f"```text\n{message.strip()}\n```"
            else:
                text += message.strip()

            if context:
                text += f"\n\n📝 *Contexto adicional:*\n```json\n{json.dumps(context, indent=2, ensure_ascii=False)}\n```"

            text += f"\n\n⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"
            text += "\n\n➡️ *Responde con 'Reply' para que el huésped reciba tu mensaje automáticamente.*"

            return text
        except Exception as e:
            log.error(f"⚠️ Error formateando mensaje: {e}", exc_info=True)
            return f"🚨 *Error formateando mensaje*\n\n{message}"

    # ---------------------------------------------------------
    def _send_telegram_message(self, formatted_message: str, chat_id: str = "") -> Optional[int]:
        """
        Envía el mensaje al encargado del hotel por Telegram y devuelve su message_id.
        """
        try:
            if not self.telegram_token or not self.telegram_chat_id:
                log.error("❌ Configuración de Telegram incompleta.")
                return None

            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            payload = {"chat_id": self.telegram_chat_id, "text": formatted_message, "parse_mode": "Markdown"}
            response = requests.post(url, json=payload, timeout=10)

            if response.status_code == 200:
                data = response.json()
                message_id = data.get("result", {}).get("message_id")
                if message_id:
                    self._register_escalation(message_id, chat_id)
                log.info(f"📨 Mensaje enviado al encargado (Telegram msg_id={message_id})")
                return message_id

            log.error(f"❌ Telegram API error: {response.text}")
            return None

        except Exception as e:
            log.error(f"❌ Error enviando mensaje a Telegram: {e}", exc_info=True)
            return None

    # ---------------------------------------------------------
    def _save_incident(self, incident_data: dict):
        """
        Guarda el incidente en Supabase si está configurado.
        """
        try:
            if not self.supabase:
                log.debug("📋 Supabase no disponible, se omite registro remoto.")
                return
            result = self.supabase.table("incidents").insert({
                "origin": "InternoAgent",
                "payload": json.dumps(incident_data, ensure_ascii=False),
                "created_at": self._get_timestamp()
            }).execute()
            log.info(f"💾 Incidente guardado en Supabase: {result.data}")
        except Exception as e:
            log.warning(f"⚠️ No se pudo guardar incidente en Supabase: {e}")

    # ---------------------------------------------------------
    def notify_staff(self, message: str, chat_id: str = "", context: dict = None) -> str:
        """
        Envía una notificación de escalación al encargado vía Telegram.
        """
        try:
            formatted = self._format_telegram_message(message, chat_id, context)
            message_id = self._send_telegram_message(formatted, chat_id)

            if message_id:
                incident = {
                    "message": message,
                    "chat_id": chat_id,
                    "context": context,
                    "telegram_message_id": message_id,
                    "timestamp": self._get_timestamp(),
                }
                self._save_incident(incident)
                return "🕓 Enseguida te confirmo la información, un momento por favor 😊"

            return "❌ Error: no se pudo enviar la notificación al encargado."

        except Exception as e:
            log.error(f"❌ Error en notify_staff: {e}", exc_info=True)
            return "❌ Ocurrió un error al escalar la consulta."

    # ---------------------------------------------------------
    async def anotify_staff(self, message: str, chat_id: str = "", context: dict = None) -> str:
        """Versión asíncrona (usada por MainAgent)."""
        return self.notify_staff(message, chat_id, context)


# =============================================================
# 🧩 FACTORY + COMPATIBILIDAD MCP
# =============================================================
def create_interno_agent() -> InternoAgent:
    """Crea una instancia lista del agente interno."""
    return InternoAgent()


async def process_tool_call(payload: str) -> str:
    """
    Permite que otros módulos (LangGraph o MCP) invoquen al agente Interno directamente.
    """
    agent = InternoAgent()
    cleaned = payload
    if isinstance(payload, str) and payload.strip().startswith("Interno("):
        cleaned = payload.strip()[8:-1].strip("`\n ")
    return agent.notify_staff(cleaned)
