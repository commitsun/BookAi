"""Helpers para adjuntar contenido estructurado a mensajes de plantilla."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

_TEMPLATE_SENT_EVENT = "template_sent"
_TEMPLATE_STRUCTURED_KINDS = {
    "template",
    "reservation_confirmation",
    "reservation_update",
    "reservation_cancellation",
    "pre_checkin",
}


def _norm_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")


def _to_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        text = re.sub(r"\s+", " ", value).strip()
        return text or None
    if isinstance(value, (list, tuple)):
        parts = [part for part in (_to_text(item) for item in value) if part]
        if parts:
            return ", ".join(parts)
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except Exception:
            return str(value)
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _csv_value(value: Any) -> Optional[str]:
    text = _to_text(value)
    if not text:
        return None
    # Evita romper el delimitador ';' manteniendo un formato facil de parsear.
    return text.replace(";", ",")


def _flat_params(params: Dict[str, Any] | None) -> Dict[str, Any]:
    if not isinstance(params, dict):
        return {}
    flat: Dict[str, Any] = {}
    for key, value in params.items():
        normalized = _norm_key(key)
        if not normalized:
            continue
        if normalized not in flat:
            flat[normalized] = value
    return flat


def _pick(flat: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = flat.get(_norm_key(key))
        text = _to_text(value)
        if text:
            return text
    return None


def _infer_kind(template_code: str, template_name: str, trigger: Optional[str] = None) -> str:
    haystack = " ".join(
        part.strip().lower()
        for part in [template_code, template_name, trigger or ""]
        if part and str(part).strip()
    )
    if any(token in haystack for token in ("pre_check", "precheck", "pre check", "online_checkin")):
        return "pre_checkin"
    if any(token in haystack for token in ("modify", "modif", "update", "cambio", "change")):
        return "reservation_update"
    if any(token in haystack for token in ("cancel", "cancellation", "anul")):
        return "reservation_cancellation"
    if any(token in haystack for token in ("confirm", "confirmation", "confirmacion")):
        return "reservation_confirmation"
    return "template"


def build_template_structured_payload(
    *,
    template_code: Optional[str],
    template_name: Optional[str],
    language: Optional[str],
    parameters: Dict[str, Any] | None,
    reservation_locator: Optional[str] = None,
    folio_id: Optional[str] = None,
    guest_name: Optional[str] = None,
    hotel_name: Optional[str] = None,
    room_type: Optional[str] = None,
    checkin: Optional[str] = None,
    checkout: Optional[str] = None,
    cta_label: Optional[str] = None,
    cta_action: Optional[str] = None,
    cta_url: Optional[str] = None,
    trigger: Optional[str] = None,
) -> Dict[str, Any]:
    flat = _flat_params(parameters)
    tpl_code = _to_text(template_code) or _pick(flat, "template_code", "template")
    tpl_name = _to_text(template_name) or tpl_code
    kind = _infer_kind(tpl_code or "", tpl_name or "", trigger=trigger)

    data: Dict[str, str] = {"kind": kind}
    if tpl_code:
        data["template_code"] = tpl_code
    if tpl_name and tpl_name != tpl_code:
        data["template_name"] = tpl_name
    if language:
        normalized_lang = str(language).strip().lower()
        if normalized_lang:
            data["template_language"] = normalized_lang

    reservation_code = _to_text(reservation_locator) or _pick(
        flat,
        "reservation_code",
        "reservation_locator",
        "locator",
        "localizador",
        "code",
        "name",
    )
    folio_value = _to_text(folio_id) or _pick(flat, "folio_id", "folio", "reservation_id", "id_reserva")
    guest_value = _to_text(guest_name) or _pick(
        flat,
        "guest_name",
        "client_name",
        "partner_name",
        "name_guest",
        "full_name",
    )
    hotel_value = _to_text(hotel_name) or _pick(flat, "hotel_name", "hotel", "property_name", "property")
    room_value = _to_text(room_type) or _pick(
        flat,
        "room_type",
        "room",
        "room_name",
        "room_category",
        "habitacion",
        "tipo_habitacion",
    )
    checkin_value = _to_text(checkin) or _pick(flat, "checkin", "check_in", "arrival", "checkin_date")
    checkout_value = _to_text(checkout) or _pick(flat, "checkout", "check_out", "departure", "checkout_date")
    cta_label_value = _to_text(cta_label) or _pick(
        flat,
        "cta_label",
        "button_label",
        "button_text",
        "action_label",
        "call_to_action",
        "cta_text",
    )
    cta_action_value = _to_text(cta_action) or _pick(
        flat,
        "cta_action",
        "button_action",
        "action_type",
        "cta_type",
        "action",
    )
    cta_url_value = _to_text(cta_url) or _pick(
        flat,
        "cta_url",
        "reservation_url",
        "folio_details_url",
        "booking_url",
        "url",
        "link",
    )

    if reservation_code:
        data["reservation_code"] = reservation_code
    if folio_value:
        data["folio_id"] = folio_value
    if guest_value:
        data["guest_name"] = guest_value
    if hotel_value:
        data["hotel_name"] = hotel_value
    if room_value:
        data["room_type"] = room_value
    if checkin_value:
        data["checkin"] = checkin_value
    if checkout_value:
        data["checkout"] = checkout_value
    if cta_label_value:
        data["cta_label"] = cta_label_value
    if cta_action_value:
        data["cta_action"] = cta_action_value
    if cta_url_value:
        data["cta_url"] = cta_url_value

    ordered_keys = [
        "kind",
        "template_code",
        "template_name",
        "template_language",
        "reservation_code",
        "folio_id",
        "guest_name",
        "hotel_name",
        "room_type",
        "checkin",
        "checkout",
        "cta_label",
        "cta_action",
        "cta_url",
    ]
    pairs: list[str] = []
    for key in ordered_keys:
        if key not in data:
            continue
        value = _csv_value(data.get(key))
        if value:
            pairs.append(f"{key}={value}")
    for key in sorted(k for k in data.keys() if k not in ordered_keys):
        value = _csv_value(data.get(key))
        if value:
            pairs.append(f"{key}={value}")

    csv = ";".join(pairs).strip()
    if csv and not csv.endswith(";"):
        csv += ";"

    return {
        "event": _TEMPLATE_SENT_EVENT,
        "kind": kind,
        "format": "kv_semicolon",
        "csv_delimiter": ";",
        "csv": csv,
        "data": data,
    }


def _coerce_structured_payload(structured_payload: Any) -> Optional[Dict[str, Any]]:
    if isinstance(structured_payload, dict):
        return structured_payload
    if isinstance(structured_payload, str):
        try:
            parsed = json.loads(structured_payload)
        except Exception:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def extract_template_sent_metadata(
    structured_payload: Any,
    content: Any = None,
) -> Optional[Dict[str, str]]:
    payload = _coerce_structured_payload(structured_payload)
    if payload:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        event = (_to_text(payload.get("event")) or _to_text(data.get("event")) or "").lower()
        kind = (_to_text(payload.get("kind")) or _to_text(data.get("kind")) or "").lower()
        template_code = _to_text(data.get("template_code")) or _to_text(payload.get("template_code"))
        template_name = (
            _to_text(data.get("template_name"))
            or _to_text(payload.get("template_name"))
            or template_code
        )
        template_language = (
            _to_text(data.get("template_language"))
            or _to_text(payload.get("template_language"))
        )
        looks_like_template = bool(template_code or template_name) and (
            event == _TEMPLATE_SENT_EVENT
            or kind in _TEMPLATE_STRUCTURED_KINDS
            or bool(template_language)
        )
        if looks_like_template:
            metadata: Dict[str, str] = {"event": _TEMPLATE_SENT_EVENT}
            if template_code:
                metadata["template_code"] = template_code
            if template_name:
                metadata["template_name"] = template_name
            if template_language:
                metadata["template_language"] = template_language.lower()
            return metadata

    raw = str(content or "").strip()
    if not raw.lower().startswith("[template_sent]"):
        return None

    metadata = {"event": _TEMPLATE_SENT_EVENT}
    for key, value in re.findall(r"([a-zA-Z_]+)=([^\s]+)", raw):
        normalized_key = str(key or "").strip().lower()
        normalized_value = str(value or "").strip()
        if not normalized_value:
            continue
        if normalized_key in {"plantilla", "template"}:
            metadata["template_name"] = normalized_value
        elif normalized_key == "lang":
            metadata["template_language"] = normalized_value.lower()
        elif normalized_key == "template_code":
            metadata["template_code"] = normalized_value
    return metadata


def build_template_sent_marker(metadata: Dict[str, Any] | None) -> str:
    payload = metadata or {}
    template_name = _to_text(payload.get("template_name")) or _to_text(payload.get("template_code"))
    template_language = _to_text(payload.get("template_language"))

    parts = ["[TEMPLATE_SENT]"]
    if template_name:
        parts.append(f"plantilla={template_name}")
    if template_language:
        parts.append(f"lang={template_language.lower()}")
    return " ".join(parts)


def build_template_sent_preview(
    metadata: Dict[str, Any] | None,
    *,
    label: str = "Plantilla enviada",
) -> Optional[str]:
    if metadata is None:
        return None
    payload = metadata or {}
    template_name = _to_text(payload.get("template_name")) or _to_text(payload.get("template_code"))
    if template_name:
        return f"{label}: {template_name}"
    clean_label = _to_text(label)
    return clean_label or None


def extract_structured_csv(structured_payload: Any) -> Optional[str]:
    if not isinstance(structured_payload, dict):
        return None
    csv = structured_payload.get("csv")
    if isinstance(csv, str) and csv.strip():
        return csv.strip()
    return None
