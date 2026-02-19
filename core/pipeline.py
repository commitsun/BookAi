"""Pipeline principal para procesar mensajes de usuarios."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from core.config import ModelConfig, ModelTier
from core.language_manager import language_manager
from core.main_agent import create_main_agent
from core.instance_context import hydrate_dynamic_context

log = logging.getLogger("Pipeline")
SUPER_OFFER_FLAG = "super_offer_pending"


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _humanize_offer_type(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "cortesía"
    return raw.replace("_", " ").strip()


def _humanize_missing_fields(fields: list[str] | None) -> str:
    mapping = {
        "schedule": "horario",
        "location": "ubicación",
        "booking_method": "método de reserva",
        "conditions": "condiciones",
        "price": "precio",
        "duration": "duración",
    }
    normalized = []
    for field in fields or []:
        key = str(field or "").strip().lower()
        if not key:
            continue
        normalized.append(mapping.get(key, key.replace("_", " ")))
    if not normalized:
        return "detalles operativos"
    return ", ".join(dict.fromkeys(normalized))


def _load_active_super_offer(memory_manager: Any, *keys: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    if not memory_manager:
        return None, None
    now = datetime.utcnow()
    for key in [str(k).strip() for k in keys if str(k or "").strip()]:
        try:
            payload = memory_manager.get_flag(key, SUPER_OFFER_FLAG)
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            continue
        expires_at_raw = str(payload.get("expires_at") or "").strip()
        if expires_at_raw:
            try:
                expires_at = datetime.fromisoformat(expires_at_raw.replace("Z", ""))
                if expires_at <= now:
                    memory_manager.clear_flag(key, SUPER_OFFER_FLAG)
                    continue
            except Exception:
                memory_manager.clear_flag(key, SUPER_OFFER_FLAG)
                continue
        if not payload.get("details_missing", True):
            continue
        return payload, key
    return None, None


async def _classify_guest_offer_intent(
    llm: Any,
    *,
    user_message: str,
    pending_offer: dict[str, Any],
) -> dict[str, Any]:
    text = (user_message or "").strip()
    if not llm or not text or not pending_offer:
        return {"intent": "other", "confidence": 0.0}
    prompt = (
        "Clasifica la intención del huésped respecto a una oferta pendiente del hotel.\n"
        "Devuelve solo JSON con este esquema exacto:\n"
        "{"
        "\"intent\":\"ask_offer_details|other\","
        "\"requested_fields\":[\"schedule|location|booking_method|conditions|price|duration\"],"
        "\"confidence\":0.0"
        "}\n"
        "Usa semántica contextual, no keywords.\n\n"
        f"Oferta pendiente: {json.dumps(pending_offer, ensure_ascii=False)}\n"
        f"Mensaje huésped: {text}"
    )
    try:
        response = await llm.ainvoke(
            [
                {"role": "system", "content": "Eres un clasificador semántico de intención conversacional."},
                {"role": "user", "content": prompt},
            ]
        )
        data = _extract_json_object((getattr(response, "content", None) or "").strip()) or {}
    except Exception:
        data = {}
    intent = str(data.get("intent") or "other").strip().lower()
    if intent not in {"ask_offer_details", "other"}:
        intent = "other"
    req = data.get("requested_fields")
    if not isinstance(req, list):
        req = []
    req = [str(x).strip() for x in req if str(x).strip()]
    return {
        "intent": intent,
        "requested_fields": req,
        "confidence": _safe_float(data.get("confidence"), 0.0),
    }


async def _check_offer_response_consistency(
    llm: Any,
    *,
    user_message: str,
    pending_offer: dict[str, Any],
    agent_response: str,
) -> dict[str, Any]:
    if not llm or not pending_offer or not agent_response:
        return {"is_consistent": True, "confidence": 1.0, "reason": ""}
    prompt = (
        "Valida consistencia de respuesta frente a una oferta hotelera pendiente sin detalles confirmados.\n"
        "Devuelve solo JSON con este esquema exacto:\n"
        "{"
        "\"is_consistent\":true|false,"
        "\"reason\":\"string\","
        "\"confidence\":0.0"
        "}\n"
        "Marca is_consistent=false si la respuesta inventa o mezcla servicios no confirmados para esa oferta.\n\n"
        f"Oferta pendiente: {json.dumps(pending_offer, ensure_ascii=False)}\n"
        f"Mensaje huésped: {user_message}\n"
        f"Respuesta propuesta: {agent_response}"
    )
    try:
        response = await llm.ainvoke(
            [
                {"role": "system", "content": "Eres un guardrail de consistencia para operaciones hoteleras."},
                {"role": "user", "content": prompt},
            ]
        )
        data = _extract_json_object((getattr(response, "content", None) or "").strip()) or {}
    except Exception:
        data = {}
    return {
        "is_consistent": bool(data.get("is_consistent", True)),
        "reason": str(data.get("reason") or "").strip(),
        "confidence": _safe_float(data.get("confidence"), 0.0),
    }


async def process_user_message(
    user_message: str,
    chat_id: str,
    state,
    hotel_name: str = "Hotel",
    channel: str = "whatsapp",
    instance_number: str | None = None,
    memory_id: str | None = None,
) -> str | None:
    """
    Flujo principal:
      1. Supervisor Input
      2. Main Agent
      3. Supervisor Output
      4. Escalación → InternoAgent
    """
    try:
        mem_id = memory_id or chat_id
        log.info("📨 Nuevo mensaje de %s: %s", chat_id, user_message[:150])
        guest_lang = "es"
        if state.memory_manager:
            state.memory_manager.set_flag(mem_id, "default_channel", channel)
            try:
                prev_lang = state.memory_manager.get_flag(mem_id, "guest_lang")
                detected_lang = language_manager.detect_language(user_message, prev_lang=prev_lang)
                guest_lang = (detected_lang or prev_lang or "es").strip().lower() or "es"
                state.memory_manager.set_flag(mem_id, "guest_lang", guest_lang)
            except Exception as exc:
                log.debug("No se pudo detectar/guardar guest_lang en pipeline: %s", exc)

        def _ensure_guest_language(text: str) -> str:
            if not text:
                return text
            if guest_lang == "es":
                return text
            try:
                return language_manager.ensure_language(text, guest_lang)
            except Exception:
                return text

        clean_id = re.sub(r"\D", "", str(chat_id or "")).strip() or str(chat_id or "")
        bookai_flags = getattr(state, "tracking", {}).get("bookai_enabled", {})
        if isinstance(bookai_flags, dict) and bookai_flags.get(clean_id) is False:
            try:
                state.memory_manager.save(mem_id, "user", user_message)
            except Exception as exc:
                log.warning("No se pudo guardar mensaje con BookAI apagado: %s", exc)
            log.info("🤫 BookAI desactivado para %s; se omite respuesta automática.", clean_id)
            return None

        input_validation = await state.supervisor_input.validate(user_message)
        estado_in = input_validation.get("estado", "Aprobado")
        motivo_in = input_validation.get("motivo", "")

        if estado_in.lower() not in ["aprobado", "ok", "aceptable"]:
            log.warning("🚨 Mensaje rechazado por Supervisor Input: %s", motivo_in)
            await state.interno_agent.escalate(
                guest_chat_id=chat_id,
                guest_message=user_message,
                escalation_type="inappropriate",
                reason=motivo_in,
                context="Rechazado por Supervisor Input",
            )
            return None

        try:
            history = state.memory_manager.get_memory_as_messages(mem_id)
        except Exception as exc:
            log.warning("⚠️ No se pudo obtener memoria: %s", exc)
            history = []
        pending_offer, pending_offer_key = _load_active_super_offer(state.memory_manager, mem_id, chat_id)
        semantic_llm = None
        if pending_offer:
            try:
                semantic_llm = ModelConfig.get_llm(ModelTier.INTERNAL)
            except Exception:
                semantic_llm = None

        # Evitar duplicados: si el huésped confirma y ya se envió un resumen reciente con localizador.
        response_raw = None
        forced_offer_escalation = False
        try:
            recent_summary = False
            raw_hist = state.memory_manager.get_memory(mem_id, limit=8) if state.memory_manager else []
            for msg in raw_hist or []:
                role = (msg.get("role") or "").lower()
                if role not in {"assistant", "bookai"}:
                    continue
                content = str(msg.get("content") or "")
                if re.search(r"Localizador\\s*[:#]?\\s*[A-Za-z0-9/\\-]{4,}", content, re.IGNORECASE):
                    recent_summary = True
                    break

            confirmation = re.fullmatch(
                r"\\s*(vale|ok|okay|perfecto|sí|si|de acuerdo|correcto|esa me va bien|me va bien|todo bien|confirmo|confirmada|est[aá] bien)\\s*[.!]*\\s*",
                user_message,
                re.IGNORECASE,
            )
            if recent_summary and confirmation:
                response_raw = _ensure_guest_language("¡Perfecto! Queda confirmada. Si necesitas algo más, dímelo.")
                try:
                    state.memory_manager.save(
                        mem_id,
                        role="assistant",
                        content=response_raw,
                        channel=channel,
                    )
                except Exception as exc:
                    log.warning("No se pudo guardar respuesta corta de confirmación: %s", exc)
        except Exception as exc:
            log.debug("No se pudo aplicar regla anti-duplicados: %s", exc)

        # Respuesta rápida: si el huésped pide el localizador y ya está en historial.
        localizador = None
        if state.memory_manager:
            try:
                localizador = state.memory_manager.get_flag(mem_id, "reservation_locator") or localizador
                raw_hist = state.memory_manager.get_memory(mem_id, limit=30) or []
                for msg in raw_hist:
                    content = (msg.get("content") or "")
                    if not isinstance(content, str):
                        continue
                    match = re.search(
                        r"(localizador)\\s*[:#]?\\s*([A-Za-z0-9/\\-]{4,})",
                        content,
                        re.IGNORECASE,
                    )
                    if match:
                        localizador = match.group(2)
                        continue
                    match = re.search(r"(folio(?:_id)?)\\s*[:#]?\\s*([A-Za-z0-9]{4,})", content, re.IGNORECASE)
                    if not match:
                        match = re.search(r"reserva\\s*[:#]?\\s*([A-Za-z0-9]{4,})", content, re.IGNORECASE)
                    if match:
                        candidate = match.group(2) if match.lastindex and match.lastindex >= 2 else match.group(1)
                        if re.fullmatch(r"(?=.*\\d)[A-Za-z0-9]{4,}", candidate or ""):
                            localizador = candidate
            except Exception as exc:
                log.debug("No se pudo extraer localizador de historial: %s", exc)

        asks_localizador = bool(re.search(r"localizador|folio|n[uú]mero de reserva", user_message, re.IGNORECASE))
        wants_details = bool(
            re.search(
                r"(mirame|mu[eé]strame|ver|consultar|detalles|m[aá]s info|informaci[oó]n|sobre esta)",
                user_message,
                re.IGNORECASE,
            )
        )
        if not response_raw and asks_localizador and localizador and not wants_details:
            response_raw = _ensure_guest_language(f"El localizador de tu reserva es {localizador}.")
            try:
                state.memory_manager.save(
                    mem_id,
                    role="assistant",
                    content=response_raw,
                    channel=channel,
                )
            except Exception as exc:
                log.warning("No se pudo guardar respuesta rápida de localizador: %s", exc)
        # response_raw ya puede venir de regla anti-duplicados o localizador rápido

        async def send_inciso_callback(msg: str):
            try:
                await state.channel_manager.send_message(
                    chat_id,
                    msg,
                    channel=channel,
                    context_id=mem_id,
                )
            except Exception as exc:
                log.error("❌ Error enviando inciso: %s", exc)

        try:
            hydrate_dynamic_context(
                state=state,
                chat_id=mem_id,
                instance_number=instance_number,
            )
        except Exception as exc:
            log.warning("No se pudo hidratar contexto dinamico: %s", exc)

        if not response_raw and pending_offer:
            intent_eval = await _classify_guest_offer_intent(
                semantic_llm,
                user_message=user_message,
                pending_offer=pending_offer,
            )
            if (
                intent_eval.get("intent") == "ask_offer_details"
                and _safe_float(intent_eval.get("confidence"), 0.0) >= 0.65
            ):
                requested = ", ".join(intent_eval.get("requested_fields") or []) or "details"
                offer_type = _humanize_offer_type(pending_offer.get("type"))
                missing_human = _humanize_missing_fields(pending_offer.get("missing_fields"))
                original_text = str(pending_offer.get("original_text") or "").strip()
                await state.interno_agent.escalate(
                    guest_chat_id=chat_id,
                    guest_message=user_message,
                    escalation_type="offer_details_missing",
                    reason=(
                        f"Oferta pendiente sin datos confirmados: {offer_type}. "
                        f"Faltan: {missing_human}."
                    ),
                    context=(
                        f"offer_key={pending_offer_key}\n"
                        f"offer_type={offer_type}\n"
                        f"missing_fields={missing_human}\n"
                        f"requested_fields={requested}\n"
                        f"guest_question={user_message}\n"
                        f"original_offer_text={original_text}\n"
                        f"pending_offer={json.dumps(pending_offer, ensure_ascii=False)}"
                    ),
                )
                response_raw = _ensure_guest_language(
                    "Gracias por escribirnos. Estamos validando con recepción el horario, lugar y condiciones "
                    "de esta cortesía para confirmártelo en breve."
                )
                forced_offer_escalation = True
                try:
                    state.memory_manager.save(mem_id, role="assistant", content=response_raw, channel=channel)
                except Exception as exc:
                    log.warning("No se pudo guardar respuesta de escalación por oferta pendiente: %s", exc)

        if not response_raw:
            main_agent = create_main_agent(
                memory_manager=state.memory_manager,
                send_callback=send_inciso_callback,
                interno_agent=state.interno_agent,
            )

            response_raw = await main_agent.ainvoke(
                user_input=user_message,
                chat_id=mem_id,
                hotel_name=hotel_name,
                chat_history=history,
            )

        if not response_raw:
            await state.interno_agent.escalate(
                guest_chat_id=chat_id,
                guest_message=user_message,
                escalation_type="info_not_found",
                reason="Main Agent no devolvió respuesta",
                context="Respuesta vacía o nula",
            )
            return None

        response_raw = response_raw.strip()
        # Fuerza el idioma final de salida al idioma detectado del último mensaje del huésped.
        # Evita respuestas en español cuando el huésped escribe en pt/fr/de, etc.
        response_raw = _ensure_guest_language(response_raw)
        if pending_offer and response_raw and not forced_offer_escalation:
            consistency = await _check_offer_response_consistency(
                semantic_llm,
                user_message=user_message,
                pending_offer=pending_offer,
                agent_response=response_raw,
            )
            if (
                not consistency.get("is_consistent", True)
                and _safe_float(consistency.get("confidence"), 0.0) >= 0.70
            ):
                offer_type = _humanize_offer_type(pending_offer.get("type"))
                missing_human = _humanize_missing_fields(pending_offer.get("missing_fields"))
                await state.interno_agent.escalate(
                    guest_chat_id=chat_id,
                    guest_message=user_message,
                    escalation_type="offer_consistency_guard",
                    reason=(
                        consistency.get("reason")
                        or f"Respuesta potencialmente inconsistente con la oferta pendiente ({offer_type})."
                    ),
                    context=(
                        f"offer_key={pending_offer_key}\n"
                        f"offer_type={offer_type}\n"
                        f"missing_fields={missing_human}\n"
                        f"pending_offer={json.dumps(pending_offer, ensure_ascii=False)}\n"
                        f"proposed_response={response_raw}"
                    ),
                )
                response_raw = _ensure_guest_language(
                    "Estamos revisando con recepción los detalles exactos de esta cortesía para darte una "
                    "confirmación correcta en breve."
                )
                try:
                    state.memory_manager.save(mem_id, role="assistant", content=response_raw, channel=channel)
                except Exception as exc:
                    log.warning("No se pudo guardar fallback por guardrail de oferta: %s", exc)
        log.info("🤖 Respuesta del MainAgent: %s", response_raw[:300])

        output_validation = await state.supervisor_output.validate(
            user_input=user_message,
            agent_response=response_raw,
        )
        estado_out = (output_validation.get("estado", "Aprobado") or "").lower()
        motivo_out = output_validation.get("motivo", "")

        if "aprobado" not in estado_out:
            log.warning("🚨 Respuesta rechazada por Supervisor Output: %s", motivo_out)

            hist_text = ""
            try:
                raw_hist = state.memory_manager.get_memory(mem_id, limit=6)
                if raw_hist:
                    lines = []
                    for m in raw_hist:
                        role = m.get("role")
                        if role == "guest":
                            prefix = "Huésped"
                        elif role == "user":
                            prefix = "Hotel"
                        elif role in {"assistant", "bookai"}:
                            prefix = "BookAI"
                        else:
                            prefix = "BookAI"
                        lines.append(f"{prefix}: {m.get('content','')}")
                    hist_text = "\n".join(lines)
            except Exception as exc:
                log.warning("⚠️ No se pudo recuperar historial para escalación: %s", exc)

            context_full = (
                f"Respuesta rechazada: {response_raw[:150]}\n\n"
                f"🧠 Historial reciente:\n{hist_text}"
            )

            await state.interno_agent.escalate(
                guest_chat_id=chat_id,
                guest_message=user_message,
                escalation_type="bad_response",
                reason=motivo_out,
                context=context_full,
            )
            return None

        # Emitimos evento en tiempo real para respuestas de IA.
        try:
            socket_mgr = getattr(state, "socket_manager", None)
            if socket_mgr and getattr(socket_mgr, "enabled", False):
                prop_id = None
                if state.memory_manager:
                    try:
                        prop_id = state.memory_manager.get_flag(mem_id, "property_id")
                    except Exception:
                        prop_id = None
                target_chat_room = mem_id or chat_id
                rooms = [f"chat:{target_chat_room}"]
                if prop_id is not None:
                    rooms.append(f"property:{prop_id}")
                if channel:
                    rooms.append(f"channel:{channel}")
                now_iso = datetime.now(timezone.utc).isoformat()
                await socket_mgr.emit(
                    "chat.message.created",
                    {
                        "rooms": rooms,
                        "chat_id": str(mem_id or chat_id),
                        "guest_chat_id": str(chat_id),
                        "context_id": str(mem_id or chat_id),
                        "property_id": prop_id,
                        "channel": channel,
                        "sender": "bookai",
                        "message": response_raw,
                        "created_at": now_iso,
                    },
                    rooms=rooms,
                )
                await socket_mgr.emit(
                    "chat.updated",
                    {
                        "rooms": rooms,
                        "chat_id": str(mem_id or chat_id),
                        "guest_chat_id": str(chat_id),
                        "context_id": str(mem_id or chat_id),
                        "property_id": prop_id,
                        "channel": channel,
                        "last_message": response_raw,
                        "last_message_at": now_iso,
                    },
                    rooms=rooms,
                )
        except Exception as exc:
            log.warning("No se pudo emitir respuesta IA por socket: %s", exc)

        return response_raw

    except Exception as exc:
        log.error("💥 Error crítico en pipeline: %s", exc, exc_info=True)
        await state.interno_agent.escalate(
            guest_chat_id=chat_id,
            guest_message=user_message,
            escalation_type="info_not_found",
            reason=f"Error crítico: {str(exc)}",
            context="Excepción general en process_user_message",
        )
        return None
