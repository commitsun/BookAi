"""
🚀 Main Entry Point - Sistema de Agentes para Hoteles (Refactorizado)
======================================================================
WhatsApp → Supervisor Input → Main Agent → Supervisor Output → WhatsApp
                     ↓                ↓
                  Interno          Interno
                     ↓                ↓
                 Telegram         Telegram
"""

import os
import warnings
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

# Imports del sistema
from channels_wrapper.manager import ChannelManager
from core.main_agent import create_main_agent
from core.memory_manager import MemoryManager
from agents.supervisor_input_agent import SupervisorInputAgent
from agents.supervisor_output_agent import SupervisorOutputAgent
from agents.interno_agent import InternoAgent

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
# INICIALIZACIÓN DE COMPONENTES GLOBALES
# =============================================================

memory_manager = MemoryManager()
supervisor_input = SupervisorInputAgent()
supervisor_output = SupervisorOutputAgent()
interno_agent = InternoAgent()
channel_manager = ChannelManager()

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
        log.info("🔍 PASO 1: Supervisor Input validando mensaje...")
        input_validation = await supervisor_input.validate(user_message)

        if isinstance(input_validation, dict):
            estado = input_validation.get("estado", "Aprobado")
            motivo = input_validation.get("motivo", "")
        else:
            estado = "Rechazado" if "rechazado" in input_validation.lower() else "Aprobado"
            motivo = input_validation if estado != "Aprobado" else ""

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

        log.info("✅ Mensaje aprobado por Supervisor Input")

        # ===== PASO 2: MAIN AGENT =====
        log.info("🤖 PASO 2: Main Agent procesando...")

        # Recuperar historial conversacional
        try:
            history = memory_manager.get_memory(chat_id)
        except Exception as e:
            log.warning(f"⚠️ No se pudo obtener memoria: {e}")
            history = []

        # 🔧 Callback corregido con await
        async def send_inciso_callback(message: str):
            try:
                await channel_manager.send_message(chat_id, message, channel=channel)
                log.info(f"📤 Inciso enviado: {message[:80]}...")
            except Exception as e:
                log.error(f"❌ Error enviando inciso: {e}")

        # Crear agente principal
        main_agent = create_main_agent(
            memory_manager=memory_manager,
            send_callback=send_inciso_callback,
            model_name="gpt-4o",
            temperature=0.3,
        )

        # Ejecutar agente principal
        agent_response = await main_agent.ainvoke(
            user_input=user_message,
            chat_id=chat_id,
            hotel_name=hotel_name,
            chat_history=history,
        )

        if not agent_response or not agent_response.strip():
            log.warning("⚠️ Respuesta vacía del Main Agent.")
            return "❌ Disculpa, no pude procesar tu solicitud. Intenta de nuevo."

        # Limpieza de respuesta
        agent_response = str(agent_response).strip()
        log.info(f"✅ Main Agent respondió: {agent_response[:100]}...")

        # ===== PASO 3: SUPERVISOR OUTPUT =====
        log.info("🔍 PASO 3: Supervisor Output validando respuesta...")
        output_validation = await supervisor_output.validate(
            user_input=user_message, agent_response=agent_response
        )

        if isinstance(output_validation, dict):
            estado_out = output_validation.get("estado", "Aprobado")
            motivo_out = output_validation.get("motivo", "")
            sugerencia = output_validation.get("sugerencia", "")
        else:
            estado_out = (
                "Rechazado"
                if "rechazado" in output_validation.lower()
                or "revisión" in output_validation.lower()
                else "Aprobado"
            )
            motivo_out = output_validation if estado_out != "Aprobado" else ""
            sugerencia = ""

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

        log.info("✅ Respuesta aprobada por Supervisor Output")
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
    mode = params.get("hub.mode")
    challenge = params.get("hub.challenge")
    token = params.get("hub.verify_token")

    log.info(f"🔍 Verificación Meta: mode={mode}, token={token}, challenge={challenge}")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return PlainTextResponse(content=challenge)
    else:
        return JSONResponse(
            content={"error": "Invalid verification token"},
            status_code=403
        )


@app.post("/webhook")
async def webhook_receiver(request: Request):
    """Recibe mensajes de WhatsApp (Meta Webhooks)."""
    try:
        data = await request.json()
        log.info(f"📩 Webhook POST recibido desde Meta: {data}")

        entry = data.get("entry", [])[0]
        changes = entry.get("changes", [])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])

        if not messages:
            log.info("📭 No hay mensajes en la notificación.")
            return JSONResponse({"status": "ignored"})

        msg = messages[0]
        sender = msg["from"]
        text = msg.get("text", {}).get("body", "")

        log.info(f"📨 Mensaje recibido de {sender}: {text}")

        response = await process_user_message(
            user_message=text,
            chat_id=sender,
            channel="whatsapp"
        )

        try:
            await channel_manager.send_message(sender, response, channel="whatsapp")
        except Exception as e:
            log.error(f"❌ Error enviando respuesta final: {e}")

        return JSONResponse({"status": "success"})

    except Exception as e:
        log.error(f"❌ Error procesando webhook POST: {e}", exc_info=True)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    """Webhook para respuestas del encargado vía Telegram."""
    try:
        data = await request.json()
        log.info(f"📞 Webhook Telegram recibido: {data}")

        staff_response = data.get("message", {}).get("text", "")
        original_chat_id = data.get("context", {}).get("original_chat_id", "")

        if not staff_response or not original_chat_id:
            return JSONResponse({"status": "ignored", "reason": "missing data"})

        await channel_manager.send_message(
            original_chat_id, staff_response, channel="whatsapp"
        )

        log.info(f"✅ Respuesta del encargado enviada a {original_chat_id}")
        return JSONResponse({"status": "success"})

    except Exception as e:
        log.error(f"❌ Error en webhook Telegram: {e}", exc_info=True)
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
            "SubAgents: dispo/precios, info, interno",
        ],
    }

# =============================================================
# EJECUCIÓN
# =============================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
