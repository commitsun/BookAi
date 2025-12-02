"""Handlers del webhook de Telegram unificado."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import os
from datetime import datetime
from typing import Any

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
from core.db import get_conversation_history

log = logging.getLogger("TelegramWebhook")
AUTO_KB_PROMPT_ENABLED = os.getenv("AUTO_KB_PROMPT_ENABLED", "false").lower() == "true"


def register_telegram_routes(app, state):
    """Registra el endpoint de Telegram y comparte estado con el pipeline."""

    def _extract_phone(text: str) -> str | None:
        """Extrae el primer número estilo teléfono con 6-15 dígitos."""
        if not text:
            return None
        match = re.search(r"\+?\d{6,15}", text.replace(" ", ""))
        return match.group(0) if match else None

    def _is_short_confirmation(text: str) -> bool:
        """
        Confirma solo respuestas cortas (ej. 'ok', 'sí') y evita frases largas
        que contengan 'si' por accidente (ej. 'que si necesita pañuelos').
        """
        clean = re.sub(r"[¡!¿?.]", "", (text or "").lower()).strip()
        tokens = [t for t in re.findall(r"[a-záéíóúñ]+", clean) if t]

        yes_words = {"ok", "okay", "okey", "si", "sí", "vale", "va", "dale", "listo", "confirmo", "confirmar"}
        if clean in yes_words:
            return True

        return 0 < len(tokens) <= 2 and all(tok in yes_words for tok in tokens)

    def _is_short_rejection(text: str) -> bool:
        """
        Detecta cancelaciones breves (ej. 'no', 'cancelar') para flujos con confirmación.
        """
        clean = re.sub(r"[¡!¿?.]", "", (text or "").lower()).strip()
        tokens = [t for t in re.findall(r"[a-záéíóúñ]+", clean) if t]

        no_words = {"no", "nop", "nel", "cancelar", "cancelado", "descartar", "rechazar", "omitir"}
        if clean in no_words:
            return True

        return 0 < len(tokens) <= 2 and all(tok in no_words for tok in tokens)

    def _is_short_wa_confirmation(text: str) -> bool:
        """
        Confirma envío WA solo con respuestas breves (ej. 'si', 'ok', 'enviar')
        y evita disparar con frases largas que incluyan la palabra 'si'.
        """
        clean = re.sub(r"[¡!¿?.]", "", (text or "").lower()).strip()
        tokens = [t for t in re.findall(r"[a-záéíóúñ]+", clean) if t]

        confirm_words = set(WA_CONFIRM_WORDS) | {"vale", "listo"}
        if clean in confirm_words:
            return True

        return 0 < len(tokens) <= 2 and all(tok in confirm_words for tok in tokens)

    def _is_short_wa_cancel(text: str) -> bool:
        """
        Cancela envío WA solo con respuestas breves (ej. 'no', 'cancelar')
        para evitar falsos positivos por subcadenas (ej. 'buenos').
        """
        clean = re.sub(r"[¡!¿?.]", "", (text or "").lower()).strip()
        tokens = [t for t in re.findall(r"[a-záéíóúñ]+", clean) if t]

        cancel_words = set(WA_CANCEL_WORDS) | {"cancelado", "cancelo", "cancela"}
        if clean in cancel_words:
            return True

        return 0 < len(tokens) <= 2 and all(tok in cancel_words for tok in tokens)

    def _looks_like_new_instruction(text: str) -> bool:
        """
        Detecta si el encargado está cambiando de tema (ej. mandar mensaje, pedir historial)
        para no atrapar la solicitud dentro de un flujo previo.
        """
        if not text:
            return False

        action_terms = {
            "mandale",
            "mándale",
            "enviale",
            "envíale",
            "manda",
            "mensaje",
            "whatsapp",
            "historial",
            "convers",
            "broadcast",
            "plantilla",
            "resumen",
            "agrega",
            "añade",
            "anade",
            "actualiza",
            "actualizar",
            "base de cono",
            "kb",
            "elimina",
            "borra",
            "quitar",
        }
        return any(term in text for term in action_terms) or bool(_extract_phone(text))

    async def _collect_conversations(guest_id: str, limit: int = 10):
        """Recupera y deduplica mensajes del huésped desde DB + runtime."""
        if not state.memory_manager:
            return []

        clean_id = str(guest_id).replace("+", "").strip()

        db_msgs = await asyncio.to_thread(
            get_conversation_history,
            clean_id,
            limit * 3,  # pedir más por ruido/system
            None,
        )
        try:
            runtime_msgs = state.memory_manager.runtime_memory.get(clean_id, [])
        except Exception:
            runtime_msgs = []

        def _parse_ts(ts: Any) -> float:
            try:
                if isinstance(ts, datetime):
                    return ts.timestamp()
                ts_str = str(ts).replace("Z", "")
                return datetime.fromisoformat(ts_str).timestamp()
            except Exception:
                return 0.0

        combined_sorted = sorted((db_msgs or []) + (runtime_msgs or []), key=lambda m: _parse_ts(m.get("created_at")))
        convos = combined_sorted[-limit:] if combined_sorted else []

        seen = set()
        deduped = []
        for msg in convos:
            key = (
                msg.get("role", "assistant"),
                (msg.get("content") or "").strip(),
                str(msg.get("created_at")),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(msg)

        return deduped

    async def _render_history(guest_id: str, mode: str, limit: int = 10) -> str:
        """Devuelve texto listo para enviar (raw o resumen IA)."""
        convos = await _collect_conversations(guest_id, limit=limit)
        if not convos:
            return f"🧠 Historial ({guest_id})\nNo hay mensajes recientes."

        def _fmt_ts(ts: Any) -> str:
            try:
                if isinstance(ts, datetime):
                    return ts.strftime("%d/%m %H:%M")
                ts_str = str(ts).replace("Z", "")
                return datetime.fromisoformat(ts_str).strftime("%d/%m %H:%M")
            except Exception:
                return ""

        lines = []
        for msg in convos:
            role = msg.get("role", "assistant")
            prefix = {"user": "Huésped", "assistant": "Asistente", "system": "Sistema", "tool": "Tool"}.get(
                role, "Asistente"
            )
            ts = _fmt_ts(msg.get("created_at"))
            ts_suffix = f" · {ts}" if ts else ""
            content = msg.get("content", "").strip()
            lines.append(f"- {prefix}{ts_suffix}: {content}")

        formatted = "\n".join(lines)
        if mode == "original":
            return f"🗂️ Conversación recuperada ({len(convos)})\n{formatted}"

        llm = getattr(state.superintendente_agent, "llm", None)
        if not llm:
            return f"🧠 Historial ({len(convos)})\n{formatted}\n\n(Sin modelo disponible para resumir)"

        try:
            system = (
                "Eres el Superintendente. Resume el historial para el encargado: puntos clave, dudas y acciones pendientes. "
                "No inventes nada y sé conciso."
            )
            user_msg = "\n".join(lines)
            resp = await llm.ainvoke(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ]
            )
            summary = (getattr(resp, "content", None) or "").strip()
            if not summary:
                return f"🧠 Historial ({len(convos)})\n{formatted}\n\n(No se pudo generar resumen)"
            return f"🧠 Resumen de {guest_id} ({len(convos)} msg)\n{summary}"
        except Exception as exc:
            log.warning("No se pudo resumir historial: %s", exc)
            return f"🧠 Historial ({len(convos)})\n{formatted}\n\n(No se pudo generar resumen: {exc})"

    def _format_removal_preview(pending: dict) -> str:
        """Texto amigable para mostrar borrador de eliminación de KB."""
        total = int(pending.get("total_matches", 0) or 0)
        preview = pending.get("preview") or []
        criteria = pending.get("criteria") or ""
        date_from = pending.get("date_from") or ""
        date_to = pending.get("date_to") or ""

        def _sanitize_preview_snippet(text: str) -> str:
            if not text:
                return ""
            lines = []
            for ln in text.splitlines():
                low = ln.lower()
                if "borrador para agregar" in low or "[kb_" in low or "[kb-" in low:
                    continue
                lines.append(ln.strip())
            cleaned = " ".join(l for l in lines if l).strip()
            return cleaned[:320] + ("…" if len(cleaned) > 320 else "")

        header = [f"🧹 Borrador para eliminar de la KB ({total} registro(s))."]
        if criteria:
            header.append(f"Criterio: {criteria}")
        if date_from or date_to:
            header.append(f"Rango: {date_from or 'n/a'} → {date_to or 'n/a'}")

        body_lines = []
        for item in preview:
            topic = item.get("topic") or "Entrada"
            fecha = item.get("fecha") or ""
            snippet = _sanitize_preview_snippet(item.get("snippet") or "")
            body_lines.append(f"- {fecha} {topic}: {snippet}")

        footer = (
            "\n✅ Responde 'ok' para eliminar estos registros.\n"
            "📝 Di qué conservar o ajusta el criterio para refinar.\n"
            "❌ Responde 'no' para cancelar."
        )

        if body_lines and total <= 12:
            return "\n".join(header + body_lines) + footer

        return "\n".join(header) + footer

    def _apply_removal_adjustments(pending: dict, manager_text: str) -> dict:
        """
        Refina la selección:
        - Si el texto sugiere conservar (conserva/deja/mantén), excluye coincidencias de la eliminación.
        - En otro caso, limita la eliminación solo a los registros que contengan los términos.
        """
        if not pending:
            return pending

        matches = pending.get("matches") or []
        if not matches:
            return pending

        text_lower = (manager_text or "").lower()
        raw_terms = re.findall(r"[a-záéíóúñ0-9]{3,}", text_lower)
        if not raw_terms:
            return pending

        is_keep_intent = any(key in text_lower for key in {"conserv", "deja", "mant", "mantén", "qued"})

        target_ids = []
        for entry in matches:
            blob = f"{entry.get('topic','')} {entry.get('content','')}".lower()
            eid = entry.get("id")
            has_term = any(term in blob for term in raw_terms)
            if is_keep_intent:
                if not has_term:
                    target_ids.append(eid)
            else:
                if has_term:
                    target_ids.append(eid)

        preview = []
        for entry in matches:
            if entry.get("id") not in target_ids:
                continue
            preview.append(
                {
                    "id": entry.get("id"),
                    "topic": entry.get("topic"),
                    "fecha": entry.get("timestamp_display"),
                    "snippet": (entry.get("content") or "")[:260],
                }
            )
            if len(preview) >= 5:
                break

        return {
            **pending,
            "target_ids": target_ids,
            "kept_ids": [],
            "preview": preview,
            "total_matches": len(target_ids),
        }

    async def _rewrite_wa_draft(llm, base_message: str, adjustments: str) -> str:
        """
        Reescribe un borrador de WhatsApp con instrucciones adicionales.
        Mantiene tono cordial y conciso, sin emojis ni firmas.
        """
        clean_base = sanitize_wa_message(base_message or "")
        clean_adj = sanitize_wa_message(adjustments or "")

        if not clean_adj:
            return clean_base

        # Sin modelo: combina de forma determinista
        if not llm:
            if clean_base and clean_adj:
                return _clean_wa_payload(f"{clean_base}. {clean_adj}")
            return _clean_wa_payload(clean_base or clean_adj)

        # Con modelo: pedir una sola frase lista para enviar
        system = (
            "Eres el asistente del encargado de un hotel. "
            "Genera un único mensaje corto de WhatsApp en español neutro, tono cordial y directo. "
            "Incluye las ideas del mensaje base y los ajustes. "
            "No añadas instrucciones, confirmaciones ni comillas; entrega solo el texto listo para enviar."
        )
        user_msg = (
            "Mensaje base:\n"
            f"{clean_base or 'N/A'}\n\n"
            "Ajustes solicitados:\n"
            f"{clean_adj}\n\n"
            "Devuelve solo el mensaje final en una línea."
        )

        try:
            response = await llm.ainvoke(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ]
            )

            text = (getattr(response, "content", None) or "").strip()
            if not text:
                return _clean_wa_payload(clean_adj)

            return _clean_wa_payload(text)
        except Exception as exc:
            log.warning("No se pudo reformular borrador WA: %s", exc)
            return _clean_wa_payload(clean_adj or clean_base)

    def _clean_wa_payload(msg: str) -> str:
        """
        Limpia un borrador de WA para dejar solo el mensaje al huésped,
        removiendo instrucciones accidentales generadas por el modelo.
        """
        base = sanitize_wa_message(msg or "")
        if not base:
            return base

        # Limpia etiquetas internas
        base = re.sub(r"\[\s*superintendente\s*\]", "", base, flags=re.IGNORECASE).strip()

        cut_markers = [
            "borrador",
            "confirma",
            "confirmar",
            "por favor",
            "ok para",
            "ok para",
            "ok p",
            "plantilla",
        ]
        lower = base.lower()
        cuts = [lower.find(m) for m in cut_markers if lower.find(m) > 0]
        if cuts:
            base = base[: min(cuts)].strip()

        return base.strip()

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
            phone_hint = _extract_phone(text)

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
                    kb_allowed = kb_allowed and AUTO_KB_PROMPT_ENABLED
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
                        cause = "deshabilitado" if not AUTO_KB_PROMPT_ENABLED else f"tipo/motivo ({esc_type}/{reason})"
                        log.info(
                            "⏭️ Se omite sugerencia de KB para escalación %s (%s)",
                            escalation_id,
                            cause,
                        )

                log.info("✅ Procesado mensaje de confirmación/ajuste para escalación %s", escalation_id)
                return JSONResponse({"status": "processed"})

            # --------------------------------------------------------
            # 3️⃣ bis - Confirmación/ajustes de eliminación en KB
            # --------------------------------------------------------
            if chat_id in state.telegram_pending_kb_removal:
                pending_rm = state.telegram_pending_kb_removal[chat_id]

                if _looks_like_new_instruction(text_lower):
                    log.info("[KB_REMOVE] Nueva instrucción detectada, se cancela borrador (%s)", chat_id)
                    state.telegram_pending_kb_removal.pop(chat_id, None)
                    await state.channel_manager.send_message(
                        chat_id,
                        "📌 Guardé el borrador de eliminación y sigo con tu nueva solicitud.",
                        channel="telegram",
                    )
                    return JSONResponse({"status": "kb_remove_released"})

                if _is_short_confirmation(text_lower):
                    target_ids = pending_rm.get("target_ids") or []
                    result = await state.superintendente_agent.handle_kb_removal(
                        hotel_name=pending_rm.get("hotel_name", DEFAULT_HOTEL_NAME),
                        target_ids=target_ids,
                        encargado_id=chat_id,
                        note=text,
                        criteria=pending_rm.get("criteria", ""),
                    )
                    state.telegram_pending_kb_removal.pop(chat_id, None)
                    await state.channel_manager.send_message(
                        chat_id,
                        result.get("message") if isinstance(result, dict) else str(result),
                        channel="telegram",
                    )
                    return JSONResponse({"status": "kb_remove_confirmed"})

                if _is_short_rejection(text_lower):
                    state.telegram_pending_kb_removal.pop(chat_id, None)
                    await state.channel_manager.send_message(
                        chat_id,
                        "Operación cancelada. No se eliminó nada de la base de conocimientos.",
                        channel="telegram",
                    )
                    return JSONResponse({"status": "kb_remove_cancelled"})

                refined = _apply_removal_adjustments(pending_rm, text)
                state.telegram_pending_kb_removal[chat_id] = refined

                targets = refined.get("target_ids") or []
                if not targets:
                    await state.channel_manager.send_message(
                        chat_id,
                        "No hay registros para eliminar con ese ajuste. Indica otro criterio o responde 'no' para cancelar.",
                        channel="telegram",
                    )
                    return JSONResponse({"status": "kb_remove_empty"})

                preview_txt = _format_removal_preview(refined)
                await state.channel_manager.send_message(
                    chat_id,
                    preview_txt,
                    channel="telegram",
                )
                return JSONResponse({"status": "kb_remove_updated"})

            # --------------------------------------------------------
            # 3️⃣ ter - Confirmación de eliminación recuperando borrador perdido
            # --------------------------------------------------------
            if (
                _is_short_confirmation(text_lower)
                and chat_id not in state.telegram_pending_kb_removal
                and chat_id not in state.telegram_pending_kb_addition
                and chat_id not in state.superintendente_pending_wa
            ):
                try:
                    recent = state.memory_manager.get_memory(chat_id, limit=15) if state.memory_manager else []
                    rm_marker = "[KB_REMOVE_DRAFT]|"
                    last_draft = None
                    for msg in reversed(recent):
                        content = msg.get("content", "") or ""
                        if rm_marker in content:
                            last_draft = content[content.index(rm_marker) :]
                            break

                    if last_draft:
                        parts = last_draft.split("|", 2)
                        kb_hotel = DEFAULT_HOTEL_NAME
                        payload = {}
                        if len(parts) >= 3:
                            kb_hotel = parts[1] or DEFAULT_HOTEL_NAME
                            try:
                                payload = json.loads(parts[2])
                            except Exception as exc:
                                log.warning("[KB_REMOVE_RECOVERY] No se pudo parsear payload: %s", exc)
                        target_ids = payload.get("target_ids") if isinstance(payload, dict) else []
                        if target_ids:
                            result = await state.superintendente_agent.handle_kb_removal(
                                hotel_name=kb_hotel or DEFAULT_HOTEL_NAME,
                                target_ids=target_ids,
                                encargado_id=chat_id,
                                note=text,
                                criteria=payload.get("criteria") if isinstance(payload, dict) else "",
                            )
                            await state.channel_manager.send_message(
                                chat_id,
                                result.get("message") if isinstance(result, dict) else str(result),
                                channel="telegram",
                            )
                            return JSONResponse({"status": "kb_remove_recovered"})
                except Exception as exc:
                    log.warning("[KB_REMOVE_RECOVERY] Error: %s", exc, exc_info=True)

            # --------------------------------------------------------
            # 3️⃣ Respuesta a preguntas de KB pendientes (agregar/ajustar)
            # --------------------------------------------------------
            if chat_id in state.telegram_pending_kb_addition:
                pending_kb = state.telegram_pending_kb_addition[chat_id]

                if _looks_like_new_instruction(text_lower):
                    log.info("[KB_PENDING] Se detecta nueva instrucción, se libera flujo KB (%s)", chat_id)
                    state.telegram_pending_kb_addition.pop(chat_id, None)
                    await state.channel_manager.send_message(
                        chat_id,
                        "📌 Dejo guardado el borrador de KB. Sigo con tu nueva solicitud; "
                        "si quieres retomarlo más tarde, dime 'ok'.",
                        channel="telegram",
                    )
                else:

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
            # 3️⃣ bis - Recuperación de confirmación de KB perdida
            # --------------------------------------------------------
            if _is_short_confirmation(text_lower) and chat_id not in state.superintendente_pending_wa:
                try:
                    recent = state.memory_manager.get_memory(chat_id, limit=15) if state.memory_manager else []
                    kb_marker = "[KB_DRAFT]|"
                    last_draft = None
                    for msg in reversed(recent):
                        content = msg.get("content", "") or ""
                        if kb_marker in content:
                            last_draft = content[content.index(kb_marker) :]
                            break

                    if last_draft:
                        parts = last_draft.split("|", 4)
                        if len(parts) >= 5:
                            _, kb_hotel, topic, category, kb_content = parts[:5]
                        else:
                            kb_hotel = DEFAULT_HOTEL_NAME
                            topic = "Información"
                            category = "general"
                            kb_content = last_draft.replace(kb_marker, "").strip()

                        result = await state.superintendente_agent.handle_kb_addition(
                            topic=topic.strip(),
                            content=kb_content.strip(),
                            encargado_id=chat_id,
                            hotel_name=kb_hotel or DEFAULT_HOTEL_NAME,
                            source="superintendente_recovery",
                        )

                        state.telegram_pending_kb_addition.pop(chat_id, None)
                        await state.channel_manager.send_message(
                            chat_id,
                            result.get("message") if isinstance(result, dict) else str(result),
                            channel="telegram",
                        )
                        return JSONResponse({"status": "kb_recovered"})
                except Exception as exc:
                    log.warning("[KB_RECOVERY] No se pudo recuperar draft: %s", exc, exc_info=True)

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
                and chat_id not in state.telegram_pending_kb_removal
                and original_msg_id is None
            )

            # --------------------------------------------------------
            # 🔄 Evitar que solicitudes nuevas (KB/historial) queden atrapadas
            # en el flujo de confirmación de WhatsApp pendiente
            # --------------------------------------------------------
            bypass_wa_flow = False
            has_kb_pending = chat_id in state.telegram_pending_kb_addition or chat_id in state.telegram_pending_kb_removal
            if chat_id in state.superintendente_pending_wa:
                if text_lower.startswith("/super") or has_kb_pending or any(
                    kw in text_lower for kw in {"base de conoc", "kb", "historial", "convers", "broadcast"}
                ):
                    log.info("[WA_CONFIRM] Se descarta borrador WA por nueva instrucción (%s)", chat_id)
                    state.superintendente_pending_wa.pop(chat_id, None)
                    bypass_wa_flow = True

            # --------------------------------------------------------
            # 1️⃣ ter - Confirmación/ajuste de envío WhatsApp directo
            # --------------------------------------------------------
            if chat_id in state.superintendente_pending_wa and not bypass_wa_flow:
                pending = state.superintendente_pending_wa[chat_id]
                guest_id = pending.get("guest_id")
                draft_msg = pending.get("message", "")

                if _is_short_wa_confirmation(text_lower):
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

                if _is_short_wa_cancel(text_lower):
                    log.info("[WA_CONFIRM] Cancelado por %s", chat_id)
                    state.superintendente_pending_wa.pop(chat_id, None)
                    await state.channel_manager.send_message(
                        chat_id,
                        "Operación cancelada.",
                        channel="telegram",
                    )
                    return JSONResponse({"status": "wa_cancelled"})

                log.info("[WA_CONFIRM] Ajuste de borrador por %s", chat_id)
                llm = getattr(state.superintendente_agent, "llm", None)
                rewritten = await _rewrite_wa_draft(llm, draft_msg, text)
                rewritten = _clean_wa_payload(rewritten)
                state.superintendente_pending_wa[chat_id]["message"] = rewritten
                await state.channel_manager.send_message(
                    chat_id,
                    (
                        f"📝 Borrador WhatsApp para {guest_id}:\n"
                        f"{rewritten}\n\n"
                        "✏️ Escribe ajustes directamente si deseas modificarlo.\n"
                        "✅ Responde 'sí' para enviar.\n"
                        "❌ Responde 'no' para descartar."
                    ),
                    channel="telegram",
                )
                return JSONResponse({"status": "wa_updated"})

            # --------------------------------------------------------
            # 1️⃣ ter-bis - Confirmación WA recuperando de memoria
            # --------------------------------------------------------
            if _is_short_wa_confirmation(text_lower):
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
                            msg_to_send = _clean_wa_payload(msg_raw)
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

            # 🧠 Flujo dedicado: historial de conversaciones
            summary_words = {"resumen", "summary", "sintesis", "síntesis"}
            # Solo considera 'original' explícito como modo directo; 'historial' es solo intención
            raw_words = {"original", "completo", "raw", "crudo", "mensajes"}

            def _detect_mode(text_l: str) -> str | None:
                if any(w in text_l for w in summary_words):
                    return "resumen"
                if any(w in text_l for w in raw_words):
                    return "original"
                return None

            def _is_review_intent(text_l: str) -> bool:
                return any(term in text_l for term in {"historial", "convers", "mensajes", "chat"})

            if _is_review_intent(text_lower) and phone_hint:
                state.superintendente_pending_review[chat_id] = phone_hint
                mode_hint = _detect_mode(text_lower)
                if mode_hint:
                    history_text = await _render_history(phone_hint, mode_hint)
                    await state.channel_manager.send_message(chat_id, history_text, channel="telegram")
                    state.superintendente_pending_review.pop(chat_id, None)
                    return JSONResponse({"status": "history_served"})
                await state.channel_manager.send_message(
                    chat_id,
                    f"¿Prefieres 'resumen' o 'original' para el historial de {phone_hint}?",
                    channel="telegram",
                )
                return JSONResponse({"status": "history_mode_requested"})

            if chat_id in state.superintendente_pending_review:
                pending_guest = state.superintendente_pending_review[chat_id]
                mode = _detect_mode(text_lower)
                if mode:
                    history_text = await _render_history(pending_guest, mode)
                    await state.channel_manager.send_message(chat_id, history_text, channel="telegram")
                    state.superintendente_pending_review.pop(chat_id, None)
                    return JSONResponse({"status": "history_served"})

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
                            wa_intent = phone_hint or any(
                                term in text_lower
                                for term in {"dile", "enviale", "envíale", "mandale", "mándale", "manda", "enviar"}
                            )
                            if not wa_intent:
                                log.info("[WA_DRAFT] Ignorado por falta de intención explícita (%s)", chat_id)
                                response = response.replace(draft_payload, "").strip()
                            else:
                                msg_to_send = _clean_wa_payload(msg_raw)
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
                                    f"📝 Borrador WhatsApp para {guest_id}:\n"
                                    f"{msg_to_send}\n\n"
                                    "✏️ Escribe ajustes directamente si deseas modificarlo.\n"
                                    "✅ Responde 'sí' para enviar.\n"
                                    "❌ Responde 'no' para descartar."
                                )
                                await state.channel_manager.send_message(
                                    chat_id,
                                    preview,
                                    channel="telegram",
                                )
                                return JSONResponse({"status": "wa_draft"})

                    kb_remove_marker = "[KB_REMOVE_DRAFT]|"
                    if kb_remove_marker in response:
                        marker_line = next((ln for ln in response.splitlines() if kb_remove_marker in ln), "")
                        draft_payload = marker_line if marker_line else response[response.index(kb_remove_marker):]

                        parts = draft_payload.split("|", 2)
                        kb_hotel = hotel_name
                        payload = {}
                        if len(parts) >= 3:
                            kb_hotel = parts[1] or hotel_name
                            try:
                                payload = json.loads(parts[2])
                            except Exception as exc:
                                log.warning("[KB_REMOVE] No se pudo parsear payload: %s", exc)

                        target_ids = payload.get("target_ids") if isinstance(payload, dict) else []
                        total = int(payload.get("total_matches", 0) or len(target_ids or [])) if isinstance(payload, dict) else 0

                        pending_payload = {
                            "hotel_name": kb_hotel or hotel_name or DEFAULT_HOTEL_NAME,
                            "criteria": (payload.get("criteria") if isinstance(payload, dict) else "") or "",
                            "date_from": (payload.get("date_from") if isinstance(payload, dict) else "") or "",
                            "date_to": (payload.get("date_to") if isinstance(payload, dict) else "") or "",
                            "preview": (payload.get("preview") if isinstance(payload, dict) else []) or [],
                            "matches": (payload.get("matches") if isinstance(payload, dict) else []) or [],
                            "target_ids": target_ids or [],
                            "total_matches": total or len(target_ids or []),
                        }

                        if not pending_payload["target_ids"]:
                            await state.channel_manager.send_message(
                                chat_id,
                                "No encontré registros para eliminar con ese criterio.",
                                channel="telegram",
                            )
                            return JSONResponse({"status": "kb_remove_empty"})

                        # 🧹 Al iniciar un nuevo borrador de eliminación, limpia borradores de agregado pendientes
                        state.telegram_pending_kb_addition.pop(chat_id, None)
                        state.telegram_pending_kb_removal[chat_id] = pending_payload

                        try:
                            await state.memory_manager.save(
                                conversation_id=chat_id,
                                role="system",
                                content=draft_payload,
                            )
                        except Exception:
                            pass

                        preview_txt = _format_removal_preview(pending_payload)
                        await state.channel_manager.send_message(
                            chat_id,
                            preview_txt,
                            channel="telegram",
                        )
                        return JSONResponse({"status": "kb_remove_draft"})

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

                        # 🧹 Al iniciar un nuevo borrador de agregado, limpia borradores de eliminación pendientes
                        state.telegram_pending_kb_removal.pop(chat_id, None)

                        preview = build_kb_preview(topic, category, kb_content)
                        await state.channel_manager.send_message(
                            chat_id,
                            preview,
                            channel="telegram",
                        )
                        return JSONResponse({"status": "kb_draft"})

                    if (
                        "tema:" in response.lower()
                        and "contenido:" in response.lower()
                        and kb_marker not in response
                        and not any(word in response.lower() for word in {"elimina", "eliminar", "borra", "borrar", "quitar", "remueve"})
                    ):
                        topic, category, content_block = extract_kb_fields(response, hotel_name)

                        state.telegram_pending_kb_addition[chat_id] = {
                            "escalation_id": "",
                            "topic": topic,
                            "content": content_block,
                            "hotel_name": hotel_name,
                            "source": "superintendente_fallback",
                            "category": category,
                        }

                        state.telegram_pending_kb_removal.pop(chat_id, None)

                        preview = build_kb_preview(topic, category, content_block)
                        await state.channel_manager.send_message(
                            chat_id,
                            preview,
                            channel="telegram",
                        )
                        return JSONResponse({"status": "kb_draft_fallback"})

                    formatted = format_superintendente_message(response)
                    if not formatted.strip():
                        if "[WA_DRAFT]|" in (response or ""):
                            formatted = (
                                "📝 Borrador de WhatsApp listo. "
                                "Di 'sí' para enviarlo o escribe ajustes para modificar el mensaje."
                            )
                        else:
                            formatted = "No hay contenido para mostrar. ¿Quieres que reformule o muestre el borrador?"
                    await state.channel_manager.send_message(
                        chat_id,
                        formatted,
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
