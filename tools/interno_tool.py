"""
🔧 Interno Tool - Escalación y Gestión de Respuestas al Encargado
=================================================================
Define las herramientas LangChain usadas por el agente interno del hotel.
Se encarga de:
- Notificar al encargado por Telegram cuando hay una escalación.
- Generar borradores de respuesta profesionales y empáticos.
- Confirmar y enviar respuestas finales al huésped por WhatsApp.
"""

import logging
import re
import importlib
import requests
from datetime import datetime
from typing import Dict, Optional
from dataclasses import dataclass
from pydantic import BaseModel
from langchain_core.tools import tool

# 🧩 Core imports
from core.language_manager import language_manager
from core.escalation_db import save_escalation, update_escalation
from core.config import Settings as C, ModelConfig, ModelTier  # ✅ Config centralizada
from core.escalation_manager import get_escalation

log = logging.getLogger("InternoTool")

# =============================================================
# 🧠 ESTRUCTURAS DE DATOS GLOBALES
# =============================================================

@dataclass
class Escalation:
    escalation_id: str
    guest_chat_id: str
    guest_message: str
    escalation_type: str
    escalation_reason: str
    context: str
    timestamp: str
    draft_response: Optional[str] = None
    manager_confirmed: bool = False
    final_response: Optional[str] = None
    sent_to_guest: bool = False


ESCALATIONS_STORE: Dict[str, Escalation] = {}

# =============================================================
# 📥 INPUT SCHEMAS
# =============================================================

class SendToEncargadoInput(BaseModel):
    escalation_id: str
    guest_chat_id: str
    guest_message: str
    escalation_type: str
    reason: str
    context: str


class GenerarBorradorInput(BaseModel):
    escalation_id: str
    manager_response: str


class ConfirmarYEnviarInput(BaseModel):
    escalation_id: str
    confirmed: bool
    adjustments: str = ""


# =============================================================
# 📨 TOOL 1: NOTIFICAR ENCARGADO (Telegram)
# =============================================================

def send_to_encargado(escalation_id, guest_chat_id, guest_message, escalation_type, reason, context) -> str:
    """Envía una notificación al encargado del hotel por Telegram."""
    try:
        esc = Escalation(
            escalation_id=escalation_id,
            guest_chat_id=guest_chat_id,
            guest_message=guest_message,
            escalation_type=escalation_type,
            escalation_reason=reason,
            context=context,
            timestamp=datetime.utcnow().isoformat(),
        )
        ESCALATIONS_STORE[escalation_id] = esc
        save_escalation(vars(esc))

        tipo_map = {
            "info_not_found": "ℹ️ Información No Disponible",
            "inappropriate": "🚨 Contenido Inapropiado",
            "bad_response": "⚠️ Respuesta Incorrecta",
            "manual": "📎 Escalación Manual",
        }

        msg = f"""
🔔 *NUEVA CONSULTA ESCALADA*
🆔 *ID:* `{escalation_id}`
📱 *Chat ID:* `{guest_chat_id}`
🏷️ *Tipo:* {tipo_map.get(escalation_type, escalation_type)}

❓ *Mensaje del huésped:*
{guest_message}

📝 *Razón:*
{reason}

💭 *Contexto:*
{context}

⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC

➡️ Responde a este mensaje (Reply). El sistema generará un borrador automáticamente.
"""

        if not C.TELEGRAM_CHAT_ID or not C.TELEGRAM_BOT_TOKEN:
            return "⚠️ No se pudo enviar la notificación: faltan credenciales de Telegram."

        r = requests.post(
            f"https://api.telegram.org/bot{C.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": str(C.TELEGRAM_CHAT_ID), "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )

        if r.status_code == 200:
            data = r.json()
            sent_message_id = str(data.get("result", {}).get("message_id", ""))

            if sent_message_id:
                try:
                    from core.escalation_manager import register_escalation
                    register_escalation(sent_message_id, escalation_id)
                    log.info(f"📎 Registrado message_id={sent_message_id} → escalación={escalation_id}")
                except Exception as e:
                    log.warning(f"⚠️ No se pudo registrar message_id → {e}")

            log.info(f"✅ Escalación {escalation_id} enviada correctamente al encargado.")
            return f"Escalación {escalation_id} notificada al encargado con éxito."

        return f"❌ Error al notificar al encargado: {r.text}"

    except Exception as e:
        log.exception("Error notificando al encargado")
        return f"Error notificando al encargado: {e}"


# =============================================================
# 🧠 TOOL 2: GENERAR BORRADOR DE RESPUESTA
# =============================================================

def generar_borrador(escalation_id: str, manager_response: str, adjustment: Optional[str] = None) -> str:
    """Genera o reformula un borrador empático y profesional para el huésped."""
    if escalation_id not in ESCALATIONS_STORE:
        return f"Error: Escalación {escalation_id} no encontrada."

    esc = ESCALATIONS_STORE[escalation_id]

    # ✅ Usa configuración centralizada para el modelo del agente interno
    llm = ModelConfig.get_llm(ModelTier.INTERNAL)

    try:
        target_lang = language_manager.detect_language(esc.guest_message)
    except Exception:
        target_lang = "es"

    system_prompt = (
        "Eres un asistente especializado en atención hotelera.\n"
        "Tu tarea es reformular el mensaje del encargado para el huésped con un tono cálido, empático y profesional.\n"
        "Usa el mismo idioma del huésped.\n"
        "No incluyas encabezados, comillas ni explicaciones, solo el texto final que se enviará al cliente.\n"
        "Si se proporcionan 'ajustes', incorpóralos en el tono o contenido."
    )

    user_prompt = (
        f"Mensaje original del huésped:\n{esc.guest_message}\n\n"
        f"Respuesta del encargado:\n{manager_response}\n"
    )

    if adjustment:
        user_prompt += f"\nInstrucciones de ajuste del encargado:\n{adjustment}\n"

    user_prompt += "\nReformula la respuesta final para el huésped siguiendo esas pautas."

    try:
        response = llm.invoke([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])
        draft = (response.content or "").strip()
        draft = re.sub(r'^[\"\'“”]+|[\"\'“”]+$', '', draft).strip()

        esc.draft_response = draft
        update_escalation(escalation_id, {"draft_response": draft})

        formatted = (
            f"📝 *BORRADOR DE RESPUESTA PROPUESTO:*\n\n"
            f"{draft}\n\n"
            "✏️ Si deseas modificar el texto, escribe tus ajustes directamente.\n"
            "✅ Si estás conforme, responde con 'OK' para enviarlo al huésped."
        )
        return formatted

    except Exception as e:
        log.exception("Error generando borrador")
        return f"Error generando borrador: {e}"


# =============================================================
# 📤 TOOL 3: CONFIRMAR Y ENVIAR RESPUESTA FINAL
# =============================================================

async def confirmar_y_enviar(escalation_id: str, confirmed: bool, adjustments: str = "") -> str:
    """Confirma o reformula según el input del encargado y envía si corresponde."""
    if escalation_id not in ESCALATIONS_STORE:
        return f"Error: Escalación {escalation_id} no encontrada."

    esc = ESCALATIONS_STORE[escalation_id]

    # 🔁 Caso 1: ajustes → reformular nuevo borrador
    if not confirmed and adjustments:
        new_draft = generar_borrador(escalation_id, esc.draft_response or "", adjustment=adjustments)

        clean_draft = new_draft
        for marker in [
            "📝 *BORRADOR DE RESPUESTA PROPUESTO:*",
            "✏️ Si deseas modificar",
            "✅ Si estás conforme",
            "📝 *Nuevo borrador generado",
        ]:
            clean_draft = clean_draft.replace(marker, "").strip()

        formatted = (
            "📝 *Nuevo borrador generado según tus ajustes:*\n\n"
            f"{clean_draft.strip()}\n\n"
            "✏️ Si deseas más cambios, vuelve a escribirlos.\n"
            "✅ Si estás conforme, responde con 'OK' para enviarlo al huésped."
        )
        return formatted

    # ✅ Caso 2: confirmado → envío final
    if confirmed:
        final_text = (esc.draft_response or "").strip()
        if not final_text:
            return "⚠️ No hay texto final disponible para enviar."

        try:
            lang = language_manager.detect_language(final_text)
            final_text = language_manager.ensure_language(final_text, lang)
        except Exception:
            pass

        try:
            ChannelManager = importlib.import_module("channels_wrapper.manager").ChannelManager
            cm = ChannelManager()
            await cm.send_message(esc.guest_chat_id, final_text, channel="whatsapp")

            esc.final_response = final_text
            esc.manager_confirmed = True
            esc.sent_to_guest = True
            update_escalation(escalation_id, {
                "final_response": final_text,
                "manager_confirmed": True,
                "sent_to_guest": True,
            })
            return f"✅ *Respuesta enviada al huésped:*\n\n{final_text}"

        except Exception as e:
            log.exception("Error enviando respuesta final")
            return f"Error enviando respuesta: {e}"

    return "❌ Borrador rechazado. Esperando nueva versión."


# =============================================================
# 🧩 REGISTRO DE TOOLS
# =============================================================

@tool("notificar_encargado", args_schema=SendToEncargadoInput, return_direct=False)
def notificar_encargado_tool(**kwargs) -> str:
    """Tool que notifica al encargado del hotel sobre una nueva escalación por Telegram."""
    return send_to_encargado(**kwargs)


@tool("generar_borrador_respuesta", args_schema=GenerarBorradorInput, return_direct=True)
def generar_borrador_tool(**kwargs) -> str:
    """Tool que genera un borrador empático y profesional para el huésped a partir de la respuesta del encargado."""
    return generar_borrador(**kwargs)


@tool("confirmar_y_enviar_respuesta", args_schema=ConfirmarYEnviarInput, return_direct=True)
async def confirmar_y_enviar_tool(**kwargs) -> str:
    """Tool que confirma o ajusta la respuesta y la envía al huésped por WhatsApp."""
    return await confirmar_y_enviar(**kwargs)


def create_interno_tools():
    """Devuelve la lista de herramientas disponibles para el agente interno."""
    return [
        notificar_encargado_tool,
        generar_borrador_tool,
        confirmar_y_enviar_tool,
    ]
