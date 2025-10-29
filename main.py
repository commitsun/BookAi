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
    """
    Procesa un mensaje del usuario siguiendo el flujo completo:
    SupervisorInput → MainAgent → (opcional) SupervisorOutput → respuesta final.
    Maneja también la escalación hacia Telegram cuando procede.
    """
    try:
        log.info(f"📨 Nuevo mensaje de {chat_id} en {channel}: {user_message[:200]}...")

        # ===== PASO 1: SUPERVISOR INPUT =====
        input_validation = await supervisor_input.validate(user_message)

        # input_validation SIEMPRE es dict normalizado según nuestro agente
        estado_in = input_validation.get("estado", "Aprobado")
        motivo_in = input_validation.get("motivo", "")

        # 🚨 Caso: mensaje marcado como NO APROBADO (insultos, amenazas, etc.)
        if estado_in.lower() not in ["aprobado", "ok", "aceptable"]:
            log.warning(f"⚠️ Mensaje rechazado por Supervisor Input: {motivo_in}")

            escalation_msg = (
                "🔔 NUEVA CONSULTA ESCALADA\n\n"
                f"📱 Chat ID: {chat_id}\n\n"
                "🚨 MENSAJE RECHAZADO POR SUPERVISOR INPUT\n\n"
                f"Chat ID: {chat_id}\n"
                f"Hotel: {hotel_name}\n\n"
                "Mensaje del usuario:\n"
                f"{user_message}\n\n"
                "Motivo del rechazo:\n"
                f"{motivo_in}\n\n"
                "Por favor, intervén manualmente.\n"
            )

            # avisar al encargado humano por Telegram
            await interno_agent.anotify_staff(escalation_msg, chat_id)

            # respuesta segura al huésped
            return (
                "🕓 Gracias por tu mensaje. Lo estamos revisando con nuestro equipo."
            )

        # ===== PASO 2: MAIN AGENT =====
        try:
            history = memory_manager.get_memory(chat_id)
        except Exception as e:
            log.warning(f"⚠️ No se pudo obtener memoria de {chat_id}: {e}")
            history = []

        async def send_inciso_callback(message: str):
            """
            Callback que las tools pueden usar (por ejemplo, disponibilidad lenta)
            para avisar al huésped tipo 'un momento por favor'.
            Este mensaje NO pasa por SupervisorOutput.
            """
            try:
                await channel_manager.send_message(chat_id, message, channel=channel)
            except Exception as e:
                log.error(f"❌ Error enviando inciso al canal {channel}: {e}")

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
        log.info(f"✅ Main Agent respondió (raw): {agent_response[:500]}...")

        # =====================================================
        # NORMALIZACIÓN DE RESPUESTA
        # =====================================================

        # 1. Si viene con marca de inciso '##INCISO##', lo tratamos como aviso temporal:
        if "##inciso##" in agent_response.lower():
            safe_resp = agent_response.replace("##INCISO##", "", 1).strip()
            log.info("ℹ️ Respuesta INCISO detectada. No se audita ni se escala.")
            return safe_resp or "🕓 Estoy verificando tu solicitud, un momento por favor."

        # 2. Heurística anti-dobles respuestas:
        #    El agente a veces concatena un bloque técnico + reformulación amable final.
        #    Nos quedamos con el último bloque separado por doble salto.
        if "\n\n" in agent_response:
            parts = [p.strip() for p in agent_response.split("\n\n") if p.strip()]
            if len(parts) > 1:
                agent_response = parts[-1]
                log.info("✂️ Limpieza heurística aplicada. Enviamos solo el último bloque al huésped.")

        # 3. Detección de mensajes tipo "estoy verificando", "un momento", etc.
        #    Estos no deben escalar ni pasar por auditoría.
        lower_resp = agent_response.lower()
        INCISO_PATTERNS = [
            "un momento por favor",
            "permíteme un momento",
            "estoy verificando",
            "estoy procesando tu solicitud",
            "estoy comprobando la información",
            "consultando con el equipo",
        ]
        if any(pat in lower_resp for pat in INCISO_PATTERNS):
            log.info("ℹ️ Respuesta tipo INCISO detectada por heurística. No se audita.")
            return agent_response

        # ===== PASO 3: SUPERVISOR OUTPUT =====
        output_validation = await supervisor_output.validate(
            user_input=user_message,
            agent_response=agent_response
        )

        # output_validation SIEMPRE dict normalizado según nuestro agente
        estado_out = output_validation.get("estado", "Aprobado")
        motivo_out = output_validation.get("motivo", "")
        sugerencia_out = output_validation.get("sugerencia", "")

        # Caso feliz → todo aprobado, se envía al huésped directamente
        if estado_out.lower() in ["aprobado", "ok"]:
            return agent_response

        # Caso "revisión necesaria" → normalmente son matices menores.
        # No escalamos duro. Mandamos la respuesta tal cual al huésped, SIN molestar al encargado.
        if "revisión" in estado_out.lower():
            log.warning(f"⚠️ Supervisor Output pidió revisión menor: {motivo_out}")
            return agent_response

        # Caso RECHAZADO explícito → sí se escala al encargado humano
        log.warning(f"⚠️ Respuesta rechazada por Supervisor Output: {motivo_out}")

        escalation_msg = (
            "🔔 NUEVA CONSULTA ESCALADA\n\n"
            f"📱 Chat ID: {chat_id}\n\n"
            "🚨 RESPUESTA RECHAZADA POR SUPERVISOR OUTPUT\n\n"
            f"Chat ID: {chat_id}\n"
            f"Hotel: {hotel_name}\n\n"
            "Mensaje del usuario:\n"
            f"{user_message}\n\n"
            "Respuesta del agente (RECHAZADA):\n"
            f"{agent_response}\n\n"
            "Motivo del rechazo:\n"
            f"{motivo_out}\n\n"
            "Sugerencia:\n"
            f"{sugerencia_out}\n\n"
            "Por favor, proporciona una respuesta manual adecuada."
        )

        await interno_agent.anotify_staff(escalation_msg, chat_id)

        # Lo que oye el huésped mientras tanto:
        return (
            "🕓 Permíteme un momento para verificar esa información con nuestro equipo."
        )

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
    """
    Webhook de WhatsApp (Meta).
    Extrae el texto del huésped, ejecuta el pipeline
    y responde al mismo chat por WhatsApp.
    """
    try:
        data = await request.json()
        if not data:
            log.warning("⚠️ Webhook recibido vacío o sin JSON válido.")
            return JSONResponse({"status": "ignored", "reason": "empty body"})

        if "entry" not in data:
            log.warning(f"⚠️ Webhook sin 'entry': {data}")
            return JSONResponse({"status": "ignored", "reason": "no entry"})

        entry_list = data.get("entry", [])
        if not entry_list:
            log.warning("⚠️ Webhook con 'entry' vacío.")
            return JSONResponse({"status": "ignored", "reason": "empty entry"})

        entry = entry_list[0]
        changes = entry.get("changes", [])
        if not changes:
            log.warning("⚠️ Webhook sin 'changes'.")
            return JSONResponse({"status": "ignored", "reason": "no changes"})

        value = changes[0].get("value", {})
        messages = value.get("messages", [])
        if not messages:
            log.info("ℹ️ Webhook sin mensajes (posible handshake inicial).")
            return JSONResponse({"status": "ignored", "reason": "no messages"})

        msg = messages[0]
        sender = msg.get("from")
        text = msg.get("text", {}).get("body", "")

        if not sender or not text:
            log.warning(f"⚠️ Mensaje inválido recibido: {msg}")
            return JSONResponse({"status": "ignored", "reason": "invalid message"})

        log.info(f"📨 Mensaje recibido de {sender}: {text}")

        response_text = await process_user_message(
            user_message=text,
            chat_id=sender,
            channel="whatsapp"
        )

        await channel_manager.send_message(sender, response_text, channel="whatsapp")
        return JSONResponse({"status": "success"})

    except Exception as e:
        log.error(f"❌ Error procesando webhook POST: {e}", exc_info=True)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    """
    Webhook de Telegram.
    Se usa para que el encargado humano responda con Reply,
    y esa respuesta llegue al huésped por WhatsApp.
    """
    try:
        data = await request.json()
        log.info(f"📞 Webhook Telegram recibido: {json.dumps(data, indent=2)}")

        message = data.get("message", {})
        text = message.get("text", "")
        reply_to = message.get("reply_to_message", {})

        if not text:
            return JSONResponse({"status": "ignored", "reason": "no text"})

        original_msg_id = reply_to.get("message_id")
        if not original_msg_id:
            log.warning("⚠️ Mensaje Telegram sin reply_to → ignorado.")
            return JSONResponse({"status": "ignored", "reason": "no reply reference"})

        original_chat_id = PENDING_ESCALATIONS.get(original_msg_id)
        if not original_chat_id:
            log.warning("⚠️ No se encontró chat_id asociado al mensaje respondido.")
            return JSONResponse({"status": "ignored", "reason": "no linked chat"})

        # reenviamos la respuesta humana directamente al huésped por WhatsApp
        await channel_manager.send_message(original_chat_id, text.strip(), channel="whatsapp")
        log.info(f"✅ Respuesta del encargado reenviada a huésped {original_chat_id}: {text[:200]}")

        # limpieza: ya no necesitamos mantener ese pending
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
