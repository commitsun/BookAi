"""
🚀 Main Entry Point - Sistema de Agentes para Hoteles (Refactorizado y Robustecido)
===================================================================================
WhatsApp → Supervisor Input → Main Agent → Supervisor Output → WhatsApp
                     ↓                ↓
                  Interno          Interno
                     ↓                ↓
                 Telegram         Telegram
"""

import os
import json
import warnings
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

# =============================================================
# IMPORTS DEL SISTEMA
# =============================================================

from channels_wrapper.manager import ChannelManager
from core.main_agent import create_main_agent
from core.memory_manager import MemoryManager
from agents.supervisor_input_agent import SupervisorInputAgent
from agents.supervisor_output_agent import SupervisorOutputAgent
from agents.interno_agent import InternoAgent as InternoAgentV2

# =============================================================
# CONFIGURACIÓN GLOBAL
# =============================================================

warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=UserWarning)
os.environ["PYTHONWARNINGS"] = "ignore"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

log = logging.getLogger("Main")

# =============================================================
# INICIALIZACIÓN DE FASTAPI
# =============================================================

app = FastAPI(title="HotelAI - Sistema de Agentes Refactorizado")

# =============================================================
# COMPONENTES GLOBALES
# =============================================================

memory_manager = MemoryManager()
supervisor_input = SupervisorInputAgent()
supervisor_output = SupervisorOutputAgent()
interno_agent = InternoAgentV2()
channel_manager = ChannelManager()

# =============================================================
# BUFFER GLOBAL DE ESCALACIONES (Telegram ↔ WhatsApp)
# =============================================================

PENDING_ESCALATIONS = {}

log.info("✅ Sistema inicializado correctamente")

# =============================================================
# FUNCIÓN PRINCIPAL DE PROCESAMIENTO
# =============================================================

async def process_user_message(
    user_message: str,
    chat_id: str,
    hotel_name: str = "Hotel",
    channel: str = "whatsapp"
) -> str:
    """Procesa un mensaje del usuario siguiendo el flujo completo."""
    try:
        log.info(f"📨 Nuevo mensaje de {chat_id} en {channel}: {user_message[:80]}...")

        # ===== PASO 1: SUPERVISOR INPUT =====
        input_validation = await supervisor_input.validate(user_message)
        estado = input_validation.get("estado", "Aprobado") if isinstance(input_validation, dict) else "Aprobado"
        motivo = input_validation.get("motivo", "") if isinstance(input_validation, dict) else ""

        if estado != "Aprobado":
            log.warning(f"⚠️ Mensaje rechazado por Supervisor Input: {motivo}")

            escalation_msg = f"""
🚨 MENSAJE RECHAZADO POR SUPERVISOR INPUT

Chat ID: {chat_id}
Hotel: {hotel_name}

Mensaje del usuario:
{user_message}

Motivo del rechazo:
{motivo}

Por favor, revisa y responde manualmente.
"""
            await interno_agent.anotify_staff(escalation_msg, chat_id)
            return "🕓 Gracias por tu mensaje. Lo estamos revisando con nuestro equipo."

        # ===== PASO 2: MAIN AGENT =====
        try:
            history = memory_manager.get_memory(chat_id)
        except Exception as e:
            log.warning(f"⚠️ No se pudo obtener memoria: {e}")
            history = []

        async def send_inciso_callback(message: str):
            try:
                await channel_manager.send_message(chat_id, message, channel=channel)
            except Exception as e:
                log.error(f"❌ Error enviando inciso: {e}")

        main_agent = create_main_agent(
            memory_manager=memory_manager,
            send_callback=send_inciso_callback,
            model_name="gpt-4o",
            temperature=0.3,
        )

        agent_response = await main_agent.ainvoke(
            user_input=user_message,
            chat_id=chat_id,
            hotel_name=hotel_name,
            chat_history=history,
        )

        if not agent_response or not agent_response.strip():
            return "❌ Disculpa, no pude procesar tu solicitud. Intenta de nuevo."

        agent_response = str(agent_response).strip()
        log.info(f"✅ Main Agent respondió: {agent_response[:100]}...")

        # ===== PASO 3: SUPERVISOR OUTPUT =====
        output_validation = await supervisor_output.validate(
            user_input=user_message, agent_response=agent_response
        )

        estado_out = output_validation.get("estado", "Aprobado") if isinstance(output_validation, dict) else "Aprobado"
        motivo_out = output_validation.get("motivo", "") if isinstance(output_validation, dict) else ""
        sugerencia = output_validation.get("sugerencia", "") if isinstance(output_validation, dict) else ""

        if estado_out != "Aprobado":
            log.warning(f"⚠️ Respuesta rechazada por Supervisor Output: {motivo_out}")

            escalation_msg = f"""
🚨 RESPUESTA RECHAZADA POR SUPERVISOR OUTPUT

Chat ID: {chat_id}
Hotel: {hotel_name}

Mensaje del usuario:
{user_message}

Respuesta del agente (RECHAZADA):
{agent_response}

Motivo del rechazo:
{motivo_out}

Sugerencia:
{sugerencia}

Por favor, proporciona una respuesta manual adecuada.
"""
            await interno_agent.anotify_staff(escalation_msg, chat_id)
            return "🕓 Permíteme un momento para verificar esa información con nuestro equipo."

        return agent_response

    except Exception as e:
        log.error(f"❌ Error en process_user_message: {e}", exc_info=True)
        return "❌ Disculpa, ocurrió un error al procesar tu mensaje."

# =============================================================
# ENDPOINTS DE FASTAPI
# =============================================================

@app.get("/webhook")
async def verify_webhook(request: Request):
    """Verificación de Webhook para Meta (Facebook/WhatsApp)."""
    VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "midemo")

    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(params.get("hub.challenge"))
    return JSONResponse({"error": "Invalid verification token"}, status_code=403)


@app.post("/webhook")
async def webhook_receiver(request: Request):
    """Recibe mensajes desde Meta (WhatsApp) o pruebas externas (p. ej. Telegram setWebhook)."""
    try:
        data = await request.json()
        if not data:
            log.warning("⚠️ Webhook recibido vacío o sin JSON válido.")
            return JSONResponse({"status": "ignored", "reason": "empty body"})

        # --- Seguridad básica: detectar si no es un webhook de WhatsApp
        if "entry" not in data:
            log.warning(f"⚠️ Webhook recibido sin 'entry': {data}")
            return JSONResponse({"status": "ignored", "reason": "no entry"})

        entry_list = data.get("entry", [])
        if not entry_list:
            log.warning("⚠️ Webhook recibido con 'entry' vacío.")
            return JSONResponse({"status": "ignored", "reason": "empty entry"})

        entry = entry_list[0]
        changes = entry.get("changes", [])
        if not changes:
            log.warning("⚠️ Webhook recibido sin cambios.")
            return JSONResponse({"status": "ignored", "reason": "no changes"})

        value = changes[0].get("value", {})
        messages = value.get("messages", [])
        if not messages:
            log.info("ℹ️ Webhook recibido sin mensajes (posiblemente validación inicial).")
            return JSONResponse({"status": "ignored", "reason": "no messages"})

        msg = messages[0]
        sender = msg.get("from")
        text = msg.get("text", {}).get("body", "")

        if not sender or not text:
            log.warning(f"⚠️ Mensaje inválido recibido: {msg}")
            return JSONResponse({"status": "ignored", "reason": "invalid message"})

        log.info(f"📨 Mensaje recibido de {sender}: {text}")

        response = await process_user_message(
            user_message=text,
            chat_id=sender,
            channel="whatsapp"
        )

        await channel_manager.send_message(sender, response, channel="whatsapp")
        return JSONResponse({"status": "success"})

    except Exception as e:
        log.error(f"❌ Error procesando webhook POST: {e}", exc_info=True)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    """Webhook para respuestas del encargado vía Telegram (Reply → WhatsApp)."""
    try:
        data = await request.json()
        log.info(f"📞 Webhook Telegram recibido: {json.dumps(data, indent=2)}")

        message = data.get("message", {})
        text = message.get("text", "")
        reply_to = message.get("reply_to_message", {})

        if not text:
            return JSONResponse({"status": "ignored", "reason": "no text"})

        # 🧩 Detectar si el encargado respondió en "Reply"
        original_msg_id = reply_to.get("message_id")
        if not original_msg_id:
            log.warning("⚠️ Mensaje Telegram sin reply_to → ignorado.")
            return JSONResponse({"status": "ignored", "reason": "no reply reference"})

        # 🔄 Buscar chat_id original asociado en buffer global
        original_chat_id = PENDING_ESCALATIONS.get(original_msg_id)
        if not original_chat_id:
            log.warning("⚠️ No se encontró chat_id asociado al mensaje respondido.")
            return JSONResponse({"status": "ignored", "reason": "no linked chat"})

        # 📤 Enviar la respuesta del encargado al huésped (WhatsApp)
        await channel_manager.send_message(original_chat_id, text.strip(), channel="whatsapp")
        log.info(f"✅ Respuesta del encargado reenviada a huésped {original_chat_id}: {text[:80]}")

        # 🧹 Limpieza del buffer
        PENDING_ESCALATIONS.pop(original_msg_id, None)

        return JSONResponse({"status": "success"})

    except Exception as e:
        log.error(f"❌ Error procesando webhook Telegram: {e}", exc_info=True)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "version": "2.0-refactored"}


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "service": "HotelAI - Sistema de Agentes",
        "version": "2.0",
        "architecture": "n8n-style orchestration",
        "components": [
            "Supervisor Input",
            "Main Agent (Orchestrator)",
            "Supervisor Output",
            "SubAgents: dispo/precios, info, interno_v2",
        ],
    }


# =============================================================
# EJECUCIÓN LOCAL
# =============================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
