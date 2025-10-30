"""
🚀 Main Entry Point - Sistema de Agentes con Orquestación + Idioma + Buffer
==================================================================
Flujo:
WhatsApp → Supervisor Input → Main Agent → Supervisor Output → WhatsApp
                     ↓                ↓
                  Interno          Interno
                     ↓                ↓
                 Telegram         Telegram

Funciones clave:
- Detección dinámica del idioma del huésped (último mensaje manda)
- Respuestas al huésped siempre en SU idioma actual
- Escalación al encargado en español
- Mensajes del encargado → pulidos y traducidos al idioma del huésped
- ✅ Buffer inteligente de mensajes para WhatsApp
"""

import os
import json
import warnings
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
import asyncio  # necesario para el buffer

# =============================================================
# IMPORTS DE COMPONENTES
# =============================================================

from channels_wrapper.manager import ChannelManager
from core.main_agent import create_main_agent
from core.memory_manager import MemoryManager
from core.language_manager import language_manager
from agents.supervisor_input_agent import SupervisorInputAgent
from agents.supervisor_output_agent import SupervisorOutputAgent
from agents.interno_agent import InternoAgent as InternoAgentV2
from core.message_buffer import MessageBufferManager  # ✅ añadido

# =============================================================
# CONFIG GLOBAL / LOGGING
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
# FASTAPI APP
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
buffer_manager = MessageBufferManager(idle_seconds=6.0)  # ✅ nuevo buffer

# Relación Telegram ↔ WhatsApp para replies humanos
PENDING_ESCALATIONS = {}

# Idioma actual del huésped por chat_id
CHAT_LANG = {}

log.info("✅ Sistema inicializado correctamente")

# =============================================================
# PIPELINE PRINCIPAL
# =============================================================

async def process_user_message(
    user_message: str,
    chat_id: str,
    hotel_name: str = "Hotel",
    channel: str = "whatsapp"
) -> str:
    """
    Procesa el mensaje del huésped y devuelve la respuesta FINAL en el idioma actual del huésped.
    Flujo:
      1. Detectar idioma del huésped y guardarlo.
      2. Supervisor Input modera / decide si debemos escalar ya.
      3. MainAgent genera respuesta.
      4. Limpieza de respuesta (incisos, duplicados).
      5. Supervisor Output audita.
      6. Entrega final al huésped traducida a SU idioma.
    """

    try:
        log.info(f"📨 Nuevo mensaje de {chat_id} en {channel}: {user_message[:200]}...")

        # =========================================================
        # 1. DETECTAR / ACTUALIZAR IDIOMA DEL HUÉSPED
        # =========================================================
        try:
            detected_lang = language_manager.detect_language(user_message)
            CHAT_LANG[chat_id] = detected_lang  # <-- SIEMPRE pisamos con el último idioma detectado
            guest_lang = detected_lang
            log.info(f"🌐 Idioma detectado para {chat_id}: {guest_lang}")
        except Exception as e:
            log.warning(f"⚠️ No se pudo detectar idioma para {chat_id}: {e}")
            guest_lang = CHAT_LANG.get(chat_id, "es")
            CHAT_LANG[chat_id] = guest_lang

        # =========================================================
        # 2. SUPERVISOR INPUT (moderación de la ENTRADA)
        # =========================================================
        input_validation = await supervisor_input.validate(user_message)
        estado_in = input_validation.get("estado", "Aprobado")
        motivo_in = input_validation.get("motivo", "")

        # Caso NO aprobado → escalamos al encargado humano
        if estado_in.lower() not in ["aprobado", "ok", "aceptable"]:
            log.warning(f"⚠️ Mensaje rechazado por Supervisor Input: {motivo_in}")

            escalation_msg_es = (
                "🔔 NUEVA CONSULTA ESCALADA\n\n"
                f"📱 Chat ID: {chat_id}\n\n"
                "🚨 MENSAJE RECHAZADO POR SUPERVISOR INPUT\n\n"
                f"Chat ID: {chat_id}\n"
                f"Hotel: {hotel_name}\n\n"
                "Mensaje del huésped:\n"
                f"{user_message}\n\n"
                "Motivo del rechazo:\n"
                f"{motivo_in}\n\n"
                "Por favor, intervén manualmente."
            )
            await interno_agent.anotify_staff(escalation_msg_es, chat_id)

            safe_reply_base = (
                "Gracias por tu mensaje. Lo estamos revisando con nuestro equipo."
            )
            safe_reply_localized = language_manager.ensure_language(
                safe_reply_base,
                guest_lang
            )
            return safe_reply_localized

        # =========================================================
        # 3. MAIN AGENT (orquestador principal)
        # =========================================================
        try:
            history = memory_manager.get_memory(chat_id)
        except Exception as e:
            log.warning(f"⚠️ No se pudo obtener memoria de {chat_id}: {e}")
            history = []

        async def send_inciso_callback(message: str):
            """
            Callback para tools internas (por ejemplo, 'estoy consultando disponibilidad...').
            - Se envía directo al huésped
            - Se adapta al idioma actual del huésped
            - NO pasa por supervisor_output ni escalación
            """
            try:
                inciso_localized = language_manager.ensure_language(
                    message,
                    guest_lang
                )
                await channel_manager.send_message(
                    chat_id,
                    inciso_localized,
                    channel=channel
                )
            except Exception as e:
                log.error(f"❌ Error enviando inciso al canal {channel}: {e}")

        main_agent = create_main_agent(
            memory_manager=memory_manager,
            send_callback=send_inciso_callback,
            model_name="gpt-4o",
            temperature=0.3,
        )

        agent_response_raw = await main_agent.ainvoke(
            user_input=user_message,
            chat_id=chat_id,
            hotel_name=hotel_name,
            chat_history=history,
        )

        if not agent_response_raw or not agent_response_raw.strip():
            fallback_msg = (
                "Disculpa, no pude procesar tu solicitud. "
                "¿Podrías reformularla, por favor?"
            )
            return language_manager.ensure_language(
                fallback_msg,
                guest_lang
            )

        agent_response_raw = str(agent_response_raw).strip()
        log.info(f"✅ Main Agent respondió (raw): {agent_response_raw[:500]}...")

        # =========================================================
        # 4. LIMPIEZA / NORMALIZACIÓN DE LA RESPUESTA
        # =========================================================
        if "##inciso##" in agent_response_raw.lower():
            clean_inciso = agent_response_raw.replace("##INCISO##", "", 1).strip()
            if not clean_inciso:
                clean_inciso = "Un momento por favor, estoy verificando la información."
            return language_manager.ensure_language(
                clean_inciso,
                guest_lang
            )

        final_candidate = agent_response_raw
        if "\n\n" in agent_response_raw:
            parts = [p.strip() for p in agent_response_raw.split("\n\n") if p.strip()]
            # En lugar de quedarnos con el último bloque, unimos todo el texto coherente
            final_candidate = "\n\n".join(dict.fromkeys(parts))  # elimina duplicados pero conserva todo el contenido
            log.info("✂️ Limpieza ajustada: respuesta completa sin recortes.")


        lower_resp = final_candidate.lower()
        INCISO_PATTERNS = [
            "un momento por favor",
            "permíteme un momento",
            "permiteme un momento",
            "estoy verificando",
            "estoy procesando tu solicitud",
            "estoy comprobando la información",
            "consultando con el equipo",
        ]
        if any(pat in lower_resp for pat in INCISO_PATTERNS):
            return language_manager.ensure_language(
                final_candidate,
                guest_lang
            )

        # =========================================================
        # 5. SUPERVISOR OUTPUT (auditoría de la SALIDA)
        # =========================================================
        output_validation = await supervisor_output.validate(
            user_input=user_message,
            agent_response=final_candidate
        )

        estado_out = (output_validation.get("estado", "Aprobado") or "").lower()
        motivo_out = output_validation.get("motivo", "")
        sugerencia_out = output_validation.get("sugerencia", "")

        if (
            "aprobado" in estado_out
            or "revisión" in estado_out
            or "revision" in estado_out
        ):
            localized = language_manager.ensure_language(
                final_candidate,
                guest_lang
            )
            return localized

        log.warning(f"⚠️ Respuesta rechazada por Supervisor Output: {motivo_out}")

        escalation_msg_es = (
            "🔔 NUEVA CONSULTA ESCALADA\n\n"
            f"📱 Chat ID: {chat_id}\n\n"
            "🚨 RESPUESTA RECHAZADA POR SUPERVISOR OUTPUT\n\n"
            f"Chat ID: {chat_id}\n"
            f"Hotel: {hotel_name}\n\n"
            "Mensaje del huésped:\n"
            f"{user_message}\n\n"
            "Respuesta del agente (RECHAZADA):\n"
            f"{final_candidate}\n\n"
            "Motivo del rechazo:\n"
            f"{motivo_out}\n\n"
            "Sugerencia:\n"
            f"{sugerencia_out}\n\n"
            "Por favor, proporciona una respuesta manual adecuada."
        )
        await interno_agent.anotify_staff(escalation_msg_es, chat_id)

        hold_msg = (
            "Permíteme un momento para verificar esa información con nuestro equipo."
        )
        hold_msg_localized = language_manager.ensure_language(
            hold_msg,
            guest_lang
        )
        return hold_msg_localized

    except Exception as e:
        log.error(f"❌ Error en process_user_message: {e}", exc_info=True)
        fallback_err = "Disculpa, ha ocurrido un error al procesar tu mensaje."
        guest_lang = CHAT_LANG.get(chat_id, "es")
        return language_manager.ensure_language(
            fallback_err,
            guest_lang
        )

# =============================================================
# ENDPOINTS WHATSAPP / TELEGRAM
# =============================================================

@app.get("/webhook")
async def verify_webhook(request: Request):
    """Verificación de Webhook para Meta (WhatsApp)."""
    VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "midemo")

    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(params.get("hub.challenge"))
    return JSONResponse({"error": "Invalid verification token"}, status_code=403)


@app.post("/webhook")
async def webhook_receiver(request: Request):
    """
    Webhook de WhatsApp (Meta) con integración del BUFFER.
    Ahora los mensajes se acumulan durante unos segundos de inactividad
    antes de ser procesados por el pipeline.
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
            log.info("ℹ️ Webhook sin mensajes (posible validación inicial).")
            return JSONResponse({"status": "ignored", "reason": "no messages"})

        msg = messages[0]
        sender = msg.get("from")
        text = msg.get("text", {}).get("body", "")

        if not sender or not text:
            log.warning(f"⚠️ Mensaje inválido recibido: {msg}")
            return JSONResponse({"status": "ignored", "reason": "invalid message"})

        log.info(f"📨 Mensaje recibido de {sender}: {text}")

        # =========================================================
        # 🔄 NUEVO: ENVIAMOS EL MENSAJE AL BUFFER
        # =========================================================
        async def _process_buffered(conversation_id: str, combined_text: str, version: int):
            """Callback que se ejecuta cuando el buffer expira."""
            try:
                log.info(f"🧠 Procesando lote buffered v{version} → {conversation_id}: {combined_text}")
                response_text = await process_user_message(
                    user_message=combined_text,
                    chat_id=conversation_id,
                    channel="whatsapp"
                )
                await channel_manager.send_message(conversation_id, response_text, channel="whatsapp")
                log.info(f"📤 Respuesta enviada a {conversation_id} (versión {version})")
            except Exception as e:
                log.error(f"❌ Error en callback buffered: {e}", exc_info=True)

        # Enviar mensaje al buffer manager
        await buffer_manager.add_message(
            conversation_id=sender,
            text=text,
            process_callback=_process_buffered,
        )

        return JSONResponse({"status": "queued"})

    except Exception as e:
        log.error(f"❌ Error procesando webhook POST: {e}", exc_info=True)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    """
    Webhook de Telegram.
    El encargado humano responde con Reply en Telegram,
    y reenviamos al huésped:
      - suavizado en tono hotelero
      - traducido al idioma ACTUAL del huésped.
    """
    try:
        data = await request.json()
        log.info(f"📞 Webhook Telegram recibido: {json.dumps(data, indent=2)}")

        message = data.get("message", {})
        text_from_staff = message.get("text", "")
        reply_to = message.get("reply_to_message", {})

        if not text_from_staff:
            return JSONResponse({"status": "ignored", "reason": "no text"})

        original_msg_id = reply_to.get("message_id")
        if not original_msg_id:
            log.warning("⚠️ Mensaje Telegram sin reply_to → ignorado.")
            return JSONResponse({"status": "ignored", "reason": "no reply reference"})

        original_chat_id = PENDING_ESCALATIONS.get(original_msg_id)
        if not original_chat_id:
            log.warning("⚠️ No se encontró chat_id asociado al mensaje respondido.")
            return JSONResponse({"status": "ignored", "reason": "no linked chat"})

        guest_lang = CHAT_LANG.get(original_chat_id, "es")

        polished_for_guest = language_manager.polish_for_guest(
            raw_message=text_from_staff,
            guest_lang=guest_lang,
        )

        await channel_manager.send_message(
            original_chat_id,
            polished_for_guest.strip(),
            channel="whatsapp"
        )

        log.info(
            f"✅ Respuesta del encargado enviada a huésped {original_chat_id} "
            f"({guest_lang}): {polished_for_guest[:200]}"
        )

        PENDING_ESCALATIONS.pop(original_msg_id, None)
        return JSONResponse({"status": "success"})

    except Exception as e:
        log.error(f"❌ Error procesando webhook Telegram: {e}", exc_info=True)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

# =============================================================
# HEALTHCHECK / ROOT
# =============================================================

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "version": "2.3-buffered",
        "buffer": True,
        "description": "Sistema de agentes con buffer de mensajes y supervisión",
    }


@app.get("/")
async def root():
    return {
        "service": "HotelAI - Sistema de Agentes",
        "version": "2.3",
        "architecture": "orchestrator + language-aware routing + buffered input",
        "components": [
            "Supervisor Input",
            "Main Agent (Orchestrator)",
            "Supervisor Output",
            "Language Manager",
            "Telegram Bridge (Interno)",
            "Message Buffer",
        ],
    }


# =============================================================
# LOCAL DEV
# =============================================================

if __name__ == "__main__":
    import uvicorn
    log.info("🚀 Iniciando servidor FastAPI con buffer habilitado...")
    uvicorn.run(app, host="0.0.0.0", port=8000)
