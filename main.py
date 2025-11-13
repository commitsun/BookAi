"""
🚀 Main Entry Point - Sistema de Agentes con Orquestación + Idioma + Buffer
==================================================================
Flujo:
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
import asyncio
import pickle  # 🔧 FIX persistencia temporal del tracking

# =============================================================
# IMPORTS DE COMPONENTES
# =============================================================

from channels_wrapper.manager import ChannelManager
from core.main_agent import create_main_agent
from core.memory_manager import MemoryManager
from core.language_manager import language_manager
from agents.supervisor_input_agent import SupervisorInputAgent
from agents.supervisor_output_agent import SupervisorOutputAgent

# 🆕 InternoAgent v4 (ReAct)
from agents.interno_agent import InternoAgent
from core.escalation_manager import register_escalation, get_escalation

from core.message_buffer import MessageBufferManager
from core.consent_manager import consent_manager
from channels_wrapper.utils.text_utils import send_fragmented_async

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

app = FastAPI(title="HotelAI - Sistema de Agentes ReAct v4")

# =============================================================
# COMPONENTES GLOBALES
# =============================================================


memory_manager = MemoryManager()

# ✅ Propagar memory_manager a todos los agentes supervisores e internos
supervisor_input = SupervisorInputAgent(memory_manager=memory_manager)
supervisor_output = SupervisorOutputAgent(memory_manager=memory_manager)
interno_agent = InternoAgent(memory_manager=memory_manager)

channel_manager = ChannelManager()
buffer_manager = MessageBufferManager(idle_seconds=6.0)

# Diccionarios auxiliares
ESCALATION_TRACKING = {}   # message_id ↔ escalation_id
CHAT_LANG = {}             # chat_id ↔ idioma
TELEGRAM_PENDING_CONFIRMATIONS = {}  # chat_id → escalación pendiente de confirmación

log.info("✅ Sistema inicializado con Agente Interno v4 (ReAct)")

# =============================================================
# 🔧 FIX: persistencia mínima del mapeo (para reinicios de contenedor)
# =============================================================

TRACK_FILE = "/tmp/escalation_tracking.pkl"

def save_tracking():
    try:
        with open(TRACK_FILE, "wb") as f:
            pickle.dump(ESCALATION_TRACKING, f)
    except Exception as e:
        log.warning(f"⚠️ No se pudo guardar tracking: {e}")

def load_tracking():
    if os.path.exists(TRACK_FILE):
        try:
            with open(TRACK_FILE, "rb") as f:
                ESCALATION_TRACKING.update(pickle.load(f))
                log.info(f"📦 Cargado ESCALATION_TRACKING ({len(ESCALATION_TRACKING)}) desde disco")
        except Exception as e:
            log.warning(f"⚠️ No se pudo cargar tracking: {e}")

load_tracking()

# =============================================================
# PIPELINE PRINCIPAL
# =============================================================

async def process_user_message(user_message: str, chat_id: str, hotel_name: str = "Hotel", channel: str = "whatsapp") -> str:
    """
    Flujo principal:
      1. Detección de idioma
      2. Supervisor Input
      3. Main Agent
      4. Supervisor Output
      5. Escalación → InternoAgent v4
    """
    try:
        log.info(f"📨 Nuevo mensaje de {chat_id}: {user_message[:150]}")

        # ---------------------------------------------------------
        # 1️⃣ Detección de idioma
        try:
            guest_lang = language_manager.detect_language(user_message)
        except Exception:
            guest_lang = CHAT_LANG.get(chat_id, "es")
        CHAT_LANG[chat_id] = guest_lang
        log.info(f"🌐 Idioma detectado: {guest_lang}")

        # ---------------------------------------------------------
        # 2️⃣ Supervisor Input
        input_validation = await supervisor_input.validate(user_message)
        estado_in = input_validation.get("estado", "Aprobado")
        motivo_in = input_validation.get("motivo", "")

        if estado_in.lower() not in ["aprobado", "ok", "aceptable"]:
            log.warning(f"🚨 Mensaje rechazado por Supervisor Input: {motivo_in}")
            await interno_agent.escalate(
                guest_chat_id=chat_id,
                guest_message=user_message,
                escalation_type="inappropriate",
                reason=motivo_in,
                context="Rechazado por Supervisor Input"
            )
            return None  # Modo silencioso: solo escalamos

        # ---------------------------------------------------------
        # 🤝 Confirmación pendiente para escalar al encargado
        # ---------------------------------------------------------
        pending_consent = consent_manager.get_pending(chat_id)
        if pending_consent:
            decision = consent_manager.classify_reply(user_message)
            log.info(
                "🤝 Respuesta sobre confirmación pendiente (%s): %s",
                chat_id,
                decision,
            )

            if decision == "yes":
                consent_manager.clear(chat_id)

                context_details = pending_consent.context or "Confirmación manual desde InfoAgent"

                await interno_agent.escalate(
                    guest_chat_id=pending_consent.chat_id,
                    guest_message=pending_consent.guest_message,
                    escalation_type=pending_consent.escalation_type,
                    reason=pending_consent.reason,
                    context=f"{context_details}\nConfirmación huésped: {user_message}",
                )

                ack = language_manager.ensure_language(
                    "Perfecto, consulto con el encargado y te aviso en cuanto tenga una respuesta.",
                    guest_lang,
                )

                if memory_manager:
                    memory_manager.save(chat_id, "user", user_message)
                    memory_manager.save(chat_id, "assistant", ack)

                return ack

            if decision == "no":
                consent_manager.clear(chat_id)

                ack = language_manager.ensure_language(
                    "De acuerdo, si necesitas algo más estaré pendiente.",
                    guest_lang,
                )

                if memory_manager:
                    memory_manager.save(chat_id, "user", user_message)
                    memory_manager.save(chat_id, "assistant", ack)

                return ack

            reminder = language_manager.ensure_language(
                "Solo necesito saber si quieres que lo consulte con el encargado. Responde 'sí' o 'no', por favor.",
                guest_lang,
            )

            if memory_manager:
                memory_manager.save(chat_id, "user", user_message)
                memory_manager.save(chat_id, "assistant", reminder)

            return reminder

        # ---------------------------------------------------------
        # 3️⃣ Main Agent
        try:
            history = memory_manager.get_memory_as_messages(chat_id)
        except Exception as e:
            log.warning(f"⚠️ No se pudo obtener memoria: {e}")
            history = []

        async def send_inciso_callback(msg: str):
            localized = language_manager.ensure_language(msg, guest_lang)
            await channel_manager.send_message(chat_id, localized, channel=channel)

        main_agent = create_main_agent(
            memory_manager=memory_manager,
            send_callback=send_inciso_callback,
            interno_agent=interno_agent,  # ✅ Instancia compartida
            model_name="gpt-4o",
            temperature=0.3,
        )


        response_raw = await main_agent.ainvoke(
            user_input=user_message,
            chat_id=chat_id,
            hotel_name=hotel_name,
            chat_history=history,
        )

        if not response_raw:
            await interno_agent.escalate(
                guest_chat_id=chat_id,
                guest_message=user_message,
                escalation_type="info_not_found",
                reason="Main Agent no devolvió respuesta",
                context="Respuesta vacía o nula"
            )
            return None

        response_raw = response_raw.strip()
        log.info(f"🤖 Respuesta del MainAgent: {response_raw[:300]}")

        # ---------------------------------------------------------
        # 4️⃣ Supervisor Output
        output_validation = await supervisor_output.validate(
            user_input=user_message,
            agent_response=response_raw
        )
        estado_out = (output_validation.get("estado", "Aprobado") or "").lower()
        motivo_out = output_validation.get("motivo", "")

        if "aprobado" not in estado_out:
            log.warning(f"🚨 Respuesta rechazada por Supervisor Output: {motivo_out}")
            await interno_agent.escalate(
                guest_chat_id=chat_id,
                guest_message=user_message,
                escalation_type="bad_response",
                reason=motivo_out,
                context=f"Respuesta rechazada: {response_raw[:150]}"
            )
            return None

        localized = language_manager.ensure_language(response_raw, guest_lang)
        return localized

    except Exception as e:
        log.error(f"💥 Error crítico en pipeline: {e}", exc_info=True)
        await interno_agent.escalate(
            guest_chat_id=chat_id,
            guest_message=user_message,
            escalation_type="info_not_found",
            reason=f"Error crítico: {str(e)}",
            context="Excepción general en process_user_message"
        )
        return None


# =============================================================
# ENDPOINTS WEBHOOKS WHATSAPP
# =============================================================

@app.get("/webhook")
async def verify_webhook(request: Request):
    VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "midemo")
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(params.get("hub.challenge"))
    return JSONResponse({"error": "Invalid verification token"}, status_code=403)


@app.post("/webhook")
async def whatsapp_webhook(request: Request):
    """
    Webhook WhatsApp (Meta) + Buffer inteligente + Transcripción de audio (Whisper)
    """
    try:
        data = await request.json()
        value = data.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {})
        msg = value.get("messages", [{}])[0]
        sender = msg.get("from")
        msg_type = msg.get("type")

        text = ""

        # ==========================================================
        # 🗣️ Si es texto normal
        # ==========================================================
        if msg_type == "text":
            text = msg.get("text", {}).get("body", "")

        # ==========================================================
        # 🎧 Si es un audio → transcribir con Whisper
        # ==========================================================
        elif msg_type == "audio":
            from channels_wrapper.utils.media_utils import transcribe_audio
            from core.config import Settings as C

            media_id = msg.get("audio", {}).get("id")
            if media_id:
                log.info(f"🎧 Audio recibido (media_id={media_id}), iniciando transcripción...")
                text = transcribe_audio(media_id, C.WHATSAPP_TOKEN, C.OPENAI_API_KEY)
                log.info(f"📝 Transcripción completada: {text}")

        # ==========================================================
        # 🚫 Si no hay texto ni audio válido → ignorar
        # ==========================================================
        if not sender or not text:
            return JSONResponse({"status": "ignored"})

        log.info(f"💬 WhatsApp {sender}: {text}")

        # ==========================================================
        # 🧠 Buffer inteligente de mensajes (para agrupar texto)
        # ==========================================================
        async def _process_buffered(cid: str, combined_text: str, version: int):
            log.info(f"🧠 Procesando lote buffered v{version} → {cid}\n🧩 Mensajes combinados:\n{combined_text}")
            resp = await process_user_message(combined_text, cid, channel="whatsapp")

            if not resp:
                log.info(f"🔇 Escalación silenciosa {cid}")
                return

            async def send_to_channel(uid: str, txt: str):
                await channel_manager.send_message(uid, txt, channel="whatsapp")

            # Fragmentación y envío con ritmo humano
            from channels_wrapper.utils.text_utils import send_fragmented_async
            await send_fragmented_async(send_to_channel, cid, resp)


        # Añadir mensaje al buffer
        await buffer_manager.add_message(sender, text, _process_buffered)

        return JSONResponse({"status": "queued"})

    except Exception as e:
        log.error(f"❌ Error en webhook WhatsApp: {e}", exc_info=True)
        return JSONResponse({"status": "error"}, status_code=500)


# =============================================================
# TELEGRAM WEBHOOK - InternoAgent v4 (UNIFICADO)
# =============================================================

@app.post("/webhook/telegram")
async def telegram_webhook_handler(request: Request):
    """
    Webhook único para manejar:
      1️⃣ Respuesta del encargado a la ESCALACIÓN -> genera borrador
      2️⃣ Confirmación o ajustes del borrador -> envía o reformula
    """
    try:
        data = await request.json()
        message = data.get("message", {}) or {}
        chat = message.get("chat", {}) or {}

        chat_id = str(chat.get("id")) if chat.get("id") is not None else None
        text = (message.get("text") or "").strip()
        reply_to = message.get("reply_to_message", {}) or {}
        original_msg_id = reply_to.get("message_id")

        if not chat_id or not text:
            return JSONResponse({"status": "ignored"})

        log.info(f"💬 Telegram ({chat_id}): {text}")

        # =========================================================
        # 1️⃣ Confirmación o ajustes de un borrador pendiente
        # =========================================================
        if chat_id in TELEGRAM_PENDING_CONFIRMATIONS:
            escalation_id = TELEGRAM_PENDING_CONFIRMATIONS[chat_id]
            text_lower = text.lower()

            # ✅ Si dice "ok" → confirmar envío
            if any(k in text_lower for k in ["ok", "confirmo", "confirmar"]):
                confirmed = True
                adjustments = ""
            else:
                # 🧩 Si hay texto distinto de "ok" → se trata como ajustes
                confirmed = False
                adjustments = text

            resp = await interno_agent.send_confirmed_response(
                escalation_id=escalation_id,
                confirmed=confirmed,
                adjustments=adjustments,
            )

            # 🚀 Solo se limpia si ya se confirmó definitivamente
            if confirmed:
                TELEGRAM_PENDING_CONFIRMATIONS.pop(chat_id, None)

            # 🗂 Guardar tracking persistente
            save_tracking()

            await channel_manager.send_message(chat_id, f"{resp}", channel="telegram")
            log.info(f"✅ Procesado mensaje de confirmación/ajuste para escalación {escalation_id}")
            return JSONResponse({"status": "processed"})

        # =========================================================
        # 2️⃣ Respuesta nueva (reply al mensaje de escalación)
        # =========================================================
        if original_msg_id is not None:
            escalation_id = get_escalation(str(original_msg_id))  # 👈 usa la función global, no el diccionario local
            if not escalation_id:
                log.warning(f"⚠️ No se encontró escalación asociada a message_id={original_msg_id}")
            else:
                draft_result = await interno_agent.process_manager_reply(
                    escalation_id=escalation_id,
                    manager_reply=text,
                )

                TELEGRAM_PENDING_CONFIRMATIONS[chat_id] = escalation_id
                save_tracking()  # 🔧 guarda el tracking actualizado

                confirmation_msg = draft_result

                await channel_manager.send_message(chat_id, confirmation_msg, channel="telegram")
                log.info(f"📝 Borrador generado y enviado a {chat_id}")
                return JSONResponse({"status": "draft_sent"})

        log.info("ℹ️ Mensaje de Telegram ignorado (sin contexto de escalación activo).")
        return JSONResponse({"status": "ignored"})

    except Exception as e:
        log.error(f"💥 Error en Telegram webhook: {e}", exc_info=True)
        return JSONResponse({"status": "error"}, status_code=500)


# =============================================================
# HEALTHCHECK
# =============================================================

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "version": "v4-react",
        "description": "Sistema de agentes con InternoAgent v4 (ReAct) + buffer WhatsApp",
    }


# =============================================================
# LOCAL DEV
# =============================================================

if __name__ == "__main__":
    import uvicorn
    log.info("🚀 Iniciando servidor con InternoAgent v4 ReAct...")
    uvicorn.run(app, host="0.0.0.0", port=8000)
