"""
📞 Interno Agent v2 - Agente de Escalación (Refactorizado + Enlace Telegram↔WhatsApp)
====================================================================================
Agente especializado en escalar consultas al encargado del hotel vía Telegram.

CARACTERÍSTICAS:
----------------
- Envía notificaciones formateadas al encargado (Telegram)
- Registra relación entre mensaje Telegram ↔ chat huésped
- Permite que la respuesta del encargado (Reply) se reenvíe automáticamente al huésped (WhatsApp)
- Guarda incidencias en Supabase (opcional)
- Compatible con arquitectura n8n / main orchestrator
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
# CONFIGURACIÓN GLOBAL
# =============================================================

try:
    from core.config import Settings as C
    TELEGRAM_BOT_TOKEN = C.TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID = C.TELEGRAM_CHAT_ID
    SUPABASE_URL = C.SUPABASE_URL
    SUPABASE_KEY = C.SUPABASE_KEY
except Exception:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# =============================================================
# CLASE PRINCIPAL
# =============================================================

class InternoAgent:
    """
    Agente de escalación que maneja la comunicación con el encargado del hotel vía Telegram.
    """

    def __init__(self):
        """Inicializa el agente interno con configuración de Telegram y Supabase."""
        self.telegram_token = TELEGRAM_BOT_TOKEN
        self.telegram_chat_id = TELEGRAM_CHAT_ID

        # Inicializar Supabase si está disponible
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

    # =========================================================
    def notify_staff(self, message: str, chat_id: str = "", context: dict = None) -> str:
        """
        Envía una notificación al encargado del hotel vía Telegram y registra el vínculo.
        """
        try:
            if not self.telegram_token or not self.telegram_chat_id:
                log.error("❌ Configuración de Telegram incompleta")
                return "❌ Error: Falta configuración de Telegram"

            # Formatear mensaje con contexto
            formatted_message = self._format_telegram_message(message, chat_id, context)

            # Enviar mensaje y registrar relación
            message_id = self._send_telegram_message(formatted_message, chat_id)

            if message_id:
                self._register_escalation(message_id, chat_id)

                # Guardar incidente si aplica
                self._save_incident({
                    "message": message,
                    "chat_id": chat_id,
                    "context": context,
                    "telegram_message_id": message_id,
                    "timestamp": self._get_timestamp()
                })

                log.info(f"✅ Escalación registrada correctamente (chat_id={chat_id}, msg_id={message_id})")
                return "🕓 Enseguida te confirmo la información, un momento por favor 😊"
            else:
                log.error("❌ Error: no se pudo enviar mensaje al encargado.")
                return "❌ Error al enviar notificación."

        except Exception as e:
            log.error(f"❌ Error en notify_staff: {e}", exc_info=True)
            return f"❌ Error: {str(e)}"

    # =========================================================
    async def anotify_staff(self, message: str, chat_id: str = "", context: dict = None) -> str:
        """Versión asíncrona de notify_staff."""
        return self.notify_staff(message, chat_id, context)

    # =========================================================
    def _format_telegram_message(self, message: str, chat_id: str = "", context: dict = None) -> str:
        """
        Formatea el mensaje para Telegram con Markdown.
        """
        try:
            base = "🔔 *NUEVA CONSULTA ESCALADA*\n\n"
            base += f"📱 *Chat ID:* `{chat_id}`\n\n"

            # Estructura del mensaje
            if message.strip().startswith("{"):
                base += f"```json\n{message.strip()}\n```"
            elif re.search(r"(?i)^estado\s*:", message, re.MULTILINE):
                base += f"```text\n{message.strip()}\n```"
            else:
                base += f"{message.strip()}"

            if context:
                base += f"\n\n📝 *Contexto adicional:*\n```json\n{json.dumps(context, indent=2, ensure_ascii=False)}\n```"

            base += f"\n\n⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"
            base += "\n\n➡️ *Por favor, responde a este mensaje usando 'Responder' (Reply) para que la respuesta llegue al huésped automáticamente.*"
            return base

        except Exception as e:
            log.error(f"⚠️ Error formateando mensaje: {e}")
            return f"🚨 *Error de formato*\n\nChat ID: {chat_id}\n\n{message}"

    # =========================================================
    def _send_telegram_message(self, formatted_message: str, chat_id: str = "") -> Optional[int]:
        """
        Envía el mensaje formateado al encargado por Telegram y devuelve su message_id.
        """
        try:
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            payload = {
                "chat_id": self.telegram_chat_id,
                "text": formatted_message,
                "parse_mode": "Markdown",
            }

            response = requests.post(url, json=payload, timeout=10)

            if response.status_code == 200:
                data = response.json()
                message_id = data.get("result", {}).get("message_id")

                # Vincular con chat del huésped
                self._register_escalation(message_id, chat_id)

                log.info(f"📨 Mensaje enviado a Telegram exitosamente (message_id={message_id})")
                return message_id
            else:
                log.error(f"⚠️ Error en Telegram API: {response.text}")
                return None

        except Exception as e:
            log.error(f"❌ Error enviando a Telegram: {e}", exc_info=True)
            return None

    # =========================================================
    def _register_escalation(self, message_id: int, chat_id: str):
        """
        Registra la relación entre el mensaje de Telegram y el chat del huésped.
        """
        try:
            # Import dinámico para evitar ciclo de importación
            from main import PENDING_ESCALATIONS
            if isinstance(PENDING_ESCALATIONS, dict):
                PENDING_ESCALATIONS[message_id] = chat_id
                log.info(f"🧩 Escalación registrada en buffer: Telegram({message_id}) → WhatsApp({chat_id})")
        except Exception as e:
            log.warning(f"⚠️ No se pudo registrar escalación global: {e}")

    # =========================================================
    def _save_incident(self, incident_data: dict) -> None:
        """
        Guarda el incidente en Supabase si está disponible.
        """
        try:
            if not self.supabase:
                log.debug("📋 Supabase no disponible, incidente solo en logs")
                return

            result = self.supabase.table("incidents").insert({
                "origin": "InternoAgent",
                "payload": json.dumps(incident_data, ensure_ascii=False),
                "created_at": self._get_timestamp()
            }).execute()

            log.info(f"💾 Incidente guardado en Supabase: {result.data}")
        except Exception as e:
            log.warning(f"⚠️ No se pudo guardar en Supabase: {e}")

    # =========================================================
    def _get_timestamp(self) -> str:
        """Retorna timestamp ISO actual."""
        return datetime.utcnow().isoformat()

# =============================================================
# FACTORY Y COMPATIBILIDAD
# =============================================================

def create_interno_agent() -> InternoAgent:
    """Crea una instancia lista del agente interno."""
    return InternoAgent()

async def process_tool_call(payload: str) -> str:
    """Compatibilidad con versión MCP."""
    agent = InternoAgent()
    cleaned = payload
    if isinstance(payload, str) and payload.strip().startswith("Interno("):
        cleaned = payload.strip()[8:-1].strip("`\n ")
    return agent.notify_staff(cleaned)
