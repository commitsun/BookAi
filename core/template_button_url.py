"""Helpers para manejar URLs de botones dinamicos en plantillas WhatsApp."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

FOLIO_URL_PARAM_KEYS: Tuple[str, ...] = ("folio_details_url", "folioDetailsUrl")
BUTTON_BASE_URL_PARAM_KEYS: Tuple[str, ...] = (
    "button_base_url",
    "buttonBaseUrl",
    "folio_base_url",
    "folioBaseUrl",
)


def extract_folio_details_url(params: Dict[str, Any] | None) -> Optional[str]:
    if not params:
        return None
    for key in FOLIO_URL_PARAM_KEYS:
        val = params.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def extract_button_base_url(params: Dict[str, Any] | None) -> Optional[str]:
    if not params:
        return None
    for key in BUTTON_BASE_URL_PARAM_KEYS:
        val = params.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def sanitize_base_url(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        return None
    if not raw.endswith("/"):
        raw += "/"
    return raw


def resolve_button_base_url(
    *,
    request_base_url: Optional[str] = None,
    params: Dict[str, Any] | None = None,
    template_components: Any = None,
) -> Optional[str]:
    """
    Prioriza base URL explicita de request y luego fallback en:
    1) parametros de payload
    2) metadata de componentes de plantilla (url con {{1}}).
    """
    return (
        sanitize_base_url(request_base_url)
        or sanitize_base_url(extract_button_base_url(params))
        or extract_button_base_url_from_components(template_components)
    )


def build_folio_details_url(base_url: Optional[str], dynamic_part: Optional[str]) -> Optional[str]:
    dynamic = str(dynamic_part or "").strip()
    if not dynamic:
        return None
    if re.match(r"^https?://", dynamic, re.IGNORECASE):
        return dynamic
    if not base_url:
        return None
    return f"{base_url}{dynamic.lstrip('/')}"


def to_folio_dynamic_part(raw_value: Optional[str], base_url: Optional[str]) -> Optional[str]:
    """
    Meta URL buttons con {{1}} esperan la parte dinamica, no la URL completa.
    Si llega una URL absoluta, se recorta base conocida o al menos host/scheme.
    """
    raw = str(raw_value or "").strip()
    if not raw:
        return None

    known_base = sanitize_base_url(base_url)
    if known_base and raw.lower().startswith(known_base.lower()):
        tail = raw[len(known_base):].lstrip("/")
        return tail or None

    if re.match(r"^https?://", raw, re.IGNORECASE):
        parsed = urlsplit(raw)
        path = (parsed.path or "").lstrip("/")
        query = f"?{parsed.query}" if parsed.query else ""
        fragment = f"#{parsed.fragment}" if parsed.fragment else ""
        dynamic = f"{path}{query}{fragment}".strip()
        return dynamic or None

    return raw.lstrip("/") or None


def extract_url_button_indexes(components: Any) -> list[int]:
    if not isinstance(components, list):
        return []
    indexes: list[int] = []
    for comp in components:
        if not isinstance(comp, dict):
            continue
        if str(comp.get("type") or "").strip().upper() != "BUTTONS":
            continue
        buttons = comp.get("buttons") or []
        if not isinstance(buttons, list):
            continue
        for idx, button in enumerate(buttons):
            if not isinstance(button, dict):
                continue
            if str(button.get("type") or "").strip().upper() == "URL":
                indexes.append(idx)
    return indexes


def extract_button_base_url_from_components(components: Any) -> Optional[str]:
    """
    Extrae base URL fija desde components de Meta.
    Caso tipico:
      button.url = "https://alda.roomdoo.com/{{1}}"
    """
    if not isinstance(components, list):
        return None

    for comp in components:
        if not isinstance(comp, dict):
            continue
        if str(comp.get("type") or "").strip().upper() != "BUTTONS":
            continue
        buttons = comp.get("buttons") or []
        if not isinstance(buttons, list):
            continue
        for button in buttons:
            if not isinstance(button, dict):
                continue
            if str(button.get("type") or "").strip().upper() != "URL":
                continue
            raw_url = str(button.get("url") or "").strip()
            if raw_url:
                with_placeholder = re.sub(r"\{\{\s*\d+\s*\}\}", "", raw_url).strip()
                base = sanitize_base_url(with_placeholder)
                if base:
                    return base
                # Si no hay placeholder pero viene URL absoluta, usa origen.
                parsed = urlsplit(raw_url)
                if parsed.scheme and parsed.netloc:
                    return sanitize_base_url(f"{parsed.scheme}://{parsed.netloc}")
            example = button.get("example")
            if isinstance(example, list) and example:
                sample = str(example[0] or "").strip()
                parsed = urlsplit(sample)
                if parsed.scheme and parsed.netloc:
                    return sanitize_base_url(f"{parsed.scheme}://{parsed.netloc}")
    return None


def strip_url_control_params(params: Dict[str, Any] | None) -> Dict[str, Any]:
    clean = dict(params or {})
    for key in FOLIO_URL_PARAM_KEYS + BUTTON_BASE_URL_PARAM_KEYS:
        clean.pop(key, None)
    return clean
