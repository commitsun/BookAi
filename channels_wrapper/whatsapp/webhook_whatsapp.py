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
from core.language_manager import language_manager

log = logging.getLogger("WhatsAppWebhook")


# Resuelve los aliases de sala para un chat.
# Se usa en el flujo de webhook WhatsApp del flujo principal multipropiedad para preparar datos, validaciones o decisiones previas.
# Recibe `values` como entrada principal según la firma.
# Devuelve un `list[str]` con el resultado de esta operación. Sin efectos secundarios relevantes.
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


# Construye la ventana activa de WhatsApp.
# Se usa en el flujo de webhook WhatsApp del flujo principal multipropiedad para preparar datos, validaciones o decisiones previas.
# Recibe `created_at` como entrada principal según la firma.
# Devuelve un `dict` con el resultado de esta operación. Sin efectos secundarios relevantes.
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


# Envía el status 'read' para reflejar doble check azul en el cliente.
# Se usa en el flujo de webhook WhatsApp del flujo principal multipropiedad para preparar datos, validaciones o decisiones previas.
# Recibe `message_id`, `phone_id`, `token` como entradas relevantes junto con el contexto inyectado en la firma.
# No devuelve un valor relevante; deja preparado el estado o ejecuta la acción necesaria. Puede realizar llamadas externas o a modelos.
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


# Limpia teléfono parecido a.
# Se usa en el flujo de webhook WhatsApp del flujo principal multipropiedad para preparar datos, validaciones o decisiones previas.
# Recibe `value` como entrada principal según la firma.
# Devuelve un `str` con el resultado de esta operación. Sin efectos secundarios relevantes.
def _clean_phone_like(value: str | None) -> str:
    raw_value = str(value or "").strip()
    return re.sub(r"\D", "", raw_value).strip() or raw_value


# Resuelve el ID de remitente teléfono.
# Se usa en el flujo de webhook WhatsApp del flujo principal multipropiedad para preparar datos, validaciones o decisiones previas.
# Recibe `contacts`, `msg` como entradas relevantes junto con el contexto inyectado en la firma.
# Devuelve un `str` con el resultado de esta operación. Sin efectos secundarios relevantes.
def _resolve_sender_phone_id(contacts: list[dict] | None, msg: dict) -> str:
    primary_contact = contacts[0] if contacts else {}
    wa_id = primary_contact.get("wa_id")
    if wa_id:
        return str(wa_id).strip()
    return str(msg.get("from") or "").strip()


# Hidrata el payload de instancia contexto desde.
# Se usa en el flujo de webhook WhatsApp del flujo principal multipropiedad para preparar datos, validaciones o decisiones previas.
# Recibe `memory_manager` como dependencias o servicios compartidos inyectados desde otras capas, y `memory_id`, `payload`, `resolve_property_table` como datos de contexto o entrada de la operación.
# No devuelve un valor relevante; deja preparado el estado o ejecuta la acción necesaria. Sin efectos secundarios relevantes.
def _hydrate_instance_context_from_payload(memory_manager, memory_id: str, payload: dict | None, resolve_property_table) -> None:
    if not (memory_manager and payload):
        return

    instance_id = payload.get("instance_id") or payload.get("instance_url")
    if instance_id:
        memory_manager.set_flag(memory_id, "instance_id", instance_id)
        memory_manager.set_flag(memory_id, "instance_hotel_code", instance_id)

    instance_url = payload.get("instance_url")
    if instance_url:
        memory_manager.set_flag(memory_id, "instance_url", instance_url)

    property_table = resolve_property_table(payload)
    if property_table:
        memory_manager.set_flag(memory_id, "property_table", property_table)

    for key in ("whatsapp_phone_id", "whatsapp_token", "whatsapp_verify_token"):
        value = payload.get(key)
        if value:
            memory_manager.set_flag(memory_id, key, value)


# Hidrata webhook contexto.
# Se usa en el flujo de webhook WhatsApp del flujo principal multipropiedad para preparar datos, validaciones o decisiones previas.
# Recibe `state` como dependencias o servicios compartidos inyectados desde otras capas, y `memory_id`, `sender_phone_id`, `normalized_instance_number`, `instance_phone_id` como datos de contexto o entrada de la operación.
# Devuelve un `tuple[str | None, str | None]` con el resultado de esta operación. Sin efectos secundarios relevantes.
def _hydrate_webhook_context(
    state,
    memory_id: str,
    sender_phone_id: str,
    normalized_instance_number: str,
    instance_phone_id: str | None,
) -> tuple[str | None, str | None]:
    from core.instance_context import hydrate_dynamic_context, fetch_instance_by_phone_id, _resolve_property_table

    memory_manager = getattr(state, "memory_manager", None)
    resolved_instance_phone_id = str(instance_phone_id or "").strip() or None
    instance_token = None

    if memory_manager:
        if normalized_instance_number:
            memory_manager.set_flag(memory_id, "instance_number", normalized_instance_number)
        if resolved_instance_phone_id:
            memory_manager.set_flag(memory_id, "whatsapp_phone_id", resolved_instance_phone_id)

    hydrate_dynamic_context(
        state=state,
        chat_id=memory_id,
        instance_number=normalized_instance_number,
        instance_phone_id=resolved_instance_phone_id,
    )

    has_instance_context = False
    if memory_manager:
        has_instance_context = bool(
            memory_manager.get_flag(memory_id, "instance_id")
            or memory_manager.get_flag(memory_id, "instance_hotel_code")
        )
    if resolved_instance_phone_id and memory_manager and not has_instance_context:
        payload = fetch_instance_by_phone_id(resolved_instance_phone_id)
        _hydrate_instance_context_from_payload(memory_manager, memory_id, payload, _resolve_property_table)

    if memory_manager:
        resolved_instance_phone_id = memory_manager.get_flag(memory_id, "whatsapp_phone_id") or resolved_instance_phone_id
        instance_token = memory_manager.get_flag(memory_id, "whatsapp_token")
        if sender_phone_id and resolved_instance_phone_id:
            memory_manager.set_flag(sender_phone_id, "whatsapp_phone_id", resolved_instance_phone_id)
        if sender_phone_id and instance_token:
            memory_manager.set_flag(sender_phone_id, "whatsapp_token", instance_token)

    return resolved_instance_phone_id, instance_token


# Extrae texto mensaje body.
# Se usa en el flujo de webhook WhatsApp del flujo principal multipropiedad para preparar datos, validaciones o decisiones previas.
# Recibe `msg`, `_` como entradas relevantes junto con el contexto inyectado en la firma.
# Devuelve un `str` con el resultado de esta operación. Sin efectos secundarios relevantes.
def _extract_text_message_body(msg: dict, _: str | None = None) -> str:
    return msg.get("text", {}).get("body", "")


# Extrae el texto de audio mensaje.
# Se usa en el flujo de webhook WhatsApp del flujo principal multipropiedad para preparar datos, validaciones o decisiones previas.
# Recibe `msg`, `instance_token` como entradas relevantes junto con el contexto inyectado en la firma.
# Devuelve un `str` con el resultado de esta operación. Sin efectos secundarios relevantes.
def _extract_audio_message_text(msg: dict, instance_token: str | None = None) -> str:
    from channels_wrapper.utils.media_utils import transcribe_audio

    media_id = msg.get("audio", {}).get("id")
    if not media_id:
        return ""

    log.info("🎧 Audio recibido (media_id=%s), iniciando transcripción...", media_id)
    whatsapp_token = instance_token or os.getenv("WHATSAPP_TOKEN", "")
    openai_key = os.getenv("OPENAI_API_KEY", "")
    text = transcribe_audio(media_id, whatsapp_token, openai_key)
    log.info("📝 Transcripción completada: %s", text)
    return text


# Extrae el texto de incoming WhatsApp.
# Se usa en el flujo de webhook WhatsApp del flujo principal multipropiedad para preparar datos, validaciones o decisiones previas.
# Recibe `msg`, `instance_token` como entradas relevantes junto con el contexto inyectado en la firma.
# Devuelve un `str` con el resultado de esta operación. Sin efectos secundarios relevantes.
def _extract_incoming_whatsapp_text(msg: dict, instance_token: str | None = None) -> str:
    message_type = str(msg.get("type") or "").strip().lower()
    message_handlers = {
        "text": _extract_text_message_body,
        "audio": _extract_audio_message_text,
    }
    handler = message_handlers.get(message_type)
    if handler is None:
        return ""
    return handler(msg, instance_token)


# Intenta recuperar property_id desde historial/reservas cuando llega nulo en webhook.
# Se usa en el flujo de webhook WhatsApp del flujo principal multipropiedad para preparar datos, validaciones o decisiones previas.
# Recibe `memory_id`, `sender_phone_id` como entradas relevantes junto con el contexto inyectado en la firma.
# Devuelve un `str | int | None` con el resultado de esta operación. Puede consultar o escribir en base de datos.
def _resolve_property_id_fallback(memory_id: str, sender_phone_id: str) -> str | int | None:
    """Intenta recuperar property_id desde historial/reservas cuando llega nulo en webhook."""
    try:
        from core.db import supabase
        from core.config import Settings
    except Exception:
        return None

    raw_memory = str(memory_id or "").strip()
    raw_sender_phone_id = str(sender_phone_id or "").strip()
    clean_sender_phone_id = re.sub(r"\D", "", raw_sender_phone_id).strip()

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

    # 2) Fallback por conversation_id (sender_phone_id limpio o memory_id)
    for cid in [raw_memory, raw_sender_phone_id, clean_sender_phone_id]:
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
    if clean_sender_phone_id:
        try:
            rows = (
                supabase.table(Settings.CHAT_RESERVATIONS_TABLE)
                .select("property_id")
                .eq("chat_id", clean_sender_phone_id)
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


# Registra los endpoints de webhook de WhatsApp en la app FastAPI.
# Se usa en el flujo de webhook WhatsApp del flujo principal multipropiedad para preparar datos, validaciones o decisiones previas.
# Recibe `app`, `state` como dependencias o servicios compartidos inyectados desde otras capas.
# Devuelve el resultado calculado para que el siguiente paso lo consuma. Puede emitir eventos socket, enviar mensajes o plantillas.
def register_whatsapp_routes(app, state):
    """Registra los endpoints de webhook de WhatsApp en la app FastAPI."""

    # Atiende el endpoint `GET /webhook` y coordina la operación pública de este módulo.
    # Se usa como punto de entrada HTTP dentro de webhook WhatsApp del flujo principal multipropiedad.
    # Recibe `request` desde path, query, body o dependencias HTTP según la firma del endpoint.
    # Devuelve la respuesta HTTP del endpoint o lanza errores de validación cuando corresponde. Sin efectos secundarios relevantes.
    @app.get("/webhook")
    async def verify_webhook(request: Request):
        verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "midemo")
        params = request.query_params
        if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == verify_token:
            return PlainTextResponse(params.get("hub.challenge"))
        return JSONResponse({"error": "Invalid verification token"}, status_code=403)

    # Webhook WhatsApp (Meta) + Buffer inteligente + Transcripción de audio (Whisper).
    # Se usa como punto de entrada HTTP dentro de webhook WhatsApp del flujo principal multipropiedad.
    # Recibe `request` desde path, query, body o dependencias HTTP según la firma del endpoint.
    # Devuelve la respuesta HTTP del endpoint o lanza errores de validación cuando corresponde. Puede emitir eventos socket, enviar mensajes o plantillas.
    @app.post("/webhook")
    async def whatsapp_webhook(request: Request):
        """Webhook WhatsApp (Meta) + Buffer inteligente + Transcripción de audio (Whisper)."""
        try:
            data = await request.json()
            value = data.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {})
            msg = value.get("messages", [{}])[0]
            metadata = value.get("metadata", {}) or {}
            contacts = value.get("contacts", []) or []
            profile = contacts[0].get("profile", {}) if contacts else {}
            client_name = profile.get("name")
            sender_phone_id = _resolve_sender_phone_id(contacts, msg)
            msg_id = msg.get("id")

            instance_number = metadata.get("display_phone_number") or ""
            normalized_instance_number = _clean_phone_like(instance_number)
            instance_phone_id = metadata.get("phone_number_id") or ""
            memory_id = (
                f"{normalized_instance_number}:{sender_phone_id}"
                if normalized_instance_number and sender_phone_id
                else sender_phone_id
            )
            instance_token = None
            if sender_phone_id and instance_number:
                try:
                    instance_phone_id, instance_token = _hydrate_webhook_context(
                        state=state,
                        memory_id=memory_id,
                        sender_phone_id=sender_phone_id,
                        normalized_instance_number=normalized_instance_number,
                        instance_phone_id=instance_phone_id,
                    )
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

            text = _extract_incoming_whatsapp_text(msg, instance_token=instance_token)

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
                # Limpiar cualquier property_id heredado del alias global del remitente para evitar mezcla.
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
            clean_sender_phone_id = _clean_phone_like(sender_phone_id)
            context_id = str(memory_id or sender_phone_id or "").strip()
            clean_chat_id = clean_sender_phone_id or context_id
            guest_lang = "es"
            guest_lang_confidence = 0.0
            try:
                prev_lang = (
                    state.memory_manager.get_flag(memory_id, "guest_lang")
                    or state.memory_manager.get_flag(clean_chat_id, "guest_lang")
                    or state.memory_manager.get_flag(sender_phone_id, "guest_lang")
                )
                detected_lang, detected_confidence = language_manager.detect_language_with_confidence(
                    text,
                    prev_lang=prev_lang,
                )
                guest_lang = (detected_lang or prev_lang or "es").strip().lower() or "es"
                try:
                    guest_lang_confidence = float(detected_confidence or 0.0)
                except Exception:
                    guest_lang_confidence = 0.0
                guest_lang_confidence = max(0.0, min(1.0, guest_lang_confidence))
                for lang_key in {memory_id, context_id, clean_chat_id, sender_phone_id}:
                    if not lang_key:
                        continue
                    state.memory_manager.set_flag(lang_key, "guest_lang", guest_lang)
                    state.memory_manager.set_flag(lang_key, "guest_lang_confidence", guest_lang_confidence)
            except Exception as exc:
                log.debug("No se pudo detectar/guardar guest_lang en webhook: %s", exc)
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
                clean_id=clean_sender_phone_id,
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
                        property_id = _resolve_property_id_fallback(memory_id, sender_phone_id)
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
                                "whatsapp_window": _build_active_whatsapp_window(),
                                "client_language": guest_lang,
                                "client_language_confidence": guest_lang_confidence,
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
                                        "client_language": guest_lang,
                                        "client_language_confidence": guest_lang_confidence,
                                        "client_phone": clean_chat_id,
                                        "whatsapp_phone_number": normalized_instance_number or None,
                                        "whatsapp_window": _build_active_whatsapp_window(),
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
                        rooms = [f"chat:{alias}" for alias in _chat_room_aliases(context_id, sender_phone_id, clean_chat_id)]
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
                                        "client_language": guest_lang,
                                        "client_language_confidence": guest_lang_confidence,
                                        "client_phone": clean_chat_id,
                                        "whatsapp_phone_number": normalized_instance_number or None,
                                        "whatsapp_window": _build_active_whatsapp_window(now_iso),
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
                        incoming_message_payload = {
                            "chat_id": clean_chat_id,
                            "guest_chat_id": clean_chat_id,
                            "context_id": context_id,
                            "property_id": property_id,
                            "channel": "whatsapp",
                            "sender": "guest",
                            "message": text,
                            "created_at": now_iso,
                            "whatsapp_window": _build_active_whatsapp_window(now_iso),
                            "client_language": guest_lang,
                            "client_language_confidence": guest_lang_confidence,
                        }
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
                                "property_id": property_id,
                                "channel": "whatsapp",
                                "last_message": text,
                                "last_message_at": now_iso,
                                "whatsapp_window": _build_active_whatsapp_window(now_iso),
                            },
                            rooms=rooms,
                        )
                except Exception as exc:
                    log.warning("No se pudo emitir mensaje entrante con BookAI apagado: %s", exc)
                log.info(
                    "🤫 BookAI desactivado para %s (property_id=%s); mensaje no encolado.",
                    clean_sender_phone_id,
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
                        current_property_id = _resolve_property_id_fallback(memory_id, sender_phone_id)
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
                                "client_language": guest_lang,
                                "client_language_confidence": guest_lang_confidence,
                                "client_phone": clean_chat_id,
                                "whatsapp_phone_number": normalized_instance_number or None,
                                "whatsapp_window": _build_active_whatsapp_window(),
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
                rooms = [f"chat:{alias}" for alias in _chat_room_aliases(context_id, sender_phone_id, clean_chat_id)]
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
                                "client_language": guest_lang,
                                "client_language_confidence": guest_lang_confidence,
                                "client_phone": clean_chat_id,
                                "whatsapp_phone_number": normalized_instance_number or None,
                                "whatsapp_window": _build_active_whatsapp_window(now_iso),
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
                incoming_message_payload = {
                    "chat_id": clean_chat_id,
                    "guest_chat_id": clean_chat_id,
                    "context_id": context_id,
                    "property_id": property_id,
                    "channel": "whatsapp",
                    "sender": "guest",
                    "message": text,
                    "created_at": now_iso,
                    "whatsapp_window": _build_active_whatsapp_window(now_iso),
                    "client_language": guest_lang,
                    "client_language_confidence": guest_lang_confidence,
                }
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
                        "property_id": property_id,
                        "channel": "whatsapp",
                        "last_message": text,
                        "last_message_at": now_iso,
                        "whatsapp_window": _build_active_whatsapp_window(now_iso),
                    },
                    rooms=rooms,
                )

            # Procesa el buffered.
            # Se invoca dentro de `whatsapp_webhook` para encapsular una parte local de webhook WhatsApp del flujo principal multipropiedad.
            # Recibe `cid`, `combined_text`, `version` como entradas relevantes junto con el contexto inyectado en la firma.
            # No devuelve un valor relevante; deja preparado el estado o ejecuta la acción necesaria. Puede enviar mensajes o plantillas.
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

                # Envía to channel.
                # Se invoca dentro de `_process_buffered` para encapsular una parte local de webhook WhatsApp del flujo principal multipropiedad.
                # Recibe `uid`, `txt` como entradas relevantes junto con el contexto inyectado en la firma.
                # Produce la acción solicitada y prioriza el efecto lateral frente a un retorno complejo. Puede enviar mensajes o plantillas.
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
                    clean_id=clean_sender_phone_id,
                    property_id=property_id,
                )
                if final_bookai_enabled is False:
                    log.info(
                        "🤫 BookAI desactivado antes del envio para %s (property_id=%s); respuesta descartada.",
                        clean_sender_phone_id,
                        property_id,
                    )
                    return

                await send_fragmented_async(send_to_channel, sender_phone_id, resp)

            await state.buffer_manager.add_message(memory_id, text, _process_buffered)

            return JSONResponse({"status": "queued"})

        except Exception as exc:
            log.error("❌ Error en webhook WhatsApp: %s", exc, exc_info=True)
            return JSONResponse({"status": "error"}, status_code=500)
