"""Pipeline principal para procesar mensajes de usuarios."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from core.config import ModelConfig, ModelTier, Settings
from core.language_manager import language_manager
from core.main_agent import NO_GUEST_REPLY, create_main_agent
from core.instance_context import hydrate_dynamic_context
from core.escalation_db import get_latest_pending_escalation

log = logging.getLogger("Pipeline")
SUPER_OFFER_FLAG = "super_offer_pending"
_HUMAN_ESCALATION_COOLDOWN_MIN = 15


def _message_requests_human_intervention(text: str) -> bool:
    raw = (text or "").strip().lower()
    if not raw:
        return False
    patterns = [
        r"\b(hablar|consultar|informar|pasar|derivar)\b.{0,30}\b(encargad[oa]|gerente|recepci[oó]n|humano|persona)\b",
        r"\b(can you|could you|please)\b.{0,40}\b(ask|check|consult|inform)\b.{0,30}\b(manager|reception|staff|human)\b",
        r"\b(i need|i want)\b.{0,40}\b(speak|talk)\b.{0,20}\b(manager|reception|human|person)\b",
    ]
    return any(re.search(p, raw, re.IGNORECASE) for p in patterns)


def _response_promises_human_escalation(text: str) -> bool:
    raw = (text or "").strip().lower()
    if not raw:
        return False
    patterns = [
        r"\b(d[ée]jame|un momento|espera|aguarda)\b.{0,60}\b(consult|pregunt|verific|confirm)\w*\b.{0,40}\b(encargad[oa]|gerente|recepci[oó]n|equipo|personal)\b",
        r"\b(he trasladado|voy a trasladar|escalar[eé]|derivar[eé])\b.{0,50}\b(encargad[oa]|gerente|recepci[oó]n|equipo|personal|humano)\b",
        r"\b(i'?ll|let me|one moment|hold on)\b.{0,60}\b(check|ask|confirm|consult)\b.{0,40}\b(manager|reception|staff|team|human)\b",
    ]
    return any(re.search(p, raw, re.IGNORECASE) for p in patterns)


async def _llm_response_promises_human_escalation(
    llm: Any,
    *,
    user_message: str,
    assistant_response: str,
) -> bool:
    if not llm:
        return _response_promises_human_escalation(assistant_response)
    try:
        system = (
            "Eres un clasificador binario.\n"
            "Debes decidir si la respuesta del asistente PROMETE que va a consultar/escalar a una persona del hotel "
            "(encargado, recepción, gerente, equipo humano).\n"
            "Responde SOLO JSON: {\"promises_human_escalation\": true|false, \"confidence\": 0..1}."
        )
        user = (
            f"Mensaje del huésped:\n{user_message}\n\n"
            f"Respuesta del asistente:\n{assistant_response}\n\n"
            "Si la respuesta indica explícita o implícitamente 'consultaré/preguntaré/verificaré con el encargado/equipo', "
            "entonces true."
        )
        raw = await llm.ainvoke(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
        )
        content = (getattr(raw, "content", None) or str(raw or "")).strip()
        data = _extract_json_object(content) or {}
        promised = bool(data.get("promises_human_escalation", False))
        confidence = _safe_float(data.get("confidence"), 0.0)
        if promised and confidence >= 0.45:
            return True
        if promised and confidence <= 0.0:
            return True
        return False
    except Exception:
        return _response_promises_human_escalation(assistant_response)


def _has_recent_pending_escalation(mem_id: str, state) -> bool:
    if not mem_id:
        return False
    try:
        property_id = None
        mm = getattr(state, "memory_manager", None)
        if mm:
            property_id = mm.get_flag(mem_id, "property_id")
        latest = get_latest_pending_escalation(mem_id, property_id=property_id)
        if not latest:
            return False
        ts_raw = str(latest.get("timestamp") or "").strip()
        if not ts_raw:
            return True
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
        return age_min <= _HUMAN_ESCALATION_COOLDOWN_MIN
    except Exception:
        return False


def _sanitize_guest_facing_response(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return raw

    # Si el modelo devolvió bloque tipo "Response to the user:", usa solo esa parte.
    marker_match = re.search(
        r"(response to the user|respuesta (?:al|para el) (?:hu[eé]sped|usuario)|respuesta final)\s*:\s*",
        raw,
        re.IGNORECASE,
    )
    if marker_match:
        tail = raw[marker_match.end() :].strip()
        if tail:
            return tail

    meta_line_patterns = [
        r"^\s*this is a .*user message.*$",
        r"^\s*the inquiry is about.*$",
        r"^\s*therefore,? i must.*$",
        r"^\s*reasoning\s*:.*$",
        r"^\s*analysis\s*:.*$",
        r"^\s*thought\s*:.*$",
        r"^\s*mensaje del usuario\s*:.*$",
        r"^\s*la consulta.*$",
        r"^\s*por lo tanto.*$",
        r"^\s*respuesta (?:al|para el) (?:hu[eé]sped|usuario)\s*:.*$",
    ]

    cleaned_lines = []
    for line in raw.splitlines():
        ln = line.strip()
        if not ln:
            continue
        if any(re.search(p, ln, re.IGNORECASE) for p in meta_line_patterns):
            continue
        cleaned_lines.append(ln)

    if cleaned_lines:
        return "\n".join(cleaned_lines).strip()
    return raw


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


def _as_bool_or_none(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return None


def _resolve_bookai_enabled(
    state: Any,
    *,
    chat_id: str,
    mem_id: str,
    clean_id: str,
    property_id: Any = None,
) -> Optional[bool]:
    if Settings.BOOKAI_GLOBAL_ENABLED is False:
        return False

    bookai_flags = getattr(state, "tracking", {}).get("bookai_enabled", {})
    if not isinstance(bookai_flags, dict):
        return None

    memory = getattr(state, "memory_manager", None)
    property_candidates: list[str] = []
    if property_id is not None and str(property_id).strip():
        property_candidates.append(str(property_id).strip())
    if memory:
        for key in (mem_id, chat_id, clean_id):
            if not key:
                continue
            try:
                prop = memory.get_flag(key, "property_id")
            except Exception:
                prop = None
            if prop is not None and str(prop).strip():
                property_candidates.append(str(prop).strip())
        try:
            for hint_key in (mem_id, chat_id, clean_id):
                if not hint_key:
                    continue
                hint = memory.get_last_property_id_hint(hint_key)
                if hint is not None and str(hint).strip():
                    property_candidates.append(str(hint).strip())
        except Exception:
            pass

    seen_props = set()
    for prop in property_candidates:
        if prop in seen_props:
            continue
        seen_props.add(prop)
        candidate_value = _as_bool_or_none(bookai_flags.get(f"{clean_id}:{prop}"))
        if candidate_value is not None:
            return candidate_value

    # Fallback útil cuando aún no hay property_id en memoria:
    # si existe una única configuración por propiedad para este chat, úsala.
    prefix = f"{clean_id}:"
    prefixed_values: list[bool] = []
    for key, raw_val in bookai_flags.items():
        if not str(key).startswith(prefix):
            continue
        parsed = _as_bool_or_none(raw_val)
        if parsed is None:
            continue
        prefixed_values.append(parsed)
    if len(prefixed_values) == 1:
        return prefixed_values[0]
    if len(prefixed_values) > 1 and all(v == prefixed_values[0] for v in prefixed_values):
        return prefixed_values[0]

    return _as_bool_or_none(bookai_flags.get(clean_id))


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


def _is_message_related_to_pending_offer(user_message: str, pending_offer: dict[str, Any]) -> bool:
    text = (user_message or "").strip().lower()
    if not text:
        return False
    # Permite deícticos cortos para no romper seguimientos naturales ("y eso?", "ok con eso").
    if len(text) <= 20 and re.search(r"\b(eso|esto|aquello|lo|la)\b", text):
        return True

    offer_context = " ".join(
        [
            str(pending_offer.get("type") or ""),
            str(pending_offer.get("original_text") or ""),
        ]
    ).lower()
    if not offer_context.strip():
        return False

    stopwords = {
        "de", "la", "el", "y", "en", "que", "si", "es", "un", "una", "por", "para",
        "con", "del", "al", "los", "las", "me", "mi", "tu", "su", "hay", "quiero",
        "saber", "sobre", "tambien", "tb",
    }

    def _tokens(value: str) -> set[str]:
        parts = re.findall(r"[a-záéíóúñü]{3,}", value, flags=re.IGNORECASE)
        return {p.lower() for p in parts if p.lower() not in stopwords}

    msg_tokens = _tokens(text)
    offer_tokens = _tokens(offer_context)
    if not msg_tokens or not offer_tokens:
        return False
    return bool(msg_tokens.intersection(offer_tokens))


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
    property_id: str | int | None = None,
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
        escalation_chat_id = mem_id or chat_id
        guest_message_persisted = False
        main_agent_invoked = False
        log.info("📨 Nuevo mensaje de %s: %s", chat_id, user_message[:150])
        guest_lang = "es"
        if state.memory_manager:
            if property_id is not None:
                # Asegura que los saves posteriores persistan property_id en chat_history.
                for key in [mem_id, chat_id]:
                    try:
                        state.memory_manager.set_flag(key, "property_id", property_id)
                    except Exception:
                        pass
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

        def _persist_guest_message() -> None:
            nonlocal guest_message_persisted
            if guest_message_persisted:
                return
            try:
                state.memory_manager.save(
                    mem_id,
                    role="user",
                    content=user_message,
                    channel=channel,
                    original_chat_id=mem_id,
                    skip_recent_duplicate_guard=True,
                )
                guest_message_persisted = True
            except Exception as exc:
                log.warning("No se pudo persistir mensaje del huésped en pipeline: %s", exc)

        clean_id = re.sub(r"\D", "", str(chat_id or "")).strip() or str(chat_id or "")
        bookai_enabled = _resolve_bookai_enabled(
            state,
            chat_id=str(chat_id or ""),
            mem_id=str(mem_id or ""),
            clean_id=clean_id,
            property_id=property_id,
        )
        if bookai_enabled is False:
            try:
                state.memory_manager.save(mem_id, "user", user_message)
                guest_message_persisted = True
            except Exception as exc:
                log.warning("No se pudo guardar mensaje con BookAI apagado: %s", exc)
            log.info("🤫 BookAI desactivado para %s; se omite respuesta automática.", clean_id)
            return None

        input_validation = await state.supervisor_input.validate(user_message)
        estado_in = input_validation.get("estado", "Aprobado")
        motivo_in = input_validation.get("motivo", "")

        if estado_in.lower() not in ["aprobado", "ok", "aceptable"]:
            _persist_guest_message()
            log.warning("🚨 Mensaje rechazado por Supervisor Input: %s", motivo_in)
            await state.interno_agent.escalate(
                guest_chat_id=escalation_chat_id,
                guest_message=user_message,
                escalation_type="inappropriate",
                reason=motivo_in,
                context="Rechazado por Supervisor Input",
                property_id=property_id,
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
            if not _is_message_related_to_pending_offer(user_message, pending_offer):
                log.info("OfferGuard: mensaje no relacionado con oferta pendiente, se ignora pending_offer en este turno.")
                pending_offer = None
                pending_offer_key = None
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
                    _persist_guest_message()
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
                _persist_guest_message()
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
                    guest_chat_id=escalation_chat_id,
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
                    property_id=property_id,
                )
                response_raw = _ensure_guest_language(
                    "Gracias por escribirnos. Estamos validando con recepción el horario, lugar y condiciones "
                    "de esta cortesía para confirmártelo en breve."
                )
                forced_offer_escalation = True
                try:
                    _persist_guest_message()
                    state.memory_manager.save(mem_id, role="assistant", content=response_raw, channel=channel)
                except Exception as exc:
                    log.warning("No se pudo guardar respuesta de escalación por oferta pendiente: %s", exc)

        if not response_raw and _message_requests_human_intervention(user_message):
            if not _has_recent_pending_escalation(mem_id, state):
                await state.interno_agent.escalate(
                    guest_chat_id=escalation_chat_id,
                    guest_message=user_message,
                    escalation_type="info_not_found",
                    reason="El huésped solicita consulta/intervención de personal humano.",
                    context="Escalación forzada por petición explícita de manager/recepción/humano.",
                    property_id=property_id,
                )
            response_raw = _ensure_guest_language(
                "He trasladado tu consulta al encargado del hotel y te informaré en cuanto tenga respuesta."
            )
            try:
                _persist_guest_message()
                state.memory_manager.save(mem_id, role="assistant", content=response_raw, channel=channel)
            except Exception as exc:
                log.warning("No se pudo guardar respuesta de escalación forzada: %s", exc)

        if not response_raw:
            main_agent = create_main_agent(
                memory_manager=state.memory_manager,
                send_callback=send_inciso_callback,
                interno_agent=state.interno_agent,
            )
            main_agent_invoked = True

            response_raw = await main_agent.ainvoke(
                user_input=user_message,
                chat_id=mem_id,
                hotel_name=hotel_name,
                chat_history=history,
            )
            if response_raw == NO_GUEST_REPLY:
                log.info("🔇 Respuesta silenciosa (solo interno) para chat_id=%s", mem_id)
                return None

        if not response_raw:
            await state.interno_agent.escalate(
                guest_chat_id=escalation_chat_id,
                guest_message=user_message,
                escalation_type="info_not_found",
                reason="Main Agent no devolvió respuesta",
                context="Respuesta vacía o nula",
                property_id=property_id,
            )
            return None

        if not main_agent_invoked:
            _persist_guest_message()

        response_raw = _sanitize_guest_facing_response(response_raw.strip())
        # Fuerza el idioma final de salida al idioma detectado del último mensaje del huésped.
        # Evita respuestas en español cuando el huésped escribe en pt/fr/de, etc.
        response_raw = _ensure_guest_language(response_raw)
        response_raw = _sanitize_guest_facing_response(response_raw)
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
                    guest_chat_id=escalation_chat_id,
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
                    property_id=property_id,
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
            chat_id=mem_id,
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
                guest_chat_id=escalation_chat_id,
                guest_message=user_message,
                escalation_type="bad_response",
                reason=motivo_out,
                context=context_full,
                property_id=property_id,
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
            guest_chat_id=escalation_chat_id,
            guest_message=user_message,
            escalation_type="info_not_found",
            reason=f"Error crítico: {str(exc)}",
            context="Excepción general en process_user_message",
            property_id=property_id,
        )
        return None
