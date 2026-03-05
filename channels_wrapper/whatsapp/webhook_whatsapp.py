"""Handlers del webhook de WhatsApp (Meta)."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

import requests
from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse

from channels_wrapper.utils.text_utils import send_fragmented_async
from core.db import (
    find_wa_outbox_message,
    is_chat_visible_in_list,
    mark_wa_outbox_message_visible,
    supabase,
    update_wa_outbox_message,
)
from core.offer_semantics import sync_guest_offer_state_from_sent_wa
from core.pipeline import process_user_message, _resolve_bookai_enabled

log = logging.getLogger("WhatsAppWebhook")


def _chat_room_aliases(*values: str) -> list[str]:
    aliases: list[str] = []
    seen: set[str] = set()
    for raw_value in values:
        raw = str(raw_value or "").strip()
        if not raw:
            continue
        candidates = [raw]
        if ":" in raw:
            tail = raw.split(":")[-1].strip()
            if tail:
                candidates.append(tail)
                tail_clean = re.sub(r"\D", "", tail).strip()
                if tail_clean:
                    candidates.append(tail_clean)
        clean = re.sub(r"\D", "", raw).strip()
        if clean:
            candidates.append(clean)
        for candidate in candidates:
            c = str(candidate or "").strip()
            if not c or c in seen:
                continue
            seen.add(c)
            aliases.append(c)
    return aliases


def _mark_as_read(message_id: str, phone_id: str | None = None, token: str | None = None):
    """Envía el status 'read' para reflejar doble check azul en el cliente."""
    phone_id = phone_id or os.getenv("WHATSAPP_PHONE_ID")
    token = token or os.getenv("WHATSAPP_TOKEN")
    if not (phone_id and token and message_id):
        log.debug("No se pudo marcar como leído: faltan credenciales o message_id")
        return

    url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        if resp.status_code >= 400:
            log.warning(
                "⚠️ No se pudo marcar como leído (%s): %s",
                resp.status_code,
                resp.text,
            )
        else:
            log.info("✅ Read receipt enviado (%s)", message_id)
    except Exception as exc:
        log.debug("No se pudo enviar read receipt: %s", exc)


def _resolve_property_id_fallback(memory_id: str, sender: str) -> str | int | None:
    """Intenta recuperar property_id desde historial/reservas cuando llega nulo en webhook."""
    try:
        from core.db import supabase
        from core.config import Settings
    except Exception:
        return None

    raw_memory = str(memory_id or "").strip()
    raw_sender = str(sender or "").strip()
    clean_sender = re.sub(r"\D", "", raw_sender).strip()

    # 1) Preferir contexto compuesto exacto (original_chat_id = instancia:telefono)
    if raw_memory:
        try:
            rows = (
                supabase.table("chat_history")
                .select("property_id, created_at")
                .eq("channel", "whatsapp")
                .eq("original_chat_id", raw_memory)
                .order("created_at", desc=True)
                .limit(50)
                .execute()
                .data
                or []
            )
            for row in rows:
                prop = row.get("property_id")
                if prop is None:
                    prop = row.get("id")
                if prop is not None:
                    return prop
        except Exception:
            pass

    # 2) Fallback por conversation_id (sender limpio o memory_id)
    for cid in [raw_memory, raw_sender, clean_sender]:
        if not cid:
            continue
        try:
            rows = (
                supabase.table("chat_history")
                .select("property_id, created_at")
                .eq("channel", "whatsapp")
                .eq("conversation_id", cid)
                .order("created_at", desc=True)
                .limit(50)
                .execute()
                .data
                or []
            )
            for row in rows:
                prop = row.get("property_id")
                if prop is None:
                    prop = row.get("id")
                if prop is not None:
                    return prop
        except Exception:
            continue

    # 3) Último recurso: chat_reservations por teléfono limpio.
    if clean_sender:
        try:
            rows = (
                supabase.table(Settings.CHAT_RESERVATIONS_TABLE)
                .select("property_id")
                .eq("chat_id", clean_sender)
                .limit(50)
                .execute()
                .data
                or []
            )
            for row in rows:
                prop = row.get("property_id")
                if prop is None:
                    prop = row.get("id")
                if prop is not None:
                    return prop
        except Exception:
            pass

    return None


def _iter_status_events(payload: dict) -> list[dict]:
    events: list[dict] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            metadata = value.get("metadata", {}) or {}
            for raw_status in value.get("statuses", []) or []:
                if not isinstance(raw_status, dict):
                    continue
                event = dict(raw_status)
                event["_metadata"] = metadata
                event["_value"] = value
                events.append(event)
    return events


def _extract_status_error(status_event: dict) -> tuple[str | None, str | None]:
    errors = status_event.get("errors") or []
    if not isinstance(errors, list) or not errors:
        return None, None
    first_error = errors[0] if isinstance(errors[0], dict) else {}
    code = first_error.get("code")
    error_data = first_error.get("error_data") if isinstance(first_error.get("error_data"), dict) else {}
    details = (
        error_data.get("details")
        or first_error.get("message")
        or first_error.get("title")
        or status_event.get("status")
    )
    code_text = str(code).strip() if code is not None else None
    details_text = str(details).strip() if details else None
    return code_text or None, details_text or None


def _is_no_whatsapp_account_error(*, error_code: str | None, error_details: str | None) -> bool:
    if str(error_code or "").strip() == "131026":
        return True
    normalized = str(error_details or "").strip().lower()
    if not normalized:
        return False
    return ("not on whatsapp" in normalized) or ("no esta en whatsapp" in normalized)


def _outbox_payload(outbox_row: dict) -> dict:
    payload = outbox_row.get("payload")
    return payload if isinstance(payload, dict) else {}


def _parse_property_id(value: str | int | None) -> str | int | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.isdigit():
        try:
            return int(raw)
        except Exception:
            return raw
    return raw


def _status_rooms(outbox_row: dict) -> list[str]:
    payload = _outbox_payload(outbox_row)
    context_id = str(payload.get("context_id") or outbox_row.get("context_id") or "").strip()
    chat_id = str(payload.get("chat_id") or outbox_row.get("chat_id") or "").strip()
    recipient_id = str(outbox_row.get("recipient_id") or "").strip()
    property_id = payload.get("property_id")
    if property_id is None:
        property_id = outbox_row.get("property_id")

    rooms = [f"chat:{alias}" for alias in _chat_room_aliases(context_id, chat_id, recipient_id)]
    if property_id is not None and str(property_id).strip():
        rooms.append(f"property:{property_id}")
    rooms.append("channel:whatsapp")

    unique_rooms: list[str] = []
    seen: set[str] = set()
    for room in rooms:
        candidate = str(room or "").strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        unique_rooms.append(candidate)
    return unique_rooms


def _restore_chat_visibility(
    chat_id: str,
    *,
    property_id: str | int | None,
    channel: str = "whatsapp",
    original_chat_id: str | None = None,
) -> bool:
    clean_id = str(chat_id or "").replace("+", "").strip()
    if not clean_id or property_id is None:
        return False

    restore_payload = {"archived_at": None, "hidden_at": None}
    current_channel = str(channel or "whatsapp").strip() or "whatsapp"
    original_clean = str(original_chat_id or "").replace("+", "").strip()

    try:
        if original_clean:
            (
                supabase.table("chat_history")
                .update(restore_payload)
                .eq("original_chat_id", original_clean)
                .eq("property_id", property_id)
                .eq("channel", current_channel)
                .execute()
            )
        (
            supabase.table("chat_history")
            .update(restore_payload)
            .eq("conversation_id", clean_id)
            .eq("property_id", property_id)
            .eq("channel", current_channel)
            .execute()
        )
        return True
    except Exception:
        return False


def _hide_legacy_visible_message(outbox_row: dict) -> None:
    payload = _outbox_payload(outbox_row)
    chat_id = str(
        payload.get("chat_id")
        or outbox_row.get("chat_id")
        or outbox_row.get("recipient_id")
        or ""
    ).strip()
    if not chat_id:
        return

    property_id = payload.get("property_id")
    if property_id is None:
        property_id = outbox_row.get("property_id")
    context_id = str(payload.get("context_id") or outbox_row.get("context_id") or "").strip()
    content = str(
        outbox_row.get("visible_message_content")
        or outbox_row.get("rendered_text")
        or payload.get("rendered_text")
        or payload.get("template_name")
        or outbox_row.get("template_name")
        or ""
    ).strip()

    try:
        query = (
            supabase.table("chat_history")
            .select("id")
            .eq("conversation_id", chat_id)
            .eq("channel", "whatsapp")
            .eq("role", "bookai")
            .order("created_at", desc=True)
            .limit(1)
        )
        if property_id is not None and str(property_id).strip():
            query = query.eq("property_id", property_id)
        if context_id:
            query = query.eq("original_chat_id", context_id)
        if content:
            query = query.eq("content", content)
        rows = query.execute().data or []
        if not rows:
            return
        row_id = rows[0].get("id")
        if row_id is None:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        (
            supabase.table("chat_history")
            .update({"hidden_at": now_iso, "archived_at": now_iso})
            .eq("id", row_id)
            .execute()
        )
        log.info(
            "[WA_STATUS] legacy hidden chat_id=%s property_id=%s row_id=%s",
            chat_id,
            property_id,
            row_id,
        )
    except Exception as exc:
        log.warning("No se pudo ocultar mensaje legacy para outbox failed: %s", exc)


async def _emit_status_event(
    state,
    *,
    outbox_row: dict,
    status: str,
    event_type: str,
    error_code: str | None = None,
    error_details: str | None = None,
) -> None:
    socket_mgr = getattr(state, "socket_manager", None)
    if not socket_mgr or not getattr(socket_mgr, "enabled", False):
        return

    payload = _outbox_payload(outbox_row)
    chat_id = str(payload.get("chat_id") or outbox_row.get("chat_id") or outbox_row.get("recipient_id") or "").strip()
    property_id = payload.get("property_id")
    if property_id is None:
        property_id = outbox_row.get("property_id")
    event_payload = {
        "type": event_type,
        "provider": outbox_row.get("provider") or "meta",
        "provider_message_id": outbox_row.get("provider_message_id"),
        "status": status,
        "recipient_id": outbox_row.get("recipient_id"),
        "chat_id": chat_id,
        "property_id": property_id,
        "error_code": error_code,
        "error_details": error_details,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await socket_mgr.emit(
        "wa.send.status",
        event_payload,
        rooms=_status_rooms(outbox_row),
        instance_id=payload.get("instance_id") or outbox_row.get("instance_id"),
    )


async def _confirm_visible_message_from_outbox(state, outbox_row: dict, status_value: str) -> None:
    payload = _outbox_payload(outbox_row)
    recipient_id = str(outbox_row.get("recipient_id") or "").strip()
    chat_id = str(payload.get("chat_id") or outbox_row.get("chat_id") or recipient_id).strip()
    context_id = str(payload.get("context_id") or outbox_row.get("context_id") or "").strip() or None
    session_id = context_id or chat_id or recipient_id
    if not session_id:
        return

    property_id = _parse_property_id(payload.get("property_id") or outbox_row.get("property_id"))
    instance_id = str(payload.get("instance_id") or outbox_row.get("instance_id") or "").strip() or None
    template_name = str(payload.get("template_name") or outbox_row.get("template_name") or "").strip() or None
    template_language = str(payload.get("template_language") or outbox_row.get("template_language") or "es").strip() or "es"
    rendered_message = str(
        outbox_row.get("rendered_text")
        or payload.get("rendered_text")
        or template_name
        or ""
    ).strip()
    if not rendered_message:
        return

    memory_manager = getattr(state, "memory_manager", None)
    if memory_manager:
        targets = [chat_id, context_id, session_id, recipient_id]
        for target in targets:
            target_value = str(target or "").strip()
            if not target_value:
                continue
            try:
                memory_manager.set_flag(target_value, "default_channel", "whatsapp")
                memory_manager.set_flag(target_value, "guest_number", recipient_id or chat_id)
                if property_id is not None:
                    memory_manager.set_flag(target_value, "property_id", property_id)
                if instance_id:
                    memory_manager.set_flag(target_value, "instance_id", instance_id)
                    memory_manager.set_flag(target_value, "instance_hotel_code", instance_id)
            except Exception:
                pass

    chat_visible_before = False
    if property_id is not None:
        chat_visible_before = is_chat_visible_in_list(
            chat_id or recipient_id,
            property_id=property_id,
            channel="whatsapp",
            original_chat_id=context_id,
        )

    if memory_manager:
        try:
            memory_manager.save(
                session_id,
                role="bookai",
                content=rendered_message,
                channel="whatsapp",
                original_chat_id=context_id,
            )
            source_tag = instance_id or "webhook"
            memory_manager.save(
                session_id,
                role="system",
                content=(
                    f"[TEMPLATE_SENT] plantilla={template_name or ''} "
                    f"lang={template_language} instance={instance_id or ''} origen={source_tag}"
                ).strip(),
                channel="whatsapp",
                original_chat_id=context_id,
            )
        except Exception as exc:
            log.warning("No se pudo confirmar mensaje visible desde outbox (%s): %s", session_id, exc)
            return

    mark_wa_outbox_message_visible(
        str(outbox_row.get("provider_message_id") or "").strip(),
        visible_message_content=rendered_message,
    )

    if property_id is not None and not chat_visible_before:
        _restore_chat_visibility(
            chat_id,
            property_id=property_id,
            channel="whatsapp",
            original_chat_id=context_id,
        )
    chat_visible_after = False
    if property_id is not None:
        chat_visible_after = is_chat_visible_in_list(
            chat_id or recipient_id,
            property_id=property_id,
            channel="whatsapp",
            original_chat_id=context_id,
        )

    socket_mgr = getattr(state, "socket_manager", None)
    if socket_mgr and getattr(socket_mgr, "enabled", False):
        now_iso = datetime.now(timezone.utc).isoformat()
        rooms = _status_rooms(outbox_row)
        if property_id is not None and not chat_visible_before and chat_visible_after:
            await socket_mgr.emit(
                "chat.list.updated",
                {
                    "property_id": property_id,
                    "action": "created",
                    "chat": {
                        "chat_id": chat_id,
                        "property_id": property_id,
                        "channel": "whatsapp",
                        "last_message": rendered_message,
                        "last_message_at": now_iso,
                        "client_phone": recipient_id or chat_id,
                        "unread_count": 0,
                    },
                },
                rooms=f"property:{property_id}",
                instance_id=instance_id,
            )
        await socket_mgr.emit(
            "chat.message.created",
            {
                "chat_id": chat_id,
                "property_id": property_id,
                "channel": "whatsapp",
                "sender": "bookai",
                "message": rendered_message,
                "created_at": now_iso,
                "template": template_name,
                "template_language": template_language,
                "provider_message_id": outbox_row.get("provider_message_id"),
                "delivery_status": status_value,
            },
            rooms=rooms,
        )
        await socket_mgr.emit(
            "chat.updated",
            {
                "chat_id": chat_id,
                "property_id": property_id,
                "channel": "whatsapp",
                "last_message": rendered_message,
                "last_message_at": now_iso,
                "provider_message_id": outbox_row.get("provider_message_id"),
                "delivery_status": status_value,
            },
            rooms=rooms,
        )

    try:
        await sync_guest_offer_state_from_sent_wa(
            state,
            guest_id=chat_id or recipient_id,
            sent_message=rendered_message,
            source="template_webhook_confirmed",
            session_id=session_id,
            property_id=property_id,
        )
    except Exception:
        pass


async def _handle_status_payload(state, payload: dict) -> dict:
    processed = 0
    matched = 0
    status_events = _iter_status_events(payload)
    for status_event in status_events:
        processed += 1
        provider_message_id = str(status_event.get("id") or "").strip()
        status_value = str(status_event.get("status") or "").strip().lower()
        if not provider_message_id or not status_value:
            continue

        outbox_row = find_wa_outbox_message(provider_message_id)
        if not outbox_row:
            log.info(
                "[WA_STATUS] unmatched wamid=%s status=%s (idempotent skip)",
                provider_message_id,
                status_value,
            )
            continue
        matched += 1
        error_code, error_details = _extract_status_error(status_event)

        if status_value in {"sent", "delivered", "read"}:
            updated = update_wa_outbox_message(
                provider_message_id,
                status=status_value,
                last_webhook_payload=status_event,
            )
            outbox_row = updated or outbox_row
            outbox_row = find_wa_outbox_message(provider_message_id) or outbox_row
            if outbox_row.get("visible_message_created_at"):
                log.info(
                    "[WA_STATUS] already visible wamid=%s status=%s",
                    provider_message_id,
                    status_value,
                )
            else:
                await _confirm_visible_message_from_outbox(state, outbox_row, status_value)
                outbox_row = find_wa_outbox_message(provider_message_id) or outbox_row
            await _emit_status_event(
                state,
                outbox_row=outbox_row,
                status=status_value,
                event_type="wa_send_confirmed",
            )
            log.info(
                "[WA_STATUS] confirmed wamid=%s status=%s chat_id=%s",
                provider_message_id,
                status_value,
                outbox_row.get("chat_id") or outbox_row.get("recipient_id"),
            )
            continue

        if status_value == "failed":
            updated = update_wa_outbox_message(
                provider_message_id,
                status="failed",
                error_code=error_code,
                error_details=error_details,
                last_webhook_payload=status_event,
            )
            outbox_row = updated or outbox_row
            _hide_legacy_visible_message(outbox_row)
            is_no_account = _is_no_whatsapp_account_error(
                error_code=error_code,
                error_details=error_details,
            )
            await _emit_status_event(
                state,
                outbox_row=outbox_row,
                status="failed",
                event_type="wa_send_failed_no_account" if is_no_account else "wa_send_failed",
                error_code=error_code,
                error_details=error_details,
            )
            log.warning(
                "[WA_STATUS] failed wamid=%s code=%s no_account=%s details=%s",
                provider_message_id,
                error_code,
                is_no_account,
                error_details,
            )
            continue

        update_wa_outbox_message(
            provider_message_id,
            status=status_value,
            last_webhook_payload=status_event,
        )
        log.info(
            "[WA_STATUS] updated wamid=%s status=%s",
            provider_message_id,
            status_value,
        )

    return {"processed": processed, "matched": matched}


def register_whatsapp_routes(app, state):
    """Registra los endpoints de webhook de WhatsApp en la app FastAPI."""

    @app.get("/webhook")
    async def verify_webhook(request: Request):
        verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "midemo")
        params = request.query_params
        if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == verify_token:
            return PlainTextResponse(params.get("hub.challenge"))
        return JSONResponse({"error": "Invalid verification token"}, status_code=403)

    @app.post("/webhook")
    async def whatsapp_webhook(request: Request):
        """Webhook WhatsApp (Meta) + Buffer inteligente + Transcripción de audio (Whisper)."""
        try:
            data = await request.json()
            status_result = await _handle_status_payload(state, data)
            value = data.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {})
            if status_result.get("processed", 0) > 0 and not value.get("messages"):
                return JSONResponse({"status": "status_processed", **status_result})
            msg = value.get("messages", [{}])[0]
            metadata = value.get("metadata", {}) or {}
            contacts = value.get("contacts", [])
            profile = contacts[0].get("profile", {}) if contacts else {}
            client_name = profile.get("name")
            sender = msg.get("from")
            msg_type = msg.get("type")
            msg_id = msg.get("id")

            text = ""
            instance_number = metadata.get("display_phone_number") or ""
            normalized_instance_number = re.sub(r"\D", "", str(instance_number or "")).strip() or instance_number
            instance_phone_id = metadata.get("phone_number_id") or ""
            memory_id = f"{normalized_instance_number}:{sender}" if normalized_instance_number and sender else sender
            instance_token = None
            if sender and instance_number:
                try:
                    from core.instance_context import hydrate_dynamic_context, fetch_instance_by_phone_id, _resolve_property_table

                    # Guarda identificadores crudos para fallback posterior.
                    if state.memory_manager:
                        if normalized_instance_number:
                            state.memory_manager.set_flag(memory_id, "instance_number", normalized_instance_number)
                        if instance_phone_id:
                            state.memory_manager.set_flag(memory_id, "whatsapp_phone_id", instance_phone_id)

                    hydrate_dynamic_context(
                        state=state,
                        chat_id=memory_id,
                        instance_number=normalized_instance_number,
                        instance_phone_id=instance_phone_id or None,
                    )
                    # Fallback duro: si no quedó instance_id, resolver directo por phone_id.
                    if instance_phone_id:
                        mm = state.memory_manager
                        if mm and not (mm.get_flag(memory_id, "instance_id") or mm.get_flag(memory_id, "instance_hotel_code")):
                            payload = fetch_instance_by_phone_id(instance_phone_id)
                            if payload:
                                inst_id = payload.get("instance_id") or payload.get("instance_url")
                                if inst_id:
                                    mm.set_flag(memory_id, "instance_id", inst_id)
                                    mm.set_flag(memory_id, "instance_hotel_code", inst_id)
                                inst_url = payload.get("instance_url")
                                if inst_url:
                                    mm.set_flag(memory_id, "instance_url", inst_url)
                                table = _resolve_property_table(payload)
                                if table:
                                    mm.set_flag(memory_id, "property_table", table)
                                for key in ("whatsapp_phone_id", "whatsapp_token", "whatsapp_verify_token"):
                                    val = payload.get(key)
                                    if val:
                                        mm.set_flag(memory_id, key, val)
                    instance_phone_id = state.memory_manager.get_flag(memory_id, "whatsapp_phone_id")
                    instance_token = state.memory_manager.get_flag(memory_id, "whatsapp_token")
                    if instance_phone_id:
                        state.memory_manager.set_flag(sender, "whatsapp_phone_id", instance_phone_id)
                    if instance_token:
                        state.memory_manager.set_flag(sender, "whatsapp_token", instance_token)
                except Exception as exc:
                    log.warning("No se pudo hidratar contexto en webhook: %s", exc)

            if msg_id:
                _mark_as_read(msg_id, phone_id=instance_phone_id, token=instance_token)
                if msg_id in state.processed_whatsapp_ids:
                    log.info("↩️ WhatsApp duplicado ignorado (msg_id=%s)", msg_id)
                    return JSONResponse({"status": "duplicate"})
                if len(state.processed_whatsapp_queue) >= state.processed_whatsapp_queue.maxlen:
                    old = state.processed_whatsapp_queue.popleft()
                    state.processed_whatsapp_ids.discard(old)
                state.processed_whatsapp_queue.append(msg_id)
                state.processed_whatsapp_ids.add(msg_id)

            if msg_type == "text":
                text = msg.get("text", {}).get("body", "")
            elif msg_type == "audio":
                from channels_wrapper.utils.media_utils import transcribe_audio

                media_id = msg.get("audio", {}).get("id")
                if media_id:
                    log.info("🎧 Audio recibido (media_id=%s), iniciando transcripción...", media_id)
                    whatsapp_token = instance_token or os.getenv("WHATSAPP_TOKEN", "")
                    openai_key = os.getenv("OPENAI_API_KEY", "")
                    text = transcribe_audio(media_id, whatsapp_token, openai_key)
                    log.info("📝 Transcripción completada: %s", text)

            if not sender or not text:
                return JSONResponse({"status": "ignored"})

            log.info("💬 WhatsApp %s: %s", sender, text)
            chat_visible_before = False
            if client_name:
                state.memory_manager.set_flag(memory_id, "client_name", client_name)
            state.memory_manager.set_flag(memory_id, "guest_number", sender)
            state.memory_manager.set_flag(memory_id, "force_guest_role", True)
            if sender and sender != memory_id:
                state.memory_manager.set_flag(sender, "force_guest_role", True)
                # Alias para que el chatter pueda ubicar el memory_id compuesto.
                state.memory_manager.set_flag(sender, "last_memory_id", memory_id)

            try:
                property_id = state.memory_manager.get_flag(memory_id, "property_id")
                # Resolución estricta: solo por contexto de instancia.
                instance_id = (
                    state.memory_manager.get_flag(memory_id, "instance_id")
                    or state.memory_manager.get_flag(memory_id, "instance_hotel_code")
                )
                if property_id is None and instance_id:
                    from core.instance_context import fetch_properties_by_code, DEFAULT_PROPERTY_TABLE

                    table = state.memory_manager.get_flag(memory_id, "property_table") or DEFAULT_PROPERTY_TABLE
                    rows = fetch_properties_by_code(table, str(instance_id)) if table else []
                    if isinstance(rows, list) and rows:
                        # Acepta múltiples filas si todas apuntan al mismo property_id.
                        prop_ids = {
                            (row.get("property_id") if isinstance(row, dict) else None)
                            if (isinstance(row, dict) and row.get("property_id") is not None)
                            else (row.get("id") if isinstance(row, dict) else None)
                            for row in rows
                            if isinstance(row, dict)
                            and ((row.get("property_id") is not None) or (row.get("id") is not None))
                        }
                        if len(prop_ids) == 1:
                            property_id = next(iter(prop_ids))
                            state.memory_manager.set_flag(memory_id, "property_id", property_id)
                if property_id is None:
                    property_id = _resolve_property_id_fallback(memory_id, sender)
                    if property_id is not None:
                        state.memory_manager.set_flag(memory_id, "property_id", property_id)
                # Limpiar cualquier property_id heredado del sender global para evitar mezcla.
                if sender:
                    state.memory_manager.clear_flag(sender, "property_id")
                for key in ("folio_id", "checkin", "checkout"):
                    if state.memory_manager.get_flag(memory_id, key) is None:
                        val = state.memory_manager.get_flag(sender, key)
                        if val is not None:
                            state.memory_manager.set_flag(memory_id, key, val)
            except Exception:
                property_id = None
            if property_id is not None and sender:
                try:
                    state.memory_manager.set_flag(sender, "property_id", property_id)
                except Exception:
                    pass
            clean_sender = re.sub(r"\D", "", str(sender or "")).strip() or str(sender or "")
            context_id = str(memory_id or sender or "").strip()
            clean_chat_id = re.sub(r"\D", "", str(sender or "")).strip() or str(sender or "").strip() or context_id
            chat_visible_before = is_chat_visible_in_list(
                clean_chat_id,
                property_id=property_id,
                channel="whatsapp",
                original_chat_id=context_id,
            )
            socket_mgr = getattr(state, "socket_manager", None)
            bookai_enabled = _resolve_bookai_enabled(
                state,
                chat_id=str(sender or ""),
                mem_id=str(memory_id or ""),
                clean_id=clean_sender,
                property_id=property_id,
            )
            if bookai_enabled is False:
                try:
                    if property_id is not None:
                        state.memory_manager.set_flag(memory_id, "property_id", property_id)
                    state.memory_manager.save(
                        conversation_id=memory_id,
                        role="user",
                        content=text,
                        channel="whatsapp",
                        original_chat_id=memory_id,
                    )
                    if property_id is None:
                        try:
                            property_id = state.memory_manager.get_flag(memory_id, "property_id")
                        except Exception:
                            property_id = None
                    if property_id is None:
                        property_id = _resolve_property_id_fallback(memory_id, sender)
                        if property_id is not None:
                            state.memory_manager.set_flag(memory_id, "property_id", property_id)
                    if property_id is not None and not chat_visible_before:
                        chat_visible_before = is_chat_visible_in_list(
                            clean_chat_id,
                            property_id=property_id,
                            channel="whatsapp",
                            original_chat_id=context_id,
                        )
                    if property_id is None:
                        state.memory_manager.set_flag(
                            memory_id,
                            "pending_property_room_guest_message",
                            {
                                "chat_id": clean_chat_id,
                                "guest_chat_id": clean_chat_id,
                                "context_id": context_id,
                                "property_id": None,
                                "channel": "whatsapp",
                                "sender": "guest",
                                "message": text,
                                "created_at": datetime.now(timezone.utc).isoformat(),
                            },
                        )
                        if not chat_visible_before:
                            state.memory_manager.set_flag(
                                memory_id,
                                "pending_property_room_chat_list_updated",
                                {
                                "property_id": None,
                                "action": "created",
                                "_original_chat_id": context_id,
                                "chat": {
                                        "chat_id": clean_chat_id,
                                        "property_id": None,
                                        "reservation_locator": state.memory_manager.get_flag(memory_id, "reservation_locator"),
                                        "reservation_status": state.memory_manager.get_flag(memory_id, "reservation_status"),
                                        "room_number": state.memory_manager.get_flag(memory_id, "room_number"),
                                        "checkin": state.memory_manager.get_flag(memory_id, "checkin"),
                                        "checkout": state.memory_manager.get_flag(memory_id, "checkout"),
                                        "channel": "whatsapp",
                                        "last_message": text,
                                        "last_message_at": datetime.now(timezone.utc).isoformat(),
                                        "avatar": None,
                                        "client_name": client_name,
                                        "client_phone": clean_chat_id,
                                        "whatsapp_phone_number": normalized_instance_number or None,
                                        "bookai_enabled": False,
                                        "unread_count": 1,
                                        "needs_action": None,
                                        "needs_action_type": None,
                                        "needs_action_reason": None,
                                        "proposed_response": None,
                                        "is_final_response": False,
                                        "escalation_messages": None,
                                        "folio_id": state.memory_manager.get_flag(memory_id, "folio_id"),
                                    },
                                },
                            )
                            log.info("[chat.list.updated] deferred — property_id not yet resolved for %s", clean_chat_id)
                except Exception as exc:
                    log.warning("No se pudo persistir mensaje con BookAI apagado: %s", exc)
                try:
                    if socket_mgr and getattr(socket_mgr, "enabled", False):
                        rooms = [f"chat:{alias}" for alias in _chat_room_aliases(context_id, sender, clean_chat_id)]
                        if property_id is not None:
                            rooms.append(f"property:{property_id}")
                        rooms.append("channel:whatsapp")
                        now_iso = datetime.now(timezone.utc).isoformat()
                        chat_visible_after = is_chat_visible_in_list(
                            clean_chat_id,
                            property_id=property_id,
                            channel="whatsapp",
                            original_chat_id=context_id,
                        )
                        if property_id is not None and not chat_visible_before and chat_visible_after:
                            folio_id = state.memory_manager.get_flag(memory_id, "folio_id")
                            reservation_locator = state.memory_manager.get_flag(memory_id, "reservation_locator")
                            checkin = state.memory_manager.get_flag(memory_id, "checkin")
                            checkout = state.memory_manager.get_flag(memory_id, "checkout")
                            reservation_status = state.memory_manager.get_flag(memory_id, "reservation_status")
                            room_number = state.memory_manager.get_flag(memory_id, "room_number")
                            await socket_mgr.emit(
                                "chat.list.updated",
                                {
                                    "property_id": property_id,
                                    "action": "created",
                                    "chat": {
                                        "chat_id": clean_chat_id,
                                        "property_id": property_id,
                                        "reservation_locator": reservation_locator,
                                        "reservation_status": reservation_status,
                                        "room_number": room_number,
                                        "checkin": checkin,
                                        "checkout": checkout,
                                        "channel": "whatsapp",
                                        "last_message": text,
                                        "last_message_at": now_iso,
                                        "avatar": None,
                                        "client_name": client_name,
                                        "client_phone": clean_chat_id,
                                        "whatsapp_phone_number": normalized_instance_number or None,
                                        "bookai_enabled": False,
                                        "unread_count": 1,
                                        "needs_action": None,
                                        "needs_action_type": None,
                                        "needs_action_reason": None,
                                        "proposed_response": None,
                                        "is_final_response": False,
                                        "escalation_messages": None,
                                        "folio_id": folio_id,
                                    },
                                },
                                rooms=f"property:{property_id}",
                                instance_id=instance_id,
                            )
                        await socket_mgr.emit(
                            "chat.message.created",
                            {
                                "chat_id": clean_chat_id,
                                "guest_chat_id": clean_chat_id,
                                "context_id": context_id,
                                "property_id": property_id,
                                "channel": "whatsapp",
                                "sender": "guest",
                                "message": text,
                                "created_at": now_iso,
                            },
                            rooms=rooms,
                        )
                        await socket_mgr.emit(
                            "chat.updated",
                            {
                                "chat_id": clean_chat_id,
                                "guest_chat_id": clean_chat_id,
                                "context_id": context_id,
                                "property_id": property_id,
                                "channel": "whatsapp",
                                "last_message": text,
                                "last_message_at": now_iso,
                            },
                            rooms=rooms,
                        )
                except Exception as exc:
                    log.warning("No se pudo emitir mensaje entrante con BookAI apagado: %s", exc)
                log.info(
                    "🤫 BookAI desactivado para %s (property_id=%s); mensaje no encolado.",
                    clean_sender,
                    property_id,
                )
                return JSONResponse({"status": "bookai_disabled"})
            # Registrar en RAM el mensaje entrante en el contexto compuesto de instancia.
            # La persistencia en DB la hará el flujo normal del agente para evitar duplicados.
            try:
                if property_id is not None:
                    state.memory_manager.set_flag(memory_id, "property_id", property_id)
                state.memory_manager.add_runtime_message(
                    conversation_id=memory_id,
                    role="user",
                    content=text,
                    channel="whatsapp",
                    original_chat_id=memory_id,
                )
            except Exception as exc:
                    log.warning("No se pudo guardar mensaje entrante en RAM (webhook): %s", exc)
            if socket_mgr and getattr(socket_mgr, "enabled", False):
                current_property_id = property_id
                if current_property_id is None:
                    try:
                        current_property_id = state.memory_manager.get_flag(memory_id, "property_id")
                    except Exception:
                        current_property_id = None
                if current_property_id is None:
                    try:
                        current_property_id = _resolve_property_id_fallback(memory_id, sender)
                        if current_property_id is not None:
                            state.memory_manager.set_flag(memory_id, "property_id", current_property_id)
                    except Exception:
                        current_property_id = None
                property_id = current_property_id
                if property_id is not None and not chat_visible_before:
                    chat_visible_before = is_chat_visible_in_list(
                        clean_chat_id,
                        property_id=property_id,
                        channel="whatsapp",
                        original_chat_id=context_id,
                    )
                if property_id is None and not chat_visible_before:
                    state.memory_manager.set_flag(
                        memory_id,
                        "pending_property_room_chat_list_updated",
                        {
                            "property_id": None,
                            "action": "created",
                            "_original_chat_id": context_id,
                            "chat": {
                                "chat_id": clean_chat_id,
                                "property_id": None,
                                "reservation_locator": state.memory_manager.get_flag(memory_id, "reservation_locator"),
                                "reservation_status": state.memory_manager.get_flag(memory_id, "reservation_status"),
                                "room_number": state.memory_manager.get_flag(memory_id, "room_number"),
                                "checkin": state.memory_manager.get_flag(memory_id, "checkin"),
                                "checkout": state.memory_manager.get_flag(memory_id, "checkout"),
                                "channel": "whatsapp",
                                "last_message": text,
                                "last_message_at": datetime.now(timezone.utc).isoformat(),
                                "avatar": None,
                                "client_name": client_name,
                                "client_phone": clean_chat_id,
                                "whatsapp_phone_number": normalized_instance_number or None,
                                "bookai_enabled": True,
                                "unread_count": 1,
                                "needs_action": None,
                                "needs_action_type": None,
                                "needs_action_reason": None,
                                "proposed_response": None,
                                "is_final_response": False,
                                "escalation_messages": None,
                                "folio_id": state.memory_manager.get_flag(memory_id, "folio_id"),
                            },
                        },
                    )
                rooms = [f"chat:{alias}" for alias in _chat_room_aliases(context_id, sender, clean_chat_id)]
                if property_id is not None:
                    rooms.append(f"property:{property_id}")
                rooms.append("channel:whatsapp")
                now_iso = datetime.now(timezone.utc).isoformat()
                if property_id is not None and not chat_visible_before:
                    folio_id = state.memory_manager.get_flag(memory_id, "folio_id")
                    reservation_locator = state.memory_manager.get_flag(memory_id, "reservation_locator")
                    checkin = state.memory_manager.get_flag(memory_id, "checkin")
                    checkout = state.memory_manager.get_flag(memory_id, "checkout")
                    reservation_status = state.memory_manager.get_flag(memory_id, "reservation_status")
                    room_number = state.memory_manager.get_flag(memory_id, "room_number")
                    await socket_mgr.emit(
                        "chat.list.updated",
                        {
                            "property_id": property_id,
                            "action": "created",
                            "chat": {
                                "chat_id": clean_chat_id,
                                "property_id": property_id,
                                "reservation_locator": reservation_locator,
                                "reservation_status": reservation_status,
                                "room_number": room_number,
                                "checkin": checkin,
                                "checkout": checkout,
                                "channel": "whatsapp",
                                "last_message": text,
                                "last_message_at": now_iso,
                                "avatar": None,
                                "client_name": client_name,
                                "client_phone": clean_chat_id,
                                "whatsapp_phone_number": normalized_instance_number or None,
                                "bookai_enabled": True,
                                "unread_count": 1,
                                "needs_action": None,
                                "needs_action_type": None,
                                "needs_action_reason": None,
                                "proposed_response": None,
                                "is_final_response": False,
                                "escalation_messages": None,
                                "folio_id": folio_id,
                            },
                        },
                        rooms=f"property:{property_id}",
                        instance_id=instance_id,
                    )
                await socket_mgr.emit(
                    "chat.message.created",
                    {
                        "chat_id": clean_chat_id,
                        "guest_chat_id": clean_chat_id,
                        "context_id": context_id,
                        "property_id": property_id,
                        "channel": "whatsapp",
                        "sender": "guest",
                        "message": text,
                        "created_at": now_iso,
                    },
                    rooms=rooms,
                )
                await socket_mgr.emit(
                    "chat.updated",
                    {
                        "chat_id": clean_chat_id,
                        "guest_chat_id": clean_chat_id,
                        "context_id": context_id,
                        "property_id": property_id,
                        "channel": "whatsapp",
                        "last_message": text,
                        "last_message_at": now_iso,
                    },
                    rooms=rooms,
                )

            async def _process_buffered(cid: str, combined_text: str, version: int):
                log.info(
                    "🧠 Procesando lote buffered v%s → %s\n🧩 Mensajes combinados:\n%s",
                    version,
                    cid,
                    combined_text,
                )
                resp = await process_user_message(
                    combined_text,
                    sender,
                    state=state,
                    channel="whatsapp",
                    instance_number=normalized_instance_number,
                    memory_id=cid,
                    property_id=property_id,
                )

                if not resp:
                    log.info("🔇 Escalación silenciosa %s", cid)
                    return

                async def send_to_channel(uid: str, txt: str):
                    await state.channel_manager.send_message(
                        uid,
                        txt,
                        channel="whatsapp",
                        context_id=cid,
                    )

                final_bookai_enabled = _resolve_bookai_enabled(
                    state,
                    chat_id=str(sender or ""),
                    mem_id=str(cid or ""),
                    clean_id=re.sub(r"\D", "", str(sender or "")).strip() or str(sender or ""),
                    property_id=property_id,
                )
                if final_bookai_enabled is False:
                    log.info(
                        "🤫 BookAI desactivado antes del envio para %s (property_id=%s); respuesta descartada.",
                        re.sub(r"\D", "", str(sender or "")).strip() or str(sender or ""),
                        property_id,
                    )
                    return

                await send_fragmented_async(send_to_channel, sender, resp)

            await state.buffer_manager.add_message(memory_id, text, _process_buffered)

            return JSONResponse({"status": "queued"})

        except Exception as exc:
            log.error("❌ Error en webhook WhatsApp: %s", exc, exc_info=True)
            return JSONResponse({"status": "error"}, status_code=500)
