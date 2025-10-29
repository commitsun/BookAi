#interno-agent-v2.py
"""
📞 Interno Agent v2 - Agente de Escalación (Refactorizado)
===========================================================
Agente especializado en escalar consultas al encargado del hotel vía Telegram.
Se invoca cuando los otros agentes no pueden resolver la consulta del usuario.

CARACTERÍSTICAS:
----------------
- Envía notificaciones formateadas por Telegram
- Guarda incidencias en Supabase (opcional)
- Puede ser usado como tool o invocado directamente
- Compatible con arquitectura n8n
"""

import logging
import os
import json
import re
import requests
from typing import Optional
from supabase import create_client

# Configuración (ajustar según tu setup)
try:
    from core.config import Settings as C
    TELEGRAM_BOT_TOKEN = C.TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID = C.TELEGRAM_CHAT_ID
    SUPABASE_URL = C.SUPABASE_URL
    SUPABASE_KEY = C.SUPABASE_KEY
except:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY")

log = logging.getLogger("InternoAgent")


class InternoAgent:
    """
    Agente de escalación que maneja comunicación con el encargado del hotel.
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
        
        log.info("✅ InternoAgent inicializado")
    
    def notify_staff(self, message: str, chat_id: str = "", context: dict = None) -> str:
        """
        Envía una notificación al encargado del hotel vía Telegram.
        
        Args:
            message: Mensaje a enviar al encargado
            chat_id: ID del chat del usuario (para contexto)
            context: Información adicional de contexto
            
        Returns:
            Confirmación de envío
        """
        try:
            if not self.telegram_token or not self.telegram_chat_id:
                log.error("❌ Configuración de Telegram incompleta")
                return "❌ Error: Falta configuración de Telegram"
            
            # Formatear mensaje con contexto
            formatted_message = self._format_telegram_message(message, chat_id, context)
            
            # Enviar por Telegram
            success = self._send_telegram_message(formatted_message)
            
            if success:
                # Guardar incidente en Supabase si está disponible
                self._save_incident({
                    "message": message,
                    "chat_id": chat_id,
                    "context": context,
                    "timestamp": self._get_timestamp()
                })
                
                log.info(f"✅ Notificación enviada al encargado (chat: {chat_id})")
                return "✅ Notificación enviada al encargado correctamente"
            else:
                log.error("❌ No se pudo enviar notificación por Telegram")
                return "❌ Error al enviar notificación"
                
        except Exception as e:
            log.error(f"❌ Error en notify_staff: {e}", exc_info=True)
            return f"❌ Error: {str(e)}"
    
    def _format_telegram_message(
        self, 
        message: str, 
        chat_id: str = "", 
        context: dict = None
    ) -> str:
        """
        Formatea el mensaje para Telegram con Markdown.
        
        Args:
            message: Mensaje base
            chat_id: ID del chat
            context: Contexto adicional
            
        Returns:
            Mensaje formateado
        """
        try:
            # Detectar si es un JSON estructurado
            if message.strip().startswith("{"):
                formatted = (
                    "🚨 *ALERTA - Sistema HotelAI*\n\n"
                    f"📱 Chat ID: `{chat_id}`\n\n"
                    "```json\n" + message.strip() + "\n```"
                )
            # Detectar bloque de supervisor
            elif re.search(r"(?i)^estado\s*:", message, re.MULTILINE):
                formatted = (
                    "🚨 *ALERTA - Supervisor*\n\n"
                    f"📱 Chat ID: `{chat_id}`\n\n"
                    "```text\n" + message.strip() + "\n```"
                )
            else:
                # Mensaje normal
                formatted = (
                    "🔔 *Notificación - HotelAI*\n\n"
                    f"📱 Chat: `{chat_id}`\n\n"
                    f"{message.strip()}"
                )
            
            # Agregar contexto si existe
            if context:
                formatted += f"\n\n📝 *Contexto adicional:*\n```json\n{json.dumps(context, indent=2, ensure_ascii=False)}\n```"
            
            return formatted
            
        except Exception as e:
            log.error(f"⚠️ Error formateando mensaje: {e}")
            return f"🚨 *Alerta HotelAI*\n\nChat: {chat_id}\n\n{message}"
    
    def _send_telegram_message(self, formatted_message: str) -> bool:
        """
        Envía el mensaje formateado a Telegram.
        
        Args:
            formatted_message: Mensaje ya formateado
            
        Returns:
            True si se envió correctamente
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
                log.info("📨 Mensaje enviado a Telegram exitosamente")
                return True
            else:
                log.error(f"⚠️ Error en Telegram API: {response.text}")
                return False
                
        except Exception as e:
            log.error(f"❌ Error enviando a Telegram: {e}", exc_info=True)
            return False
    
    def _save_incident(self, incident_data: dict) -> None:
        """
        Guarda el incidente en Supabase si está disponible.
        
        Args:
            incident_data: Datos del incidente a guardar
        """
        try:
            if not self.supabase:
                log.debug("📋 Supabase no disponible, incidente solo en logs")
                return
            
            # Insertar en tabla de incidentes
            result = self.supabase.table("incidents").insert({
                "origin": "InternoAgent",
                "payload": json.dumps(incident_data, ensure_ascii=False),
                "created_at": self._get_timestamp()
            }).execute()
            
            log.info(f"💾 Incidente guardado en Supabase: {result.data}")
            
        except Exception as e:
            log.warning(f"⚠️ No se pudo guardar en Supabase: {e}")
    
    def _get_timestamp(self) -> str:
        """Retorna timestamp ISO actual."""
        from datetime import datetime
        return datetime.utcnow().isoformat()
    
    async def anotify_staff(
        self, 
        message: str, 
        chat_id: str = "", 
        context: dict = None
    ) -> str:
        """
        Versión asíncrona de notify_staff.
        
        Args:
            message: Mensaje a enviar
            chat_id: ID del chat
            context: Contexto adicional
            
        Returns:
            Confirmación de envío
        """
        # Wrapper asíncrono (la implementación actual es síncrona)
        return self.notify_staff(message, chat_id, context)


# =============================================================
# Factory y funciones de compatibilidad
# =============================================================

def create_interno_agent() -> InternoAgent:
    """
    Factory function para crear instancia del agente Interno.
    
    Returns:
        InternoAgent configurado
    """
    return InternoAgent()


# Compatibilidad con versión anterior (MCP style)
async def process_tool_call(payload: str) -> str:
    """
    Wrapper de compatibilidad con la versión anterior.
    
    Args:
        payload: Mensaje o JSON a escalar
        
    Returns:
        Confirmación
    """
    agent = InternoAgent()
    
    # Limpiar payload si viene con formato "Interno(...)"
    cleaned = payload
    if isinstance(payload, str) and payload.strip().startswith("Interno("):
        cleaned = payload.strip()[8:-1].strip("`\n ")
    
    return agent.notify_staff(cleaned)