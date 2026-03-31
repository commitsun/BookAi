"""Helpers puros para healthchecks de WhatsApp."""

from __future__ import annotations

import re
import unicodedata

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
