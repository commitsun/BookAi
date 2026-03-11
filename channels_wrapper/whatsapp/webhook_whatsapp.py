"""Handlers del webhook de WhatsApp (Meta)."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone

import requests
from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse

from channels_wrapper.utils.text_utils import send_fragmented_async
from core.db import is_chat_visible_in_list
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


def _build_active_whatsapp_window(created_at: str | None = None) -> dict:
    base_dt = None
    if created_at:
        try:
            base_dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        except Exception:
            base_dt = None
    if base_dt is None:
        base_dt = datetime.now(timezone.utc)
    if base_dt.tzinfo is None:
        base_dt = base_dt.replace(tzinfo=timezone.utc)
    expires_at = (base_dt + timedelta(hours=24)).astimezone(timezone.utc).replace(microsecond=0)
    return {
        "status": "active",
        "remaining_hours": 24.0,
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
    }


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


def _resolve_property_id_fallback(memory_id: str, sender_phone_id: str) -> str | int | None:
    """Intenta recuperar property_id desde historial/reservas cuando llega nulo en webhook."""
    try:
        from core.db import supabase
        from core.config import Settings
    except Exception:
        return None

    raw_memory = str(memory_id or "").strip()
    raw_sender = str(sender_phone_id or "").strip()
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
            value = data.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {})
            msg = value.get("messages", [{}])[0]
            metadata = value.get("metadata", {}) or {}
            contacts_raw = value.get("contacts", [])
            contacts = contacts_raw if isinstance(contacts_raw, list) else []
            message_from = str(msg.get("from") or "").strip()
            profile: dict = {}
            sender_source = "missing"
            sender_phone_id = ""

            # En payloads de Meta el remitente canónico llega en contacts[].wa_id.
            if message_from:
                for contact in contacts:
                    if not isinstance(contact, dict):
                        continue
                    wa_id = str(contact.get("wa_id") or "").strip()
                    if wa_id and wa_id == message_from:
                        sender_phone_id = wa_id
                        sender_source = "contacts.wa_id"
                        candidate_profile = contact.get("profile")
                        if isinstance(candidate_profile, dict):
                            profile = candidate_profile
                        break
            if not sender_phone_id:
                for contact in contacts:
                    if not isinstance(contact, dict):
                        continue
                    wa_id = str(contact.get("wa_id") or "").strip()
                    if wa_id:
                        sender_phone_id = wa_id
                        sender_source = "contacts.wa_id"
                        candidate_profile = contact.get("profile")
                        if isinstance(candidate_profile, dict):
                            profile = candidate_profile
                        break
            if not sender_phone_id and message_from:
                sender_phone_id = message_from
                sender_source = "messages.from_fallback"

            if not profile and contacts and isinstance(contacts[0], dict):
                candidate_profile = contacts[0].get("profile")
                if isinstance(candidate_profile, dict):
                    profile = candidate_profile

            client_name = profile.get("name")
            msg_type = str(msg.get("type") or "").strip()
            msg_id = msg.get("id")

            text = ""
            unsupported_message_type = ""
            instance_display_phone_number = metadata.get("display_phone_number") or ""
            normalized_instance_number = (
                re.sub(r"\D", "", str(instance_display_phone_number or "")).strip()
                or str(instance_display_phone_number or "").strip()
            )
            instance_phone_id = str(metadata.get("phone_number_id") or "").strip()
            instance_context_key = normalized_instance_number or instance_phone_id
            memory_id = (
                f"{instance_context_key}:{sender_phone_id}"
                if instance_context_key and sender_phone_id
                else sender_phone_id
            )
            instance_token = None
            log.info(
                "[WA_WEBHOOK] msg_id=%s type=%s sender_source=%s sender_phone_id=%s display_phone_number=%s phone_number_id=%s",
                msg_id or "",
                msg_type or "unknown",
                sender_source,
                sender_phone_id or "",
                normalized_instance_number or "",
                instance_phone_id or "",
            )

            # display_phone_number se mantiene para contexto de instancia; si falta, usamos phone_number_id.
            if sender_phone_id and (normalized_instance_number or instance_phone_id):
                try:
                    from core.instance_context import hydrate_dynamic_context

                    # Guarda identificadores crudos para fallback posterior.
                    if state.memory_manager:
                        if normalized_instance_number:
                            state.memory_manager.set_flag(memory_id, "instance_number", normalized_instance_number)
                        if instance_phone_id:
                            state.memory_manager.set_flag(memory_id, "whatsapp_phone_id", instance_phone_id)

                    hydrate_dynamic_context(
                        state=state,
                        chat_id=memory_id,
                        instance_number=normalized_instance_number or None,
                        instance_phone_id=instance_phone_id or None,
                    )
                    mm = state.memory_manager
                    if mm:
                        resolved_instance_id = (
                            mm.get_flag(memory_id, "instance_id")
                            or mm.get_flag(memory_id, "instance_hotel_code")
                        )
                        if not resolved_instance_id:
                            log.info(
                                "[WA_WEBHOOK] contexto de instancia no resuelto via hydrate; se mantiene metadata directa (phone_number_id=%s, display_phone_number=%s)",
                                instance_phone_id or "",
                                normalized_instance_number or "",
                            )
                        instance_phone_id = str(mm.get_flag(memory_id, "whatsapp_phone_id") or instance_phone_id or "").strip()
                        instance_token = mm.get_flag(memory_id, "whatsapp_token")
                    if instance_phone_id:
                        state.memory_manager.set_flag(sender_phone_id, "whatsapp_phone_id", instance_phone_id)
                    if instance_token:
                        state.memory_manager.set_flag(sender_phone_id, "whatsapp_token", instance_token)
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
            else:
                unsupported_message_type = msg_type or "unknown"
                unsupported_message_labels = {
                    "image": "una imagen",
                    "video": "un video",
                    "document": "un documento",
                    "sticker": "un sticker",
                    "location": "una ubicación",
                    "contacts": "contactos",
                    "interactive": "un mensaje interactivo",
                    "button": "una respuesta de botón",
                    "reaction": "una reacción",
                }
                human_label = unsupported_message_labels.get(
                    unsupported_message_type,
                    f"un mensaje de tipo {unsupported_message_type}",
                )
                text = f"El huésped envió {human_label}."
                log.info(
                    "📎 Mensaje WhatsApp no soportado funcionalmente: type=%s sender=%s (se registra nota).",
                    unsupported_message_type,
                    sender_phone_id or "",
                )

            if not sender_phone_id or not text:
                return JSONResponse({"status": "ignored"})

            log.info("💬 WhatsApp %s: %s", sender_phone_id, text)
            chat_visible_before = False
            if client_name:
                state.memory_manager.set_flag(memory_id, "client_name", client_name)
            state.memory_manager.set_flag(memory_id, "guest_number", sender_phone_id)
            state.memory_manager.set_flag(memory_id, "force_guest_role", True)
            if sender_phone_id and sender_phone_id != memory_id:
                state.memory_manager.set_flag(sender_phone_id, "force_guest_role", True)
                # Alias para que el chatter pueda ubicar el memory_id compuesto.
                state.memory_manager.set_flag(sender_phone_id, "last_memory_id", memory_id)

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
                    property_id = _resolve_property_id_fallback(memory_id, sender_phone_id)
                    if property_id is not None:
                        state.memory_manager.set_flag(memory_id, "property_id", property_id)
                # Limpiar cualquier property_id heredado del sender global para evitar mezcla.
                if sender_phone_id:
                    state.memory_manager.clear_flag(sender_phone_id, "property_id")
                for key in ("folio_id", "checkin", "checkout"):
                    if state.memory_manager.get_flag(memory_id, key) is None:
                        val = state.memory_manager.get_flag(sender_phone_id, key)
                        if val is not None:
                            state.memory_manager.set_flag(memory_id, key, val)
            except Exception:
                property_id = None
            if property_id is not None and sender_phone_id:
                try:
                    state.memory_manager.set_flag(sender_phone_id, "property_id", property_id)
                except Exception:
                    pass
            clean_sender = re.sub(r"\D", "", str(sender_phone_id or "")).strip() or str(sender_phone_id or "")
            context_id = str(memory_id or sender_phone_id or "").strip()
            clean_chat_id = (
                re.sub(r"\D", "", str(sender_phone_id or "")).strip()
                or str(sender_phone_id or "").strip()
                or context_id
            )
            chat_visible_before = is_chat_visible_in_list(
                clean_chat_id,
                property_id=property_id,
                channel="whatsapp",
                original_chat_id=context_id,
            )
            socket_mgr = getattr(state, "socket_manager", None)
            bookai_enabled = _resolve_bookai_enabled(
                state,
                chat_id=str(sender_phone_id or ""),
                mem_id=str(memory_id or ""),
                clean_id=clean_sender,
                property_id=property_id,
            )

            def _resolve_property_id_with_fallback(current_property_id):
                resolved_property_id = current_property_id
                if resolved_property_id is None:
                    try:
                        resolved_property_id = state.memory_manager.get_flag(memory_id, "property_id")
                    except Exception:
                        resolved_property_id = None
                if resolved_property_id is None:
                    try:
                        resolved_property_id = _resolve_property_id_fallback(memory_id, sender_phone_id)
                        if resolved_property_id is not None:
                            state.memory_manager.set_flag(memory_id, "property_id", resolved_property_id)
                    except Exception:
                        resolved_property_id = None
                return resolved_property_id

            def _build_chat_list_chat_payload(
                *,
                property_id_value,
                now_iso: str,
                bookai_enabled_value: bool,
            ) -> dict:
                return {
                    "chat_id": clean_chat_id,
                    "property_id": property_id_value,
                    "reservation_locator": state.memory_manager.get_flag(memory_id, "reservation_locator"),
                    "reservation_status": state.memory_manager.get_flag(memory_id, "reservation_status"),
                    "room_number": state.memory_manager.get_flag(memory_id, "room_number"),
                    "checkin": state.memory_manager.get_flag(memory_id, "checkin"),
                    "checkout": state.memory_manager.get_flag(memory_id, "checkout"),
                    "channel": "whatsapp",
                    "last_message": text,
                    "last_message_at": now_iso,
                    "avatar": None,
                    "client_name": client_name,
                    "client_phone": clean_chat_id,
                    "whatsapp_phone_number": normalized_instance_number or None,
                    "whatsapp_window": _build_active_whatsapp_window(now_iso),
                    "bookai_enabled": bool(bookai_enabled_value),
                    "unread_count": 1,
                    "needs_action": None,
                    "needs_action_type": None,
                    "needs_action_reason": None,
                    "proposed_response": None,
                    "is_final_response": False,
                    "escalation_messages": None,
                    "folio_id": state.memory_manager.get_flag(memory_id, "folio_id"),
                }

            def _build_pending_chat_list_updated_payload(*, bookai_enabled_value: bool) -> dict:
                return {
                    "property_id": None,
                    "action": "created",
                    "_original_chat_id": context_id,
                    "chat": _build_chat_list_chat_payload(
                        property_id_value=None,
                        now_iso=datetime.now(timezone.utc).isoformat(),
                        bookai_enabled_value=bookai_enabled_value,
                    ),
                }

            def _build_incoming_message_payload(*, property_id_value, now_iso: str) -> dict:
                return {
                    "chat_id": clean_chat_id,
                    "guest_chat_id": clean_chat_id,
                    "context_id": context_id,
                    "property_id": property_id_value,
                    "channel": "whatsapp",
                    "sender": "guest",
                    "message": text,
                    "created_at": now_iso,
                    "whatsapp_window": _build_active_whatsapp_window(now_iso),
                }

            async def _emit_chat_list_updated_if_needed(
                *,
                property_id_value,
                now_iso: str,
                bookai_enabled_value: bool,
                require_visibility_after_check: bool,
            ) -> None:
                if property_id_value is None or chat_visible_before:
                    return
                if require_visibility_after_check:
                    chat_visible_after = is_chat_visible_in_list(
                        clean_chat_id,
                        property_id=property_id_value,
                        channel="whatsapp",
                        original_chat_id=context_id,
                    )
                    if not chat_visible_after:
                        return
                await socket_mgr.emit(
                    "chat.list.updated",
                    {
                        "property_id": property_id_value,
                        "action": "created",
                        "chat": _build_chat_list_chat_payload(
                            property_id_value=property_id_value,
                            now_iso=now_iso,
                            bookai_enabled_value=bookai_enabled_value,
                        ),
                    },
                    rooms=f"property:{property_id_value}",
                    instance_id=instance_id,
                )

            async def _emit_incoming_message_events(*, rooms, property_id_value, now_iso: str) -> None:
                incoming_message_payload = _build_incoming_message_payload(
                    property_id_value=property_id_value,
                    now_iso=now_iso,
                )
                await socket_mgr.emit(
                    "chat.message.created",
                    incoming_message_payload,
                    rooms=rooms,
                )
                await socket_mgr.emit(
                    "chat.message.new",
                    incoming_message_payload,
                    rooms=rooms,
                )
                await socket_mgr.emit(
                    "chat.updated",
                    {
                        "chat_id": clean_chat_id,
                        "guest_chat_id": clean_chat_id,
                        "context_id": context_id,
                        "property_id": property_id_value,
                        "channel": "whatsapp",
                        "last_message": text,
                        "last_message_at": now_iso,
                    },
                    rooms=rooms,
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
                    property_id = _resolve_property_id_with_fallback(property_id)
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
                                "whatsapp_window": _build_active_whatsapp_window(),
                            },
                        )
                        if not chat_visible_before:
                            state.memory_manager.set_flag(
                                memory_id,
                                "pending_property_room_chat_list_updated",
                                _build_pending_chat_list_updated_payload(bookai_enabled_value=False),
                            )
                            log.info("[chat.list.updated] deferred — property_id not yet resolved for %s", clean_chat_id)
                except Exception as exc:
                    log.warning("No se pudo persistir mensaje con BookAI apagado: %s", exc)
                try:
                    if socket_mgr and getattr(socket_mgr, "enabled", False):
                        rooms = [f"chat:{alias}" for alias in _chat_room_aliases(context_id, sender_phone_id, clean_chat_id)]
                        if property_id is not None:
                            rooms.append(f"property:{property_id}")
                        rooms.append("channel:whatsapp")
                        now_iso = datetime.now(timezone.utc).isoformat()
                        await _emit_chat_list_updated_if_needed(
                            property_id_value=property_id,
                            now_iso=now_iso,
                            bookai_enabled_value=False,
                            require_visibility_after_check=True,
                        )
                        await _emit_incoming_message_events(
                            rooms=rooms,
                            property_id_value=property_id,
                            now_iso=now_iso,
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
            # La persistencia en DB la hace el flujo normal para text/audio.
            # Para tipos no soportados se persiste aquí para no perder trazabilidad.
            try:
                if property_id is not None:
                    state.memory_manager.set_flag(memory_id, "property_id", property_id)
                if unsupported_message_type:
                    state.memory_manager.save(
                        conversation_id=memory_id,
                        role="user",
                        content=text,
                        channel="whatsapp",
                        original_chat_id=memory_id,
                        skip_recent_duplicate_guard=True,
                    )
                else:
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
                property_id = _resolve_property_id_with_fallback(property_id)
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
                        _build_pending_chat_list_updated_payload(bookai_enabled_value=True),
                    )
                rooms = [f"chat:{alias}" for alias in _chat_room_aliases(context_id, sender_phone_id, clean_chat_id)]
                if property_id is not None:
                    rooms.append(f"property:{property_id}")
                rooms.append("channel:whatsapp")
                now_iso = datetime.now(timezone.utc).isoformat()
                await _emit_chat_list_updated_if_needed(
                    property_id_value=property_id,
                    now_iso=now_iso,
                    bookai_enabled_value=True,
                    require_visibility_after_check=False,
                )
                await _emit_incoming_message_events(
                    rooms=rooms,
                    property_id_value=property_id,
                    now_iso=now_iso,
                )

            if unsupported_message_type:
                log.info(
                    "ℹ️ Mensaje WhatsApp tipo=%s registrado como nota en hilo (sender=%s) sin encolar al agente.",
                    unsupported_message_type,
                    sender_phone_id,
                )
                return JSONResponse(
                    {
                        "status": "unsupported_type_logged",
                        "message_type": unsupported_message_type,
                    }
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
                    sender_phone_id,
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
                    chat_id=str(sender_phone_id or ""),
                    mem_id=str(cid or ""),
                    clean_id=re.sub(r"\D", "", str(sender_phone_id or "")).strip() or str(sender_phone_id or ""),
                    property_id=property_id,
                )
                if final_bookai_enabled is False:
                    log.info(
                        "🤫 BookAI desactivado antes del envio para %s (property_id=%s); respuesta descartada.",
                        re.sub(r"\D", "", str(sender_phone_id or "")).strip() or str(sender_phone_id or ""),
                        property_id,
                    )
                    return

                await send_fragmented_async(send_to_channel, sender_phone_id, resp)

            await state.buffer_manager.add_message(memory_id, text, _process_buffered)

            return JSONResponse({"status": "queued"})

        except Exception as exc:
            log.error("❌ Error en webhook WhatsApp: %s", exc, exc_info=True)
            return JSONResponse({"status": "error"}, status_code=500)
