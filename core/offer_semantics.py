"""Utilidades semánticas para detectar y persistir ofertas operativas pendientes."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Optional

from core.config import ModelConfig, ModelTier

log = logging.getLogger("OfferSemantics")


def normalize_guest_id(guest_id: str | None) -> str:
    return str(guest_id or "").replace("+", "").strip()


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


async def classify_offer_state_from_wa_message(llm: Any, message: str) -> dict[str, Any]:
    text = (message or "").strip()
    if not llm or not text:
        return {"action": "none", "confidence": 0.0}
    prompt = (
        "Analiza un mensaje saliente de WhatsApp enviado por un hotel al huésped.\n"
        "Devuelve JSON con este esquema exacto:\n"
        "{"
        "\"action\":\"set_pending|clear_pending|none\","
        "\"is_offer\":true|false,"
        "\"offer_type\":\"string\","
        "\"has_actionable_details\":true|false,"
        "\"missing_fields\":[\"schedule|location|booking_method|conditions\"],"
        "\"confidence\":0.0"
        "}\n\n"
        "Reglas:\n"
        "- set_pending: promete una cortesía/beneficio pero faltan datos operativos para ejecutarlo.\n"
        "- clear_pending: aporta datos operativos suficientes para una oferta pendiente previa.\n"
        "- none: no aplica a una oferta pendiente.\n"
        "- Usa semántica, no coincidencia literal de palabras.\n"
        "- Responde solo JSON válido.\n\n"
        f"Mensaje:\n{text}"
    )
    try:
        response = await llm.ainvoke(
            [
                {"role": "system", "content": "Eres un clasificador semántico de operaciones hoteleras."},
                {"role": "user", "content": prompt},
            ]
        )
        data = _extract_json_object((getattr(response, "content", None) or "").strip()) or {}
    except Exception as exc:
        log.debug("No se pudo clasificar estado de oferta WA: %s", exc)
        data = {}

    action = str(data.get("action") or "none").strip().lower()
    if action not in {"set_pending", "clear_pending", "none"}:
        action = "none"
    missing_fields = data.get("missing_fields")
    if not isinstance(missing_fields, list):
        missing_fields = []
    missing_fields = [str(x).strip() for x in missing_fields if str(x).strip()]
    return {
        "action": action,
        "is_offer": bool(data.get("is_offer")),
        "offer_type": str(data.get("offer_type") or "").strip() or "unspecified_offer",
        "has_actionable_details": bool(data.get("has_actionable_details")),
        "missing_fields": missing_fields,
        "confidence": _safe_float(data.get("confidence"), 0.0),
    }


async def sync_guest_offer_state_from_sent_wa(
    state: Any,
    *,
    guest_id: str,
    sent_message: str,
    source: str = "superintendente",
    owner_id: Optional[str] = None,
    session_id: Optional[str] = None,
    property_id: Optional[str | int] = None,
) -> None:
    memory = getattr(state, "memory_manager", None)
    clean_guest = normalize_guest_id(guest_id)
    if not memory or not clean_guest or not str(sent_message or "").strip():
        return
    try:
        llm = getattr(getattr(state, "superintendente_agent", None), "llm", None) or ModelConfig.get_llm(ModelTier.INTERNAL)
        sem = await classify_offer_state_from_wa_message(llm, sent_message)
    except Exception:
        return

    action = sem.get("action")
    confidence = _safe_float(sem.get("confidence"), 0.0)
    if action == "set_pending" and confidence >= 0.65:
        now = datetime.utcnow()
        payload = {
            "type": sem.get("offer_type") or "unspecified_offer",
            "source": str(source or "unknown"),
            "details_missing": True,
            "missing_fields": sem.get("missing_fields") or [],
            "original_text": (sent_message or "").strip(),
            "owner_id": str(owner_id or "").strip() or None,
            "session_id": str(session_id or "").strip() or None,
            "property_id": property_id,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=24)).isoformat(),
        }
        try:
            memory.set_flag(clean_guest, "super_offer_pending", payload)
            if guest_id != clean_guest:
                memory.set_flag(guest_id, "super_offer_pending", payload)
            memory.set_flag(clean_guest, "super_offer_pending_offer_type", payload["type"])
        except Exception:
            pass
    elif action == "clear_pending" and confidence >= 0.60:
        try:
            memory.clear_flag(clean_guest, "super_offer_pending")
            memory.clear_flag(clean_guest, "super_offer_pending_offer_type")
            if guest_id != clean_guest:
                memory.clear_flag(guest_id, "super_offer_pending")
                memory.clear_flag(guest_id, "super_offer_pending_offer_type")
        except Exception:
            pass
