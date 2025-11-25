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
from collections import deque
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

from agents.interno_agent import InternoAgent
from agents.superintendente_agent import SuperintendenteAgent
from core.escalation_manager import register_escalation, get_escalation
from core import escalation_db as escalation_db_store
from core.db import supabase

from core.message_buffer import MessageBufferManager
from channels_wrapper.utils.text_utils import send_fragmented_async
from tools.interno_tool import ESCALATIONS_STORE

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

# Silenciar el spam de LangSmith cuando se alcanzan límites o fallos de red
for noisy_logger in ("langsmith", "langsmith.client"):
    logging.getLogger(noisy_logger).setLevel(logging.ERROR)

log = logging.getLogger("Main")

# Silenciar ruido de LangSmith cuando se alcanza el límite mensual
logging.getLogger("langsmith").setLevel(logging.ERROR)
logging.getLogger("langsmith.client").setLevel(logging.ERROR)
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
channel_manager = ChannelManager()
buffer_manager = MessageBufferManager(idle_seconds=6.0)
interno_agent = InternoAgent(memory_manager=memory_manager)
superintendente_agent = SuperintendenteAgent(
    memory_manager=memory_manager,
    supabase_client=supabase,
    channel_manager=channel_manager,
)
log.info("✅ SuperintendenteAgent inicializado")

# Diccionarios auxiliares
ESCALATION_TRACKING = {}   # message_id ↔ escalation_id
CHAT_LANG = {}             # chat_id ↔ idioma
TELEGRAM_PENDING_CONFIRMATIONS = {}  # chat_id → escalación pendiente de confirmación
TELEGRAM_PENDING_KB_ADDITION = {}  # {chat_id: {escalation_id, topic, content, hotel_name}}
SUPERINTENDENTE_CHATS = {}  # {encargado_id: {hotel_name, last_message, ...}}
SUPERINTENDENTE_PENDING_WA = {}  # {chat_id: {guest_id, message}}
PROCESSED_WHATSAPP_IDS = set()
PROCESSED_WHATSAPP_QUEUE = deque(maxlen=5000)

log.info("✅ Sistema inicializado con Agente Interno (ReAct)")

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


def _get_escalation_metadata(escalation_id: str) -> dict:
    """
    Recupera metadatos de una escalación desde memoria o DB.
    Sirve para evitar flujos no deseados (ej. KB en insultos).
    """
    try:
        esc = ESCALATIONS_STORE.get(escalation_id)
        if esc:
            return {
                "type": esc.escalation_type,
                "reason": esc.escalation_reason,
                "context": esc.context,
            }
    except Exception:
        pass

    try:
        record = escalation_db_store.get_escalation(escalation_id)
        if record:
            return {
                "type": record.get("escalation_type"),
                "reason": record.get("escalation_reason") or record.get("reason", ""),
                "context": record.get("context", ""),
            }
    except Exception as e:
        log.warning(f"⚠️ No se pudo obtener metadatos de escalación {escalation_id}: {e}")

    return {}


def _extract_clean_draft(text: str) -> str:
    """
    Devuelve solo el borrador limpio generado por el InternoAgent,
    eliminando razonamiento intermedio o metadata que no debería ver el encargado.
    """
    if not text:
        return text

    draft_markers = [
        "📝 *BORRADOR DE RESPUESTA PROPUESTO:*",
        "📝 *Nuevo borrador generado",
        "📝 BORRADOR",
    ]

    metadata_markers = [
        "[- Origen:",
        "- Origen:",
        "- Acción requerida:",
        "- Contenido:",
        "- Evidencia:",
        "- Estado:",
        "Utilizo la herramienta",
        "¿Desea que esta directriz",
    ]

    lines = text.splitlines()
    clean_lines = []
    in_draft = False
    skip_next_blank = False

    for line in lines:
        stripped = line.strip()

        if any(marker in line for marker in draft_markers):
            in_draft = True
            clean_lines.append(line)
            skip_next_blank = False
            continue

        if any(marker in line for marker in metadata_markers):
            skip_next_blank = True
            continue

        if skip_next_blank and not stripped:
            skip_next_blank = False
            continue

        if in_draft:
            clean_lines.append(line)
        elif not any(marker in line for marker in metadata_markers):
            clean_lines.append(line)

    result = "\n".join(clean_lines).strip()

    if not in_draft:
        return text

    return result or text


def _sanitize_wa_message(msg: str) -> str:
    """Devuelve un mensaje corto y limpio para WhatsApp (solo la primera línea útil)."""
    if not msg:
        return msg
    lines = [ln.strip() for ln in msg.splitlines() if ln.strip()]
    core = lines[0] if lines else msg
    return core.strip().strip('\"“”')


def _format_superintendente_message(text: str) -> str:
    """
    Aplica un formato ligero y consistente a las respuestas del Superintendente
    sin alterar su contenido ni funcionalidad.
    """
    if not text:
        return text

    # Evitar doble formateo
    if text.strip().startswith("╭─ Superintendente"):
        return text

    # Limpieza básica y normalización de listas
    lines = [ln.rstrip() for ln in text.strip().splitlines()]
    compact = []
    blank_seen = False
    for ln in lines:
        if not ln.strip():
            if blank_seen:
                continue
            blank_seen = True
            compact.append("")
            continue
        blank_seen = False
        stripped = ln.strip()
        if stripped.lower().startswith("[superintendente]"):
            stripped = stripped.split("]", 1)[-1].strip()
        if stripped.startswith("- "):
            stripped = f"• {stripped[2:].strip()}"
        compact.append(stripped)

    body_lines = []
    for ln in compact:
        if not ln:
            body_lines.append("┆")
        else:
            body_lines.append(f"┆ {ln}")

    body = "\n".join(body_lines).strip()

    header = "✨ Panel del Superintendente"
    footer = "━━━━━━━━━━━━━━━━━━━━"
    return f"╭ {header}\n{body}\n╰ {footer}"

# =============================================================
# PIPELINE PRINCIPAL
# =============================================================


async def process_user_message(
    user_message: str,
    chat_id: str,
    hotel_name: str = "Hotel",
    channel: str = "whatsapp",
) -> str | None:
    """
    Flujo principal:
      1. Detección de idioma
      2. Supervisor Input
      3. Main Agent
      4. Supervisor Output
      5. Escalación → InternoAgent
    """
    try:
        log.info(f"📨 Nuevo mensaje de {chat_id}: {user_message[:150]}")

        # ---------------------------------------------------------
        # 1️⃣ Detección de idioma
        # ---------------------------------------------------------
        prev_lang = CHAT_LANG.get(chat_id)
        try:
            guest_lang = language_manager.detect_language(user_message, prev_lang=prev_lang)
        except Exception:
            guest_lang = prev_lang or "es"
        CHAT_LANG[chat_id] = guest_lang
        log.info(f"🌐 Idioma detectado: {guest_lang}")

        # ---------------------------------------------------------
        # 2️⃣ Supervisor Input
        # ---------------------------------------------------------
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
                context="Rechazado por Supervisor Input",
            )
            return None  # Modo silencioso: solo escalamos

        # ---------------------------------------------------------
        # 3️⃣ Main Agent
        # ---------------------------------------------------------
        try:
            history = memory_manager.get_memory_as_messages(chat_id)
        except Exception as e:
            log.warning(f"⚠️ No se pudo obtener memoria: {e}")
            history = []

        # Callback que envía respuestas intermedias (incisos)
        async def send_inciso_callback(msg: str):
            try:
                localized = language_manager.ensure_language(msg, guest_lang)
                await channel_manager.send_message(chat_id, localized, channel=channel)
            except Exception as e:
                log.error(f"❌ Error enviando inciso: {e}")

        # ✅ Creación del MainAgent con configuración centralizada (ModelConfig)
        main_agent = create_main_agent(
            memory_manager=memory_manager,
            send_callback=send_inciso_callback,
            interno_agent=interno_agent,  # instancia compartida global
        )

        # 🚀 Invocar el MainAgent con historial y contexto
        response_raw = await main_agent.ainvoke(
            user_input=user_message,
            chat_id=chat_id,
            hotel_name=hotel_name,
            chat_history=history,
        )

        # 🧩 Validación de respuesta del MainAgent
        if not response_raw:
            await interno_agent.escalate(
                guest_chat_id=chat_id,
                guest_message=user_message,
                escalation_type="info_not_found",
                reason="Main Agent no devolvió respuesta",
                context="Respuesta vacía o nula",
            )
            return None

        response_raw = response_raw.strip()
        log.info(f"🤖 Respuesta del MainAgent: {response_raw[:300]}")

        # ---------------------------------------------------------
        # 4️⃣ Supervisor Output
        # ---------------------------------------------------------
        output_validation = await supervisor_output.validate(
            user_input=user_message,
            agent_response=response_raw,
        )
        estado_out = (output_validation.get("estado", "Aprobado") or "").lower()
        motivo_out = output_validation.get("motivo", "")

        if "aprobado" not in estado_out:
            log.warning(f"🚨 Respuesta rechazada por Supervisor Output: {motivo_out}")

            # 🧠 Recuperar historial reciente del huésped
            hist_text = ""
            try:
                raw_hist = memory_manager.get_memory(chat_id, limit=6)
                if raw_hist:
                    lines = []
                    for m in raw_hist:
                        role = m.get("role")
                        prefix = "Huésped" if role == "user" else "Asistente"
                        lines.append(f"{prefix}: {m.get('content','')}")
                    hist_text = "\n".join(lines)
            except Exception as e:
                log.warning(f"⚠️ No se pudo recuperar historial para escalación: {e}")

            # 🧩 Combinar contexto con historial
            context_full = (
                f"Respuesta rechazada: {response_raw[:150]}\n\n"
                f"🧠 Historial reciente:\n{hist_text}"
            )

            await interno_agent.escalate(
                guest_chat_id=chat_id,
                guest_message=user_message,  # último mensaje literal
                escalation_type="bad_response",
                reason=motivo_out,
                context=context_full,
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
            context="Excepción general en process_user_message",
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
        msg_id = msg.get("id")

        text = ""

        # ==========================================================
        # 🔁 Filtro de duplicados (reintentos del webhook de Meta)
        # ==========================================================
        if msg_id:
            if msg_id in PROCESSED_WHATSAPP_IDS:
                log.info(f"↩️ WhatsApp duplicado ignorado (msg_id={msg_id})")
                return JSONResponse({"status": "duplicate"})
            # Mantener un buffer acotado de IDs ya procesados
            if len(PROCESSED_WHATSAPP_QUEUE) >= PROCESSED_WHATSAPP_QUEUE.maxlen:
                old = PROCESSED_WHATSAPP_QUEUE.popleft()
                PROCESSED_WHATSAPP_IDS.discard(old)
            PROCESSED_WHATSAPP_QUEUE.append(msg_id)
            PROCESSED_WHATSAPP_IDS.add(msg_id)

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

            media_id = msg.get("audio", {}).get("id")
            if media_id:
                log.info(f"🎧 Audio recibido (media_id={media_id}), iniciando transcripción...")
                whatsapp_token = os.getenv("WHATSAPP_TOKEN", "")
                openai_key = os.getenv("OPENAI_API_KEY", "")
                text = transcribe_audio(media_id, whatsapp_token, openai_key)
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
            log.info(
                f"🧠 Procesando lote buffered v{version} → {cid}\n"
                f"🧩 Mensajes combinados:\n{combined_text}"
            )
            resp = await process_user_message(combined_text, cid, channel="whatsapp")

            if not resp:
                log.info(f"🔇 Escalación silenciosa {cid}")
                return

            async def send_to_channel(uid: str, txt: str):
                await channel_manager.send_message(uid, txt, channel="whatsapp")

            # Fragmentación y envío con ritmo humano
            await send_fragmented_async(send_to_channel, cid, resp)

        # Añadir mensaje al buffer
        await buffer_manager.add_message(sender, text, _process_buffered)

        return JSONResponse({"status": "queued"})

    except Exception as e:
        log.error(f"❌ Error en webhook WhatsApp: {e}", exc_info=True)
        return JSONResponse({"status": "error"}, status_code=500)


# =============================================================
# TELEGRAM WEBHOOK - InternoAgent (UNIFICADO)
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
        text_lower = text.lower()

        # =========================================================
        # 1️⃣ Confirmación o ajustes de un borrador pendiente
        # =========================================================
        if chat_id in TELEGRAM_PENDING_CONFIRMATIONS:
            # ⚖️ Prioriza flujos del InternoAgent (evita colisión con modo Superintendente)
            SUPERINTENDENTE_CHATS.pop(chat_id, None)

            pending_conf = TELEGRAM_PENDING_CONFIRMATIONS[chat_id]
            if isinstance(pending_conf, dict):
                escalation_id = pending_conf.get("escalation_id")
                manager_reply = pending_conf.get("manager_reply", "")
            else:
                escalation_id = pending_conf
                manager_reply = ""

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
            elif isinstance(pending_conf, dict):
                TELEGRAM_PENDING_CONFIRMATIONS[chat_id] = {
                    "escalation_id": escalation_id,
                    "manager_reply": adjustments or manager_reply,
                }

            # 🗂 Guardar tracking persistente
            save_tracking()

            await channel_manager.send_message(chat_id, f"{resp}", channel="telegram")
            if confirmed and manager_reply:
                meta = _get_escalation_metadata(escalation_id or "")
                esc_type = (meta.get("type") or "").lower()
                reason = (meta.get("reason") or "").lower()

                kb_allowed = esc_type in {"info_not_found", "manual"}
                kb_allowed = kb_allowed and not TELEGRAM_PENDING_KB_ADDITION.get(chat_id)
                kb_allowed = kb_allowed and all(term not in reason for term in ["inapropiad", "ofens", "rechaz", "error"])

                if kb_allowed:
                    topic = manager_reply.split("\n")[0][:50]
                    kb_question = await interno_agent.ask_add_to_knowledge_base(
                        chat_id=chat_id,
                        escalation_id=escalation_id or "",
                        topic=topic,
                        response_content=manager_reply,
                        hotel_name="Hotel Default",
                        superintendente_agent=superintendente_agent,
                    )

                    TELEGRAM_PENDING_KB_ADDITION[chat_id] = {
                        "escalation_id": escalation_id,
                        "topic": topic,
                        "content": manager_reply,
                        "hotel_name": "Hotel Default",
                    }

                    await channel_manager.send_message(
                        chat_id,
                        kb_question,
                        channel="telegram",
                    )

                    log.info("Pregunta KB enviada: %s", escalation_id)
                else:
                    log.info(
                        "⏭️ Se omite sugerencia de KB para escalación %s (tipo: %s, motivo: %s)",
                        escalation_id,
                        esc_type or "desconocido",
                        reason or "n/a",
                    )

            log.info(f"✅ Procesado mensaje de confirmación/ajuste para escalación {escalation_id}")
            return JSONResponse({"status": "processed"})

        # =========================================================
        # 1️⃣ bis - Ruta explícita para Superintendente con mismo bot
        # Trigger: /super ...  o modo persistido sin reply ni flujos activos
        # =========================================================
        if text_lower.startswith("/super_exit"):
            SUPERINTENDENTE_CHATS.pop(chat_id, None)
            await channel_manager.send_message(
                chat_id,
                "Has salido del modo Superintendente.",
                channel="telegram",
            )
            return JSONResponse({"status": "processed"})

        super_mode = text_lower.startswith("/super")
        in_super_session = (
            chat_id in SUPERINTENDENTE_CHATS
            and chat_id not in TELEGRAM_PENDING_CONFIRMATIONS
            and chat_id not in TELEGRAM_PENDING_KB_ADDITION
            and original_msg_id is None
        )

        # =========================================================
        # 1️⃣ ter - Confirmación/ajuste de envío WhatsApp directo (Superintendente)
        # =========================================================
        if chat_id in SUPERINTENDENTE_PENDING_WA:
            pending = SUPERINTENDENTE_PENDING_WA[chat_id]
            resp_lower = text_lower
            guest_id = pending.get("guest_id")
            draft_msg = pending.get("message", "")

            if any(x in resp_lower for x in ["enviar", "ok", "confirmar", "si", "sí", "sii", "siii", "dale", "manda"]):
                log.info(f"[WA_CONFIRM] Enviando mensaje a {guest_id} desde {chat_id}")
                await channel_manager.send_message(
                    guest_id,
                        draft_msg,
                    channel="whatsapp",
                )
                SUPERINTENDENTE_PENDING_WA.pop(chat_id, None)
                await channel_manager.send_message(
                    chat_id,
                    f"✅ Enviado a {guest_id}: {draft_msg}",
                    channel="telegram",
                )
                return JSONResponse({"status": "wa_sent"})

            if any(x in resp_lower for x in ["cancel", "cancelar", "no"]):
                log.info(f"[WA_CONFIRM] Cancelado por {chat_id}")
                SUPERINTENDENTE_PENDING_WA.pop(chat_id, None)
                await channel_manager.send_message(
                    chat_id,
                    "Operación cancelada.",
                    channel="telegram",
                )
                return JSONResponse({"status": "wa_cancelled"})

            # Ajuste del borrador
            log.info(f"[WA_CONFIRM] Ajuste de borrador por {chat_id}")
            SUPERINTENDENTE_PENDING_WA[chat_id]["message"] = _sanitize_wa_message(text)
            await channel_manager.send_message(
                chat_id,
                f"📝 Borrador actualizado:\n{text}\n\nResponde 'sí' para enviar o 'no' para descartar.",
                channel="telegram",
            )
            return JSONResponse({"status": "wa_updated"})

        # =========================================================
        # 1️⃣ ter-bis - Confirmación WA sin estado en memoria volátil (recuperar de memoria_manager)
        # =========================================================
        if any(x in text_lower for x in ["enviar", "ok", "confirmar", "si", "sí", "sii", "siii", "dale", "manda"]):
            try:
                recent = memory_manager.get_memory(chat_id, limit=10)
                marker = "[WA_DRAFT]|"
                last_draft = None
                for msg in reversed(recent):
                    content = msg.get("content", "")
                    if marker in content:
                        last_draft = content[content.index(marker):]
                        break
                if last_draft:
                    parts = last_draft.split("|", 2)
                    if len(parts) == 3:
                        guest_id, msg_raw = parts[1], parts[2]
                        msg = _sanitize_wa_message(msg_raw)
                        await channel_manager.send_message(
                            guest_id,
                            msg,
                            channel="whatsapp",
                        )
                        await memory_manager.save(chat_id, "system", f"[WA_SENT]|{guest_id}|{msg}")
                        await channel_manager.send_message(
                            chat_id,
                            f"✅ Mensaje enviado a {guest_id}:\n{msg}",
                            channel="telegram",
                        )
                        return JSONResponse({"status": "wa_sent_recovered"})
            except Exception as exc:
                log.error(f"[WA_CONFIRM_RECOVERY] Error: {exc}", exc_info=True)

        if super_mode or in_super_session:
            payload = text.split(" ", 1)[1].strip() if " " in text else ""
            hotel_name = SUPERINTENDENTE_CHATS.get(chat_id, {}).get("hotel_name", "Hotel Default")
            SUPERINTENDENTE_CHATS[chat_id] = {"hotel_name": hotel_name}

            try:
                response = await superintendente_agent.ainvoke(
                    user_input=payload or "Hola, ¿en qué puedo ayudarte?",
                    encargado_id=chat_id,
                    hotel_name=hotel_name,
                )

                # 🚦 Detectar borrador WA en la respuesta (aunque no sea el inicio)
                marker = "[WA_DRAFT]|"
                if marker in response:
                    draft_payload = response[response.index(marker):]
                    parts = draft_payload.split("|", 2)
                    if len(parts) == 3:
                        guest_id, msg_raw = parts[1], parts[2]
                        msg = _sanitize_wa_message(msg_raw)
                        SUPERINTENDENTE_PENDING_WA[chat_id] = {
                            "guest_id": guest_id,
                            "message": msg,
                        }
                        log.info(f"[WA_DRAFT] Registrado draft para {guest_id} desde {chat_id}")
                        try:
                            await memory_manager.save(
                                conversation_id=chat_id,
                                role="system",
                                content=f"[WA_DRAFT]|{guest_id}|{msg}",
                            )
                        except Exception:
                            pass
                        preview = (
                            f"📝 Borrador WhatsApp para {guest_id}:\n{msg}\n\n"
                            "Responde 'sí' para enviar, 'no' para descartar o escribe ajustes."
                        )
                        await channel_manager.send_message(
                            chat_id,
                            preview,
                            channel="telegram",
                        )
                        return JSONResponse({"status": "wa_draft"})

                await channel_manager.send_message(
                    chat_id,
                    _format_superintendente_message(response),
                    channel="telegram",
                )

                return JSONResponse({"status": "processed"})

            except Exception as exc:
                log.error(f"Error procesando en Superintendente (mismo bot): {exc}")
                await channel_manager.send_message(
                    chat_id,
                    f"❌ Error procesando tu solicitud: {exc}",
                    channel="telegram",
                )
                return JSONResponse({"status": "error"}, status_code=500)

        # =========================================================
        # 2️⃣ Respuesta nueva (reply al mensaje de escalación)
        # =========================================================
        if original_msg_id is not None:
            # ✅ Evita que el modo Superintendente capture respuestas a escalaciones
            SUPERINTENDENTE_CHATS.pop(chat_id, None)

            escalation_id = get_escalation(str(original_msg_id))
            if not escalation_id:
                log.warning(
                    f"⚠️ No se encontró escalación asociada a message_id={original_msg_id}"
                )
            else:
                draft_result = await interno_agent.process_manager_reply(
                    escalation_id=escalation_id,
                    manager_reply=text,
                )

                TELEGRAM_PENDING_CONFIRMATIONS[chat_id] = {
                    "escalation_id": escalation_id,
                    "manager_reply": text,
                }
                save_tracking()  # 🔧 guarda el tracking actualizado

                confirmation_msg = _extract_clean_draft(draft_result)

                await channel_manager.send_message(chat_id, confirmation_msg, channel="telegram")
                log.info(f"📝 Borrador generado y enviado a {chat_id}")
                return JSONResponse({"status": "draft_sent"})

        # =========================================================
        # 3️⃣ Respuesta a preguntas de KB pendientes
        # =========================================================
        if chat_id in TELEGRAM_PENDING_KB_ADDITION:
            pending_kb = TELEGRAM_PENDING_KB_ADDITION[chat_id]

            kb_response = await interno_agent.process_kb_response(
                chat_id=chat_id,
                escalation_id=pending_kb.get("escalation_id", ""),
                manager_response=text,
                topic=pending_kb.get("topic", ""),
                draft_content=pending_kb.get("content", ""),
                hotel_name=pending_kb.get("hotel_name", "Hotel Default"),
                superintendente_agent=superintendente_agent,
            )

            if "agregad" in kb_response.lower() or "✅" in kb_response:
                TELEGRAM_PENDING_KB_ADDITION.pop(chat_id, None)

            await channel_manager.send_message(
                chat_id,
                kb_response,
                channel="telegram",
            )

            save_tracking()
            return JSONResponse({"status": "processed"})

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
        "description": "Sistema de agentes con InternoAgent ReAct + buffer WhatsApp",
    }


# =============================================================
# LOCAL DEV
# =============================================================

if __name__ == "__main__":
    import uvicorn

    log.info("🚀 Iniciando servidor con InternoAgent ReAct...")
    uvicorn.run(app, host="0.0.0.0", port=8000)
