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

from core.constants import ACTIVE_HOTEL_NAME, WA_CANCEL_WORDS, WA_CONFIRM_WORDS
from core.escalation_manager import get_escalation
from core.message_utils import (
    build_kb_preview,
    extract_clean_draft,
    extract_kb_fields,
    format_superintendente_message,
    get_escalation_metadata,
    looks_like_new_instruction,
    sanitize_wa_message,
)
from core.db import get_conversation_history
from core.instance_context import DEFAULT_PROPERTY_TABLE, ensure_instance_credentials, fetch_property_by_id

log = logging.getLogger("TelegramWebhook")
log.setLevel(logging.INFO)
AUTO_KB_PROMPT_ENABLED = os.getenv("AUTO_KB_PROMPT_ENABLED", "false").lower() == "true"


def register_telegram_routes(app, state):
    """Registra el endpoint de Telegram y comparte estado con el pipeline."""

    def _extract_phone(text: str) -> str | None:
        """Extrae el primer número estilo teléfono con 6-15 dígitos."""
        if not text:
            return None
        match = re.search(r"\+?\d{6,15}", text.replace(" ", ""))
        return match.group(0) if match else None

    def _extract_property_id(text: str) -> int | None:
        if not text:
            return None
        match = re.search(r"\b(?:property|propiedad)\s*(?:id\s*)?(\d+)\b", text, flags=re.IGNORECASE)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

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

    def _normalize_guest_id(guest_id: str | None) -> str:
        return str(guest_id or "").replace("+", "").strip()

    def _ensure_guest_language(msg: str, guest_id: str) -> str:
        """Mantiene el idioma del huésped al reenviar mensajes del superintendente."""
        return msg

    def _looks_like_new_instruction(text: str) -> bool:
        """
        Detecta si el encargado está cambiando de tema (ej. mandar mensaje, pedir historial)
        para no atrapar la solicitud dentro de un flujo previo.
        """
        return looks_like_new_instruction(text) or bool(_extract_phone(text))

    async def _collect_conversations(
        guest_id: str,
        limit: int = 10,
        property_id: int | None = None,
        channel: str | None = "whatsapp",
    ):
        """Recupera y deduplica mensajes del huésped desde DB + runtime."""
        if not state.memory_manager:
            return []

        clean_id = str(guest_id).replace("+", "").strip()

        db_msgs = await asyncio.to_thread(
            get_conversation_history,
            clean_id,
            limit * 3,  # pedir más por ruido/system
            None,
            property_id,
            "chat_history",
            channel,
        )

        def _parse_ts(ts: Any) -> float:
            try:
                if isinstance(ts, datetime):
                    return ts.timestamp()
                ts_str = str(ts).replace("Z", "")
                return datetime.fromisoformat(ts_str).timestamp()
            except Exception:
                return 0.0

        combined_sorted = sorted((db_msgs or []), key=lambda m: _parse_ts(m.get("created_at")))
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

    async def _render_history(
        guest_id: str,
        mode: str,
        limit: int = 10,
        property_id: int | None = None,
        channel: str | None = "whatsapp",
    ) -> str:
        """Devuelve texto listo para enviar (raw o resumen IA)."""
        convos = await _collect_conversations(
            guest_id,
            limit=limit,
            property_id=property_id,
            channel=channel,
        )
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
            prefix = {
                "user": "Hotel",
                "guest": "Huésped",
                "assistant": "BookAI",
                "bookai": "BookAI",
                "system": "BookAI",
                "tool": "BookAI",
            }.get(role, "BookAI")
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

    def _parse_wa_drafts(raw_text: str) -> list[dict]:
        """
        Extrae uno o varios borradores [WA_DRAFT]|guest|msg de una respuesta
        del superintendente. Devuelve lista de dicts {guest_id, message}.
        """
        if "[WA_DRAFT]|" not in (raw_text or ""):
            return []

        drafts: list[dict] = []
        parts = (raw_text or "").split("[WA_DRAFT]|")
        for chunk in parts[1:]:
            if not chunk:
                continue
            subparts = chunk.split("|", 1)
            if len(subparts) < 2:
                continue
            guest_id = subparts[0].strip()
            msg = subparts[1].strip()
            if not guest_id or not msg:
                continue
            msg_clean = _clean_wa_payload(msg)
            msg_clean = _ensure_guest_language(msg_clean, guest_id)
            drafts.append({"guest_id": guest_id, "message": msg_clean})
        return drafts

    def _format_wa_preview(drafts: list[dict]) -> str:
        """
        Construye el panel de confirmación para uno o varios borradores WA.
        """
        if not drafts:
            return ""

        if len(drafts) == 1:
            guest_id = drafts[0].get("guest_id")
            msg = drafts[0].get("message", "")
            return (
                f"📝 Borrador WhatsApp para {guest_id}:\n"
                f"{msg}\n\n"
                "✏️ Escribe ajustes directamente si deseas modificarlo.\n"
                "✅ Responde 'sí' para enviar.\n"
                "❌ Responde 'no' para descartar."
            )

        lines = ["📝 Borradores de WhatsApp preparados:"]
        for draft in drafts:
            guest_id = draft.get("guest_id", "")
            msg = draft.get("message", "")
            lines.append(f"• {guest_id}: {msg}")
        lines.append("")
        lines.append("✏️ Escribe ajustes para aplicar a todos.")
        lines.append("✅ Responde 'sí' para enviar todos.")
        lines.append("❌ Responde 'no' para descartar.")
        return "\n".join(lines)

    def _extract_wa_preview(raw_text: str) -> tuple[str | None, str | None]:
        """
        Extrae guest_id y mensaje desde respuestas tipo "Borrador preparado..." sin marcador [WA_DRAFT].
        """
        if not raw_text or "Borrador preparado para enviar por WhatsApp" not in raw_text:
            return None, None

        guest_id = _extract_phone(raw_text)
        lines = raw_text.splitlines()
        start_idx = None
        for idx, line in enumerate(lines):
            if "Borrador preparado para enviar por WhatsApp" in line:
                start_idx = idx
                break
        if start_idx is None:
            return guest_id, None

        msg_lines: list[str] = []
        for line in lines[start_idx + 1:]:
            stripped = line.strip()
            if not stripped and not msg_lines:
                continue
            if stripped.lower().startswith("¿quieres enviarlo") or stripped.lower().startswith("quieres enviarlo"):
                break
            msg_lines.append(line)

        message = "\n".join([ln.strip() for ln in msg_lines]).strip()
        return guest_id, message or None

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
            property_id_hint = _extract_property_id(text)
            if property_id_hint is not None and state.memory_manager:
                state.memory_manager.set_flag(chat_id, "property_id", property_id_hint)
                try:
                    property_table = (
                        state.memory_manager.get_flag(chat_id, "property_table") or DEFAULT_PROPERTY_TABLE
                    )
                    prop_payload = fetch_property_by_id(property_table, property_id_hint)
                    prop_name = prop_payload.get("name")
                    if prop_name:
                        state.memory_manager.set_flag(chat_id, "property_name", prop_name)
                except Exception:
                    pass

            # --------------------------------------------------------
            # 1️⃣ bis - Completar borrador de broadcast check-in
            # --------------------------------------------------------
            if chat_id in state.superintendente_pending_broadcast:
                pending = state.superintendente_pending_broadcast.get(chat_id) or {}
                missing_fields = pending.get("missing_fields") or []
                parameters: dict = {}
                parsed = None
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        parameters = parsed
                except Exception:
                    parsed = None

                if not parameters:
                    if len(missing_fields) == 1:
                        parameters[missing_fields[0]] = text.strip()
                    else:
                        parsed_text = text.replace("\n", " ").strip()
                        for field in missing_fields:
                            pattern = re.compile(
                                rf"{re.escape(field)}\s*[:=]\s*([^,;]+)",
                                flags=re.IGNORECASE,
                            )
                            match = pattern.search(parsed_text)
                            if match:
                                parameters[field] = match.group(1).strip()

                    if not parameters:
                        await state.channel_manager.send_message(
                            chat_id,
                            "Pásame los valores en formato `campo: valor`, uno por línea. "
                            "Ejemplo:\n"
                            "host_name: Alda Ponferrada\n"
                            "parking_info: Parking Ponferrada\n"
                            "reservation_url: http://hotel.ponferrada.es",
                            channel="telegram",
                        )
                        return JSONResponse({"status": "broadcast_missing_params"})

                try:
                    from tools.superintendente_tool import create_send_broadcast_checkin_tool

                    tool = create_send_broadcast_checkin_tool(
                        hotel_name=state.superintendente_chats.get(chat_id, {}).get("hotel_name", ACTIVE_HOTEL_NAME),
                        channel_manager=state.channel_manager,
                        supabase_client=state.supabase_client,
                        template_registry=state.template_registry,
                        memory_manager=state.memory_manager,
                        chat_id=chat_id,
                    )
                    payload = {
                        "template_id": pending.get("template_id"),
                        "date": pending.get("date"),
                        "parameters": parameters,
                        "language": pending.get("language") or "es",
                        "instance_id": pending.get("instance_id"),
                        "property_id": pending.get("property_id"),
                    }
                    result = await tool.ainvoke(payload)
                    state.superintendente_pending_broadcast.pop(chat_id, None)
                    await state.channel_manager.send_message(chat_id, str(result), channel="telegram")
                    return JSONResponse({"status": "broadcast_sent"})
                except Exception as exc:
                    await state.channel_manager.send_message(
                        chat_id,
                        f"❌ No pude enviar el broadcast: {exc}",
                        channel="telegram",
                    )
                    return JSONResponse({"status": "broadcast_error"})

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
                            hotel_name=ACTIVE_HOTEL_NAME,
                            superintendente_agent=state.superintendente_agent,
                        )

                        state.telegram_pending_kb_addition[chat_id] = {
                            "escalation_id": escalation_id,
                            "topic": topic,
                            "content": manager_reply,
                            "hotel_name": ACTIVE_HOTEL_NAME,
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
                        hotel_name=pending_rm.get("hotel_name", ACTIVE_HOTEL_NAME),
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
                and chat_id not in state.superintendente_pending_tpl
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
                        kb_hotel = ACTIVE_HOTEL_NAME
                        payload = {}
                        if len(parts) >= 3:
                            kb_hotel = parts[1] or ACTIVE_HOTEL_NAME
                            try:
                                payload = json.loads(parts[2])
                            except Exception as exc:
                                log.warning("[KB_REMOVE_RECOVERY] No se pudo parsear payload: %s", exc)
                        target_ids = payload.get("target_ids") if isinstance(payload, dict) else []
                        if target_ids:
                            result = await state.superintendente_agent.handle_kb_removal(
                                hotel_name=kb_hotel or ACTIVE_HOTEL_NAME,
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
                        hotel_name=pending_kb.get("hotel_name", ACTIVE_HOTEL_NAME),
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
            if (
                _is_short_confirmation(text_lower)
                and chat_id not in state.superintendente_pending_wa
                and chat_id not in state.superintendente_pending_tpl
            ):
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
                            kb_hotel = ACTIVE_HOTEL_NAME
                            topic = "Información"
                            category = "general"
                            kb_content = last_draft.replace(kb_marker, "").strip()

                        result = await state.superintendente_agent.handle_kb_addition(
                            topic=topic.strip(),
                            content=kb_content.strip(),
                            encargado_id=chat_id,
                            hotel_name=kb_hotel or ACTIVE_HOTEL_NAME,
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
                and original_msg_id is None
            )

            # --------------------------------------------------------
            # 🔄 Evitar que solicitudes nuevas (KB/historial) queden atrapadas
            # en el flujo de confirmación de WhatsApp pendiente
            # --------------------------------------------------------
            bypass_wa_flow = False
            bypass_tpl_flow = False
            has_kb_pending = chat_id in state.telegram_pending_kb_addition or chat_id in state.telegram_pending_kb_removal
            if chat_id in state.superintendente_pending_wa:
                if text_lower.startswith("/super") or has_kb_pending or any(
                    kw in text_lower for kw in {"base de conoc", "kb", "historial", "convers", "broadcast"}
                ):
                    log.info("[WA_CONFIRM] Se descarta borrador WA por nueva instrucción (%s)", chat_id)
                    state.superintendente_pending_wa.pop(chat_id, None)
                    bypass_wa_flow = True
            if chat_id in state.superintendente_pending_tpl:
                if text_lower.startswith("/super") or _looks_like_new_instruction(text_lower):
                    log.info("[TPL_CONFIRM] Se descarta borrador de plantilla por nueva instrucción (%s)", chat_id)
                    state.superintendente_pending_tpl.pop(chat_id, None)
                    bypass_tpl_flow = True

            # --------------------------------------------------------
            # 1️⃣ ter - Confirmación de envío de plantillas (Superintendente)
            # --------------------------------------------------------
            if chat_id in state.superintendente_pending_tpl and not bypass_tpl_flow:
                pending_tpl = state.superintendente_pending_tpl[chat_id]
                guest_ids = pending_tpl.get("guest_ids") or []
                display_ids = pending_tpl.get("display_guest_ids") or guest_ids
                template_id = pending_tpl.get("template")
                language_code = pending_tpl.get("language") or "es"
                parameters = pending_tpl.get("parameters")

                if _is_short_wa_confirmation(text_lower):
                    sent = 0
                    errors = 0
                    for gid in guest_ids:
                        try:
                            await state.channel_manager.send_template_message(
                                gid,
                                template_id,
                                parameters=parameters,
                                language=language_code,
                                channel="whatsapp",
                                context_id=chat_id,
                            )
                            try:
                                if state.memory_manager:
                                    payload_preview = parameters if isinstance(parameters, (dict, list)) else str(parameters or "")
                                    state.memory_manager.save(
                                        gid,
                                        "assistant",
                                        f"[TPL_SENT]|{template_id}|{payload_preview}",
                                        channel="whatsapp",
                                    )
                            except Exception as mem_exc:
                                log.warning("[TPL_CONFIRM] No se pudo guardar plantilla en memoria (%s): %s", gid, mem_exc)
                            sent += 1
                        except Exception as exc:
                            errors += 1
                            log.warning("[TPL_CONFIRM] Error enviando a %s: %s", gid, exc, exc_info=True)

                    state.superintendente_pending_tpl.pop(chat_id, None)
                    dest_label = ", ".join(display_ids)
                    await state.channel_manager.send_message(
                        chat_id,
                        f"✅ Plantilla {template_id} enviada a {sent}/{len(guest_ids)} huésped(es): {dest_label}",
                        channel="telegram",
                    )
                    return JSONResponse({"status": "tpl_sent"})

                if _is_short_wa_cancel(text_lower):
                    state.superintendente_pending_tpl.pop(chat_id, None)
                    await state.channel_manager.send_message(
                        chat_id,
                        "Operación cancelada.",
                        channel="telegram",
                    )
                    return JSONResponse({"status": "tpl_cancelled"})

            # --------------------------------------------------------
            # 1️⃣ ter - Confirmación/ajuste de envío WhatsApp directo
            # --------------------------------------------------------
            if chat_id in state.superintendente_pending_wa and not bypass_wa_flow:
                pending_raw = state.superintendente_pending_wa[chat_id]
                drafts: list[dict] = []
                if isinstance(pending_raw, list):
                    drafts = [d for d in pending_raw if isinstance(d, dict) and d.get("guest_id")]
                elif isinstance(pending_raw, dict) and pending_raw.get("drafts"):
                    drafts = [d for d in pending_raw.get("drafts", []) if isinstance(d, dict) and d.get("guest_id")]
                elif isinstance(pending_raw, dict) and pending_raw.get("guest_id"):
                    drafts = [pending_raw]

                if not drafts:
                    state.superintendente_pending_wa.pop(chat_id, None)
                    log.info("[WA_CONFIRM] Borrador vacío, se limpia estado (%s)", chat_id)
                    return JSONResponse({"status": "wa_missing_draft"})

                guest_id = drafts[0].get("guest_id")
                draft_msg = drafts[0].get("message", "")

                if _is_short_wa_confirmation(text_lower):
                    log.info("[WA_CONFIRM] Enviando %s borrador(es) desde %s", len(drafts), chat_id)
                    if state.memory_manager:
                        first = drafts[0] if drafts else {}
                        ctx_prop = first.get("property_id")
                        ctx_instance_id = first.get("instance_id")
                        if ctx_prop is not None:
                            state.memory_manager.set_flag(chat_id, "property_id", ctx_prop)
                        if ctx_instance_id:
                            state.memory_manager.set_flag(chat_id, "instance_id", ctx_instance_id)
                            state.memory_manager.set_flag(chat_id, "instance_hotel_code", ctx_instance_id)
                        ensure_instance_credentials(state.memory_manager, chat_id)
                    sent = 0
                    for draft in drafts:
                        gid = draft.get("guest_id")
                        msg_raw = draft.get("message", "")
                        if state.memory_manager and gid:
                            ctx_prop = draft.get("property_id")
                            ctx_instance_id = draft.get("instance_id")
                            if ctx_prop is not None:
                                state.memory_manager.set_flag(gid, "property_id", ctx_prop)
                            if ctx_instance_id:
                                state.memory_manager.set_flag(gid, "instance_id", ctx_instance_id)
                                state.memory_manager.set_flag(gid, "instance_hotel_code", ctx_instance_id)
                        final_msg = _ensure_guest_language(msg_raw, gid)
                        await state.channel_manager.send_message(
                            gid,
                            final_msg,
                            channel="whatsapp",
                            context_id=chat_id,
                        )
                        try:
                            if state.memory_manager:
                                state.memory_manager.save(gid, "assistant", final_msg, channel="whatsapp")
                        except Exception as mem_exc:
                            log.warning("[WA_CONFIRM] No se pudo guardar memoria para %s: %s", gid, mem_exc)
                        sent += 1
                    state.superintendente_pending_wa.pop(chat_id, None)
                    guest_list = ", ".join([d.get("guest_id", "") for d in drafts])
                    await state.channel_manager.send_message(
                        chat_id,
                        f"✅ Enviado a {sent}/{len(drafts)} huésped(es): {guest_list}",
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
                rewritten = _ensure_guest_language(rewritten, guest_id)
                updated = []
                for draft in drafts:
                    updated.append({"guest_id": draft.get("guest_id"), "message": rewritten})
                state.superintendente_pending_wa[chat_id] = {"drafts": updated}
                await state.channel_manager.send_message(
                    chat_id,
                    _format_wa_preview(updated),
                    channel="telegram",
                )
                return JSONResponse({"status": "wa_updated"})

            # --------------------------------------------------------
            # 1️⃣ ter-bis - Confirmación WA recuperando de memoria
            # --------------------------------------------------------
            if _is_short_wa_confirmation(text_lower):
                try:
                    recent = state.memory_manager.get_memory(chat_id, limit=10)
                    marker_bulk = "[WA_BULK]|"
                    marker = "[WA_DRAFT]|"
                    drafts = []
                    for msg in reversed(recent):
                        content = msg.get("content", "") or ""
                        if marker_bulk in content:
                            raw_json = content.split(marker_bulk, 1)[1].strip()
                            try:
                                parsed = json.loads(raw_json)
                                drafts = [
                                    {"guest_id": d.get("guest_id"), "message": d.get("message", "")}
                                    for d in parsed
                                    if isinstance(d, dict) and d.get("guest_id")
                                ]
                            except Exception as exc:
                                log.warning("[WA_CONFIRM_RECOVERY] No pude parsear WA_BULK: %s", exc)
                            break
                        if marker in content:
                            last_draft = content[content.index(marker):]
                            parts = last_draft.split("|", 2)
                            if len(parts) == 3:
                                drafts = [{"guest_id": parts[1], "message": parts[2]}]
                                break

                    if drafts:
                        if state.memory_manager:
                            first = drafts[0] if drafts else {}
                            ctx_prop = first.get("property_id")
                            ctx_instance_id = first.get("instance_id")
                            if ctx_prop is not None:
                                state.memory_manager.set_flag(chat_id, "property_id", ctx_prop)
                            if ctx_instance_id:
                                state.memory_manager.set_flag(chat_id, "instance_id", ctx_instance_id)
                                state.memory_manager.set_flag(chat_id, "instance_hotel_code", ctx_instance_id)
                            ensure_instance_credentials(state.memory_manager, chat_id)
                        sent = 0
                        for draft in drafts:
                            guest_id = draft.get("guest_id")
                            msg_raw = draft.get("message", "")
                            if state.memory_manager and guest_id:
                                ctx_prop = draft.get("property_id")
                                ctx_instance_id = draft.get("instance_id")
                                if ctx_prop is not None:
                                    state.memory_manager.set_flag(guest_id, "property_id", ctx_prop)
                                if ctx_instance_id:
                                    state.memory_manager.set_flag(guest_id, "instance_id", ctx_instance_id)
                                    state.memory_manager.set_flag(guest_id, "instance_hotel_code", ctx_instance_id)
                            msg_to_send = _clean_wa_payload(msg_raw)
                            msg_to_send = _ensure_guest_language(msg_to_send, guest_id)
                            await state.channel_manager.send_message(
                                guest_id,
                                msg_to_send,
                                channel="whatsapp",
                                context_id=chat_id,
                            )
                            try:
                                if state.memory_manager:
                                    state.memory_manager.save(guest_id, "assistant", msg_to_send, channel="whatsapp")
                            except Exception as mem_exc:
                                log.warning("[WA_CONFIRM_RECOVERY] No se pudo guardar memoria para %s: %s", guest_id, mem_exc)
                            if state.memory_manager:
                                state.memory_manager.save(
                                    chat_id,
                                    "system",
                                    f"[WA_SENT]|{guest_id}|{msg_to_send}",
                                    channel="telegram",
                                )
                            sent += 1

                        guest_list = ", ".join([d.get("guest_id", "") for d in drafts])
                        await state.channel_manager.send_message(
                            chat_id,
                            f"✅ Mensaje enviado a {sent}/{len(drafts)} huésped(es): {guest_list}",
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
                property_id_ctx = None
                if state.memory_manager:
                    property_id_ctx = state.memory_manager.get_flag(chat_id, "property_id")
                state.superintendente_pending_review[chat_id] = {
                    "guest_id": phone_hint,
                    "property_id": property_id_ctx,
                }
                mode_hint = _detect_mode(text_lower)
                if mode_hint:
                    history_text = await _render_history(phone_hint, mode_hint, property_id=property_id_ctx)
                    await state.channel_manager.send_message(chat_id, history_text, channel="telegram")
                    state.superintendente_pending_review.pop(chat_id, None)
                    if state.memory_manager:
                        state.memory_manager.set_flag(chat_id, "last_review_guest_id", phone_hint)
                        if property_id_ctx is not None:
                            state.memory_manager.set_flag(chat_id, "last_review_property_id", property_id_ctx)
                    return JSONResponse({"status": "history_served"})
                await state.channel_manager.send_message(
                    chat_id,
                    f"¿Prefieres 'resumen' o 'original' para el historial de {phone_hint}?",
                    channel="telegram",
                )
                return JSONResponse({"status": "history_mode_requested"})

            if chat_id in state.superintendente_pending_review:
                pending_payload = state.superintendente_pending_review[chat_id]
                if isinstance(pending_payload, dict):
                    pending_guest = pending_payload.get("guest_id")
                    pending_property_id = pending_payload.get("property_id")
                else:
                    pending_guest = pending_payload
                    pending_property_id = None
                mode = _detect_mode(text_lower)
                if mode and pending_guest:
                    property_id_ctx = pending_property_id
                    if state.memory_manager and property_id_ctx is None:
                        property_id_ctx = state.memory_manager.get_flag(chat_id, "property_id")
                    history_text = await _render_history(pending_guest, mode, property_id=property_id_ctx)
                    await state.channel_manager.send_message(chat_id, history_text, channel="telegram")
                    state.superintendente_pending_review.pop(chat_id, None)
                    if state.memory_manager:
                        state.memory_manager.set_flag(chat_id, "last_review_guest_id", pending_guest)
                        if property_id_ctx is not None:
                            state.memory_manager.set_flag(chat_id, "last_review_property_id", property_id_ctx)
                    return JSONResponse({"status": "history_served"})

            # Resumen/original sin guest_id: reusar el ultimo historial solicitado
            mode = _detect_mode(text_lower)
            if mode and not phone_hint and state.memory_manager:
                last_guest = state.memory_manager.get_flag(chat_id, "last_review_guest_id")
                if last_guest:
                    last_property = state.memory_manager.get_flag(chat_id, "last_review_property_id")
                    history_text = await _render_history(
                        last_guest,
                        mode,
                        property_id=last_property,
                    )
                    await state.channel_manager.send_message(chat_id, history_text, channel="telegram")
                    return JSONResponse({"status": "history_served"})

            # ✅ Confirmaciones rápidas de borradores de plantilla
            normalized_reply = text.strip().lower()
            tpl_pending = state.superintendente_pending_tpl.get(chat_id)
            # Recupera último borrador desde la memoria si se reinició el proceso.
            if not tpl_pending and normalized_reply in {"sí", "si", "no"}:
                try:
                    recent = state.memory_manager.get_memory(chat_id, limit=10)
                    marker = "[TPL_DRAFT]|"
                    last_draft = None
                    for msg in reversed(recent):
                        content = msg.get("content", "")
                        if marker in content:
                            line = next((ln for ln in content.splitlines() if marker in ln), "")
                            raw_payload = line[len(marker):] if line else content.split(marker, 1)[1]
                            raw_payload = raw_payload.split("\n", 1)[0].strip()
                            try:
                                tpl_pending = json.loads(raw_payload)
                            except Exception:
                                tpl_pending = None
                            break
                except Exception as exc:
                    log.warning("[TPL_RECOVER] No se pudo recuperar borrador: %s", exc)

            if tpl_pending and normalized_reply in {"sí", "si", "no"}:
                if normalized_reply == "no":
                    state.superintendente_pending_tpl.pop(chat_id, None)
                    await state.channel_manager.send_message(
                        chat_id,
                        "❌ Envío cancelado. Si necesitas otro borrador, indícame la plantilla y los datos.",
                        channel="telegram",
                    )
                    return JSONResponse({"status": "tpl_cancelled"})

                try:
                    template = tpl_pending.get("template")
                    guest_ids = tpl_pending.get("guest_ids") or []
                    parameters = tpl_pending.get("parameters") or []
                    language = tpl_pending.get("language") or "es"

                    for gid in guest_ids:
                        await state.channel_manager.send_template_message(
                            gid,
                            template,
                            parameters=parameters,
                            language=language,
                            channel="whatsapp",
                        )
                        try:
                            if state.memory_manager:
                                payload_preview = parameters if isinstance(parameters, (dict, list)) else str(parameters or "")
                                state.memory_manager.save(
                                    gid,
                                    "assistant",
                                    f"[TPL_SENT]|{template}|{payload_preview}",
                                    channel="whatsapp",
                                )
                        except Exception as mem_exc:
                            log.warning("[TPL_SEND] No se pudo guardar plantilla en memoria (%s): %s", gid, mem_exc)

                    state.superintendente_pending_tpl.pop(chat_id, None)
                    await state.channel_manager.send_message(
                        chat_id,
                        f"✅ Plantilla '{template}' enviada a {', '.join(guest_ids)}.",
                        channel="telegram",
                    )
                    return JSONResponse({"status": "tpl_sent"})
                except Exception as exc:
                    log.error("[TPL_SEND] Error enviando plantilla: %s", exc, exc_info=True)
                    await state.channel_manager.send_message(
                        chat_id,
                        f"❌ No se pudo enviar la plantilla: {exc}",
                        channel="telegram",
                    )
                    return JSONResponse({"status": "tpl_error"}, status_code=500)

            if super_mode or in_super_session:
                payload = text.split(" ", 1)[1].strip() if " " in text else ""
                hotel_name = state.superintendente_chats.get(chat_id, {}).get("hotel_name", ACTIVE_HOTEL_NAME)
                if payload:
                    prop_id = _extract_property_id(payload)
                    if prop_id is not None and state.memory_manager:
                        try:
                            state.memory_manager.set_flag(chat_id, "property_id", prop_id)
                        except Exception:
                            pass
                    payload_lower = payload.lower()
                    if "alda" in payload_lower and ("hotel" in payload_lower or "hostal" in payload_lower):
                        match = re.search(
                            r"(hotel|hostal)\s+alda[^,.\\n]*",
                            payload,
                            flags=re.IGNORECASE,
                        )
                        hotel_name = match.group(0).strip() if match else payload.strip()
                        state.superintendente_chats[chat_id] = {"hotel_name": hotel_name}
                        if state.memory_manager:
                            try:
                                state.memory_manager.set_flag(chat_id, "property_name", hotel_name)
                            except Exception:
                                pass
                else:
                    state.superintendente_chats[chat_id] = {"hotel_name": hotel_name}

                try:
                    response = await state.superintendente_agent.ainvoke(
                        user_input=payload or "Hola, ¿en qué puedo ayudarte?",
                        encargado_id=chat_id,
                        hotel_name=hotel_name,
                    )

                    wa_drafts = _parse_wa_drafts(response)
                    if not wa_drafts:
                        fallback_guest, fallback_msg = _extract_wa_preview(response)
                        if fallback_guest and fallback_msg:
                            fallback_msg = _ensure_guest_language(fallback_msg, fallback_guest)
                            wa_drafts = [{"guest_id": fallback_guest, "message": fallback_msg}]
                    if wa_drafts:
                        wa_intent = phone_hint or any(
                            term in text_lower for term in {"dile", "enviale", "envíale", "mandale", "mándale", "manda", "enviar"}
                        )
                        if not wa_intent:
                            log.info("[WA_DRAFT] Ignorado por falta de intención explícita (%s)", chat_id)
                            response = re.sub(r"\[WA_DRAFT\]\|.*", "", response, flags=re.S).strip()
                        else:
                            if state.memory_manager:
                                try:
                                    ctx_property_id = state.memory_manager.get_flag(chat_id, "property_id")
                                    ctx_instance_id = (
                                        state.memory_manager.get_flag(chat_id, "instance_id")
                                        or state.memory_manager.get_flag(chat_id, "instance_hotel_code")
                                    )
                                    for draft in wa_drafts:
                                        if ctx_property_id is not None:
                                            draft["property_id"] = ctx_property_id
                                        if ctx_instance_id:
                                            draft["instance_id"] = ctx_instance_id
                                        guest_id = draft.get("guest_id")
                                        if guest_id:
                                            if ctx_property_id is not None:
                                                state.memory_manager.set_flag(guest_id, "property_id", ctx_property_id)
                                            if ctx_instance_id:
                                                state.memory_manager.set_flag(guest_id, "instance_id", ctx_instance_id)
                                                state.memory_manager.set_flag(guest_id, "instance_hotel_code", ctx_instance_id)
                                except Exception:
                                    pass
                            pending_payload: Any = {"drafts": wa_drafts} if len(wa_drafts) > 1 else wa_drafts[0]
                            state.superintendente_pending_wa[chat_id] = pending_payload
                            log.info("[WA_DRAFT] Registrado %s borrador(es) desde %s", len(wa_drafts), chat_id)
                            try:
                                if state.memory_manager:
                                    if len(wa_drafts) > 1:
                                        state.memory_manager.save(
                                            conversation_id=chat_id,
                                            role="system",
                                            content=f"[WA_BULK]|{json.dumps(wa_drafts, ensure_ascii=False)}",
                                            channel="telegram",
                                        )
                                    else:
                                        draft = wa_drafts[0]
                                        state.memory_manager.save(
                                            conversation_id=chat_id,
                                            role="system",
                                            content=f"[WA_DRAFT]|{draft.get('guest_id')}|{draft.get('message')}",
                                            channel="telegram",
                                        )
                            except Exception as exc:
                                log.warning("[WA_DRAFT] No se pudo guardar borrador en memoria: %s", exc)

                            preview = _format_wa_preview(wa_drafts)
                            await state.channel_manager.send_message(
                                chat_id,
                                preview,
                                channel="telegram",
                            )
                            return JSONResponse({"status": "wa_draft"})

                    broadcast_marker = "[BROADCAST_DRAFT]|"
                    if broadcast_marker in response:
                        marker_line = next((ln for ln in response.splitlines() if broadcast_marker in ln), "")
                        raw_payload = marker_line[len(broadcast_marker):] if marker_line else response.split(broadcast_marker, 1)[1]
                        raw_payload = raw_payload.split("\n", 1)[0].strip()
                        try:
                            bc_payload = json.loads(raw_payload)
                        except Exception as exc:
                            log.error("[BROADCAST_DRAFT] No se pudo parsear el payload: %s", exc, exc_info=True)
                            bc_payload = None

                        if bc_payload:
                            state.superintendente_pending_broadcast[chat_id] = bc_payload
                            preview = response.replace(marker_line, "").replace(f"{broadcast_marker}{raw_payload}", "").strip()
                            await state.channel_manager.send_message(
                                chat_id,
                                format_superintendente_message(preview or "Indica los parámetros faltantes para continuar."),
                                channel="telegram",
                            )
                            return JSONResponse({"status": "broadcast_draft"})
                        else:
                            response = response.replace(marker_line, "").replace(f"{broadcast_marker}{raw_payload}", "").strip()

                    tpl_marker = "[TPL_DRAFT]|"
                    if tpl_marker in response:
                        marker_line = next((ln for ln in response.splitlines() if tpl_marker in ln), "")
                        raw_payload = marker_line[len(tpl_marker):] if marker_line else response.split(tpl_marker, 1)[1]
                        raw_payload = raw_payload.split("\n", 1)[0].strip()
                        try:
                            tpl_payload = json.loads(raw_payload)
                        except Exception as exc:
                            log.error("[TPL_DRAFT] No se pudo parsear el payload: %s", exc, exc_info=True)
                            tpl_payload = None

                        if tpl_payload:
                            state.superintendente_pending_tpl[chat_id] = tpl_payload
                            # Al iniciar un borrador de plantilla, limpia pendientes de KB para evitar colisiones de confirmación.
                            state.telegram_pending_kb_addition.pop(chat_id, None)
                            state.telegram_pending_kb_removal.pop(chat_id, None)
                            # Guarda el marcador en memoria para poder recuperar tras reinicios.
                            try:
                                if state.memory_manager:
                                    state.memory_manager.save(
                                        conversation_id=chat_id,
                                        role="system",
                                        content=f"{tpl_marker}{raw_payload}",
                                        channel="telegram",
                                    )
                            except Exception as exc:
                                log.warning("[TPL_DRAFT] No se pudo guardar en memoria: %s", exc)
                            preview = response.replace(marker_line, "").replace(f"{tpl_marker}{raw_payload}", "").strip()
                            if not preview:
                                guests = tpl_payload.get("display_guest_ids") or tpl_payload.get("guest_ids") or []
                                guest_label = ", ".join(guests)
                                preview = (
                                    f"📝 Borrador preparado para {guest_label} "
                                    f"(plantilla {tpl_payload.get('template')}).\n"
                                    "✅ Responde 'sí' para enviar o 'no' para cancelar."
                                )

                            await state.channel_manager.send_message(
                                chat_id,
                                format_superintendente_message(preview),
                                channel="telegram",
                            )
                            return JSONResponse({"status": "tpl_draft"})
                        else:
                            response = response.replace(marker_line, "").replace(f"{tpl_marker}{raw_payload}", "").strip()

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
                            "hotel_name": kb_hotel or hotel_name or ACTIVE_HOTEL_NAME,
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
                            if state.memory_manager:
                                state.memory_manager.save(
                                    conversation_id=chat_id,
                                    role="system",
                                    content=draft_payload,
                                    channel="telegram",
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
                            "hotel_name": kb_hotel or hotel_name or ACTIVE_HOTEL_NAME,
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
                        elif "[TPL_DRAFT]|" in (response or ""):
                            formatted = (
                                "📝 Borrador de plantilla listo. "
                                "Responde 'sí' para enviar o 'no' para cancelar."
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
