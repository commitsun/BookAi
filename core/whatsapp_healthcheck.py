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
            return "✅ BookAI responde correctamente.\nFlujo completo de prueba operativo."
        return (
            "⚠️ BookAI responde correctamente, pero el flujo completo de WhatsApp "
            "no está validado en este entorno."
        )
    return "✅ BookAI responde correctamente."
