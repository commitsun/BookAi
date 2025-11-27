"""Handlers del webhook de Telegram unificado."""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from core.constants import DEFAULT_HOTEL_NAME, WA_CANCEL_WORDS, WA_CONFIRM_WORDS
from core.escalation_manager import get_escalation
from core.message_utils import (
    build_kb_preview,
    extract_clean_draft,
    extract_kb_fields,
    format_superintendente_message,
    get_escalation_metadata,
    sanitize_wa_message,
)

log = logging.getLogger("TelegramWebhook")


def register_telegram_routes(app, state):
    """Registra el endpoint de Telegram y comparte estado con el pipeline."""

    @app.post("/webhook/telegram")
    async def telegram_webhook_handler(request: Request):
        """
        Webhook único para manejar:
          1️⃣ Respuesta del encargado a la ESCALACIÓN -> genera borrador
          2️⃣ Confirmación o ajustes del borrador -> envía o reformula
          3️⃣ Modo Superintendente y propuestas de KB
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

            log.info("💬 Telegram (%s): %s", chat_id, text)
            text_lower = text.lower()

            # --------------------------------------------------------
            # 1️⃣ Confirmación o ajustes de un borrador pendiente
            # --------------------------------------------------------
            if chat_id in state.telegram_pending_confirmations:
                state.superintendente_chats.pop(chat_id, None)

                pending_conf = state.telegram_pending_confirmations[chat_id]
                if isinstance(pending_conf, dict):
                    escalation_id = pending_conf.get("escalation_id")
                    manager_reply = pending_conf.get("manager_reply", "")
                else:
                    escalation_id = pending_conf
                    manager_reply = ""

                if any(k in text_lower for k in ["ok", "confirmo", "confirmar"]):
                    confirmed = True
                    adjustments = ""
                else:
                    confirmed = False
                    adjustments = text

                resp = await state.interno_agent.send_confirmed_response(
                    escalation_id=escalation_id,
                    confirmed=confirmed,
                    adjustments=adjustments,
                )

                if confirmed:
                    state.telegram_pending_confirmations.pop(chat_id, None)
                elif isinstance(pending_conf, dict):
                    state.telegram_pending_confirmations[chat_id] = {
                        "escalation_id": escalation_id,
                        "manager_reply": adjustments or manager_reply,
                    }

                state.tracking = state.telegram_pending_confirmations
                state.save_tracking()

                await state.channel_manager.send_message(chat_id, f"{resp}", channel="telegram")
                if confirmed and manager_reply:
                    meta = get_escalation_metadata(escalation_id or "")
                    esc_type = (meta.get("type") or "").lower()
                    reason = (meta.get("reason") or "").lower()

                    kb_allowed = esc_type in {"info_not_found", "manual"}
                    kb_allowed = kb_allowed and not state.telegram_pending_kb_addition.get(chat_id)
                    kb_allowed = kb_allowed and all(
                        term not in reason for term in ["inapropiad", "ofens", "rechaz", "error"]
                    )

                    if kb_allowed:
                        topic = manager_reply.split("\n")[0][:50]
                        kb_question = await state.interno_agent.ask_add_to_knowledge_base(
                            chat_id=chat_id,
                            escalation_id=escalation_id or "",
                            topic=topic,
                            response_content=manager_reply,
                            hotel_name=DEFAULT_HOTEL_NAME,
                            superintendente_agent=state.superintendente_agent,
                        )

                        state.telegram_pending_kb_addition[chat_id] = {
                            "escalation_id": escalation_id,
                            "topic": topic,
                            "content": manager_reply,
                            "hotel_name": DEFAULT_HOTEL_NAME,
                        }

                        await state.channel_manager.send_message(
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

                log.info("✅ Procesado mensaje de confirmación/ajuste para escalación %s", escalation_id)
                return JSONResponse({"status": "processed"})

            # --------------------------------------------------------
            # 3️⃣ Respuesta a preguntas de KB pendientes
            # --------------------------------------------------------
            if chat_id in state.telegram_pending_kb_addition:
                pending_kb = state.telegram_pending_kb_addition[chat_id]

                kb_response = await state.interno_agent.process_kb_response(
                    chat_id=chat_id,
                    escalation_id=pending_kb.get("escalation_id", ""),
                    manager_response=text,
                    topic=pending_kb.get("topic", ""),
                    draft_content=pending_kb.get("content", ""),
                    hotel_name=pending_kb.get("hotel_name", DEFAULT_HOTEL_NAME),
                    superintendente_agent=state.superintendente_agent,
                    pending_state=pending_kb,
                    source=pending_kb.get("source", "escalation"),
                )

                if isinstance(kb_response, (tuple, list)):
                    kb_response = " ".join(str(x) for x in kb_response)
                elif not isinstance(kb_response, str):
                    kb_response = str(kb_response)

                sent = False
                if "agregad" in kb_response.lower() or "✅" in kb_response:
                    state.telegram_pending_kb_addition.pop(chat_id, None)
                    sent = True

                await state.channel_manager.send_message(
                    chat_id,
                    kb_response,
                    channel="telegram",
                )

                if sent:
                    return JSONResponse({"status": "processed"})

                state.tracking = state.telegram_pending_confirmations
                state.save_tracking()
                return JSONResponse({"status": "processed"})

            # --------------------------------------------------------
            # 1️⃣ bis - Ruta explícita para Superintendente con mismo bot
            # --------------------------------------------------------
            if text_lower.startswith("/super_exit"):
                state.superintendente_chats.pop(chat_id, None)
                await state.channel_manager.send_message(
                    chat_id,
                    "Has salido del modo Superintendente.",
                    channel="telegram",
                )
                return JSONResponse({"status": "processed"})

            super_mode = text_lower.startswith("/super")
            in_super_session = (
                chat_id in state.superintendente_chats
                and chat_id not in state.telegram_pending_confirmations
                and chat_id not in state.telegram_pending_kb_addition
                and original_msg_id is None
            )

            # --------------------------------------------------------
            # 1️⃣ ter - Confirmación/ajuste de envío WhatsApp directo
            # --------------------------------------------------------
            if chat_id in state.superintendente_pending_wa:
                pending = state.superintendente_pending_wa[chat_id]
                guest_id = pending.get("guest_id")
                draft_msg = pending.get("message", "")

                if any(x in text_lower for x in WA_CONFIRM_WORDS):
                    log.info("[WA_CONFIRM] Enviando mensaje a %s desde %s", guest_id, chat_id)
                    await state.channel_manager.send_message(
                        guest_id,
                        draft_msg,
                        channel="whatsapp",
                    )
                    state.superintendente_pending_wa.pop(chat_id, None)
                    await state.channel_manager.send_message(
                        chat_id,
                        f"✅ Enviado a {guest_id}: {draft_msg}",
                        channel="telegram",
                    )
                    return JSONResponse({"status": "wa_sent"})

                if any(x in text_lower for x in WA_CANCEL_WORDS):
                    log.info("[WA_CONFIRM] Cancelado por %s", chat_id)
                    state.superintendente_pending_wa.pop(chat_id, None)
                    await state.channel_manager.send_message(
                        chat_id,
                        "Operación cancelada.",
                        channel="telegram",
                    )
                    return JSONResponse({"status": "wa_cancelled"})

                log.info("[WA_CONFIRM] Ajuste de borrador por %s", chat_id)
                state.superintendente_pending_wa[chat_id]["message"] = sanitize_wa_message(text)
                await state.channel_manager.send_message(
                    chat_id,
                    f"📝 Borrador actualizado:\n{text}\n\nResponde 'sí' para enviar o 'no' para descartar.",
                    channel="telegram",
                )
                return JSONResponse({"status": "wa_updated"})

            # --------------------------------------------------------
            # 1️⃣ ter-bis - Confirmación WA recuperando de memoria
            # --------------------------------------------------------
            if any(x in text_lower for x in WA_CONFIRM_WORDS):
                try:
                    recent = state.memory_manager.get_memory(chat_id, limit=10)
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
                            msg_to_send = sanitize_wa_message(msg_raw)
                            await state.channel_manager.send_message(
                                guest_id,
                                msg_to_send,
                                channel="whatsapp",
                            )
                            await state.memory_manager.save(chat_id, "system", f"[WA_SENT]|{guest_id}|{msg_to_send}")
                            await state.channel_manager.send_message(
                                chat_id,
                                f"✅ Mensaje enviado a {guest_id}:\n{msg_to_send}",
                                channel="telegram",
                            )
                            return JSONResponse({"status": "wa_sent_recovered"})
                except Exception as exc:
                    log.error("[WA_CONFIRM_RECOVERY] Error: %s", exc, exc_info=True)

            if super_mode or in_super_session:
                payload = text.split(" ", 1)[1].strip() if " " in text else ""
                hotel_name = state.superintendente_chats.get(chat_id, {}).get("hotel_name", DEFAULT_HOTEL_NAME)
                state.superintendente_chats[chat_id] = {"hotel_name": hotel_name}

                try:
                    response = await state.superintendente_agent.ainvoke(
                        user_input=payload or "Hola, ¿en qué puedo ayudarte?",
                        encargado_id=chat_id,
                        hotel_name=hotel_name,
                    )

                    marker = "[WA_DRAFT]|"
                    if marker in response:
                        draft_payload = response[response.index(marker):]
                        parts = draft_payload.split("|", 2)
                        if len(parts) == 3:
                            guest_id, msg_raw = parts[1], parts[2]
                            msg_to_send = sanitize_wa_message(msg_raw)
                            state.superintendente_pending_wa[chat_id] = {
                                "guest_id": guest_id,
                                "message": msg_to_send,
                            }
                            log.info("[WA_DRAFT] Registrado draft para %s desde %s", guest_id, chat_id)
                            try:
                                await state.memory_manager.save(
                                    conversation_id=chat_id,
                                    role="system",
                                    content=f"[WA_DRAFT]|{guest_id}|{msg_to_send}",
                                )
                            except Exception:
                                pass
                            preview = (
                                f"📝 Borrador WhatsApp para {guest_id}:\n{msg_to_send}\n\n"
                                "Responde 'sí' para enviar, 'no' para descartar o escribe ajustes."
                            )
                            await state.channel_manager.send_message(
                                chat_id,
                                preview,
                                channel="telegram",
                            )
                            return JSONResponse({"status": "wa_draft"})

                    kb_marker = "[KB_DRAFT]|"
                    if kb_marker in response:
                        marker_line = next((ln for ln in response.splitlines() if kb_marker in ln), "")
                        draft_payload = marker_line if marker_line else response[response.index(kb_marker):]

                        parts = draft_payload.split("|", 4)
                        if len(parts) >= 5:
                            _, kb_hotel, topic, category, kb_content = parts[:5]
                        else:
                            kb_hotel = hotel_name
                            topic = "Información"
                            category = "general"
                            kb_content = draft_payload.replace(kb_marker, "").strip()

                        state.telegram_pending_kb_addition[chat_id] = {
                            "escalation_id": "",
                            "topic": topic.strip(),
                            "content": kb_content.strip(),
                            "hotel_name": kb_hotel or hotel_name or DEFAULT_HOTEL_NAME,
                            "source": "superintendente",
                            "category": category.strip() or "general",
                        }

                        preview = build_kb_preview(topic, category, kb_content)
                        await state.channel_manager.send_message(
                            chat_id,
                            preview,
                            channel="telegram",
                        )
                        return JSONResponse({"status": "kb_draft"})

                    if "tema:" in response.lower() and "contenido:" in response.lower() and kb_marker not in response:
                        topic, category, content_block = extract_kb_fields(response, hotel_name)

                        state.telegram_pending_kb_addition[chat_id] = {
                            "escalation_id": "",
                            "topic": topic,
                            "content": content_block,
                            "hotel_name": hotel_name,
                            "source": "superintendente_fallback",
                            "category": category,
                        }

                        preview = build_kb_preview(topic, category, content_block)
                        await state.channel_manager.send_message(
                            chat_id,
                            preview,
                            channel="telegram",
                        )
                        return JSONResponse({"status": "kb_draft_fallback"})

                    await state.channel_manager.send_message(
                        chat_id,
                        format_superintendente_message(response),
                        channel="telegram",
                    )

                    return JSONResponse({"status": "processed"})

                except Exception as exc:
                    log.error("Error procesando en Superintendente (mismo bot): %s", exc)
                    await state.channel_manager.send_message(
                        chat_id,
                        f"❌ Error procesando tu solicitud: {exc}",
                        channel="telegram",
                    )
                    return JSONResponse({"status": "error"}, status_code=500)

            # --------------------------------------------------------
            # 2️⃣ Respuesta nueva (reply al mensaje de escalación)
            # --------------------------------------------------------
            if original_msg_id is not None:
                state.superintendente_chats.pop(chat_id, None)

                escalation_id = get_escalation(str(original_msg_id))
                if not escalation_id:
                    log.warning("⚠️ No se encontró escalación asociada a message_id=%s", original_msg_id)
                else:
                    draft_result = await state.interno_agent.process_manager_reply(
                        escalation_id=escalation_id,
                        manager_reply=text,
                    )

                    state.telegram_pending_confirmations[chat_id] = {
                        "escalation_id": escalation_id,
                        "manager_reply": text,
                    }
                    state.tracking = state.telegram_pending_confirmations
                    state.save_tracking()

                    confirmation_msg = extract_clean_draft(draft_result)

                    await state.channel_manager.send_message(chat_id, confirmation_msg, channel="telegram")
                    log.info("📝 Borrador generado y enviado a %s", chat_id)
                    return JSONResponse({"status": "draft_sent"})

            log.info("ℹ️ Mensaje de Telegram ignorado (sin contexto de escalación activo).")
            return JSONResponse({"status": "ignored"})

        except Exception as exc:
            log.error("💥 Error en Telegram webhook: %s", exc, exc_info=True)
            return JSONResponse({"status": "error"}, status_code=500)
