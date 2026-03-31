"""Helpers de healthchecks de WhatsApp."""

from __future__ import annotations

import logging
import re
import unicodedata

log = logging.getLogger("WhatsAppHealthcheck")

WHATSAPP_HEALTHCHECKS = {
    "PRUEBA COMPLETA BOOKAI": {
        "matched_keyword": "PRUEBA COMPLETA BOOKAI",
        "path": "complete",
    },
    "PRUEBA IA BOOKAI": {
        "matched_keyword": "PRUEBA IA BOOKAI",
        "path": "ia",
    },
    "PRUEBA BOOKAI": {
        "matched_keyword": "PRUEBA BOOKAI",
        "path": "basic",
    },
}

WHATSAPP_HEALTHCHECK_INPUT_PROBE = "Quiero saber el horario de check-in del hotel."


def _normalize_whatsapp_healthcheck_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.upper()
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def detect_whatsapp_healthcheck(value: str) -> dict | None:
    normalized = _normalize_whatsapp_healthcheck_text(value)
    if not normalized:
        return None
    return WHATSAPP_HEALTHCHECKS.get(normalized)


def build_whatsapp_healthcheck_response(path: str, *, has_real_meta_inbound: bool = False) -> str:
    normalized_path = str(path or "").strip().lower()
    if normalized_path == "basic":
        return "✅ BookAI responde correctamente.\nFlujo básico de prueba operativo."
    if normalized_path == "ia":
        return "✅ BookAI responde correctamente.\nFlujo de IA de prueba operativo."
    if normalized_path == "complete":
        if has_real_meta_inbound:
            return "✅ BookAI funciona correctamente.\nFlujo completo operativo."
        return "⚠️ La comprobación completa de BookAI no funciona correctamente en este entorno."
    return "✅ BookAI responde correctamente."


def build_whatsapp_healthcheck_ai_failure_response(path: str) -> str:
    normalized_path = str(path or "").strip().lower()
    if normalized_path == "complete":
        return "⚠️ La comprobación completa de BookAI no funciona correctamente en este entorno."
    if normalized_path == "ia":
        return "⚠️ La IA de BookAI no funciona correctamente en este entorno."
    return "⚠️ BookAI responde, pero la validación interna no está operativa correctamente."


def build_whatsapp_healthcheck_complete_failure_response() -> str:
    return "⚠️ La comprobación completa de BookAI no funciona correctamente en este entorno."


def is_whatsapp_healthcheck_response(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    candidates = {
        build_whatsapp_healthcheck_response("basic", has_real_meta_inbound=False),
        build_whatsapp_healthcheck_response("ia", has_real_meta_inbound=False),
        build_whatsapp_healthcheck_response("complete", has_real_meta_inbound=False),
        build_whatsapp_healthcheck_response("complete", has_real_meta_inbound=True),
        build_whatsapp_healthcheck_ai_failure_response("ia"),
        build_whatsapp_healthcheck_ai_failure_response("complete"),
        build_whatsapp_healthcheck_complete_failure_response(),
    }
    return text in candidates


async def execute_whatsapp_healthcheck(
    state,
    path: str,
    *,
    has_real_meta_inbound: bool = False,
    chat_id: str | None = None,
    trace_id: str | None = None,
) -> str:
    normalized_path = str(path or "").strip().lower()
    current_trace_id = str(trace_id or chat_id or f"healthcheck:{normalized_path or 'unknown'}").strip()
    response = build_whatsapp_healthcheck_response(
        normalized_path,
        has_real_meta_inbound=has_real_meta_inbound,
    )

    if normalized_path not in {"ia", "complete"}:
        return response

    try:
        input_validation = await state.supervisor_input.validate(
            WHATSAPP_HEALTHCHECK_INPUT_PROBE,
            chat_id=chat_id,
        )
        estado_in = str(input_validation.get("estado", "") or "").strip().lower()
        if estado_in not in {"aprobado", "ok", "aceptable"}:
            log.warning(
                "healthcheck ai validation rejected trace_id=%s path=%s estado=%s motivo=%s",
                current_trace_id,
                normalized_path,
                input_validation.get("estado"),
                input_validation.get("motivo"),
            )
            return build_whatsapp_healthcheck_ai_failure_response(normalized_path)
        log.info(
            "healthcheck ai validation success trace_id=%s path=%s",
            current_trace_id,
            normalized_path,
        )
    except Exception as exc:
        log.error(
            "healthcheck ai validation failure trace_id=%s path=%s error=%s",
            current_trace_id,
            normalized_path,
            exc,
            exc_info=True,
        )
        return build_whatsapp_healthcheck_ai_failure_response(normalized_path)

    if normalized_path != "complete":
        return response

    try:
        output_validation = await state.supervisor_output.validate(
            user_input=WHATSAPP_HEALTHCHECK_INPUT_PROBE,
            agent_response=response,
            chat_id=chat_id,
        )
        estado_out = str(output_validation.get("estado", "") or "").strip().lower()
        if "aprobado" not in estado_out:
            log.warning(
                "healthcheck output validation rejected trace_id=%s estado=%s motivo=%s",
                current_trace_id,
                output_validation.get("estado"),
                output_validation.get("motivo"),
            )
            return build_whatsapp_healthcheck_complete_failure_response()
        log.info("healthcheck output validation success trace_id=%s", current_trace_id)
    except Exception as exc:
        log.error(
            "healthcheck output validation failure trace_id=%s error=%s",
            current_trace_id,
            exc,
            exc_info=True,
        )
        return build_whatsapp_healthcheck_complete_failure_response()

    return response
