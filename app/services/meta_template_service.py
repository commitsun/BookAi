"""
Meta Cloud API operations for WhatsApp message templates.

Handles creation, deletion, and status checking of templates on the
Meta Business Platform. All HTTP calls go through an httpx.AsyncClient.
"""

import logging

import httpx

log = logging.getLogger("meta_template_service")

GRAPH_API_BASE = "https://graph.facebook.com/v20.0"


class MetaTemplateError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Meta API {status_code}: {detail}")


async def create_template_in_meta(
    http: httpx.AsyncClient,
    waba_id: str,
    access_token: str,
    name: str,
    language: str,
    category: str,
    header_text: str | None = None,
    body_text: str | None = None,
    footer_text: str | None = None,
    button_texts: list[dict] | None = None,
) -> tuple[str, str]:
    """Create a template in Meta. Returns (meta_template_id, status).

    Raises MetaTemplateError on failure.
    """
    components = []

    if header_text:
        components.append({
            "type": "HEADER",
            "format": "TEXT",
            "text": header_text,
        })

    if body_text:
        components.append({
            "type": "BODY",
            "text": body_text,
        })

    if footer_text:
        components.append({
            "type": "FOOTER",
            "text": footer_text,
        })

    if button_texts:
        buttons = []
        for btn in button_texts:
            btn_type = btn.get("type", "URL").upper()
            if btn_type == "URL":
                buttons.append({
                    "type": "URL",
                    "text": btn.get("text", ""),
                    "url": btn.get("url", ""),
                })
            elif btn_type == "QUICK_REPLY":
                buttons.append({
                    "type": "QUICK_REPLY",
                    "text": btn.get("text", ""),
                })
            elif btn_type == "PHONE_NUMBER":
                buttons.append({
                    "type": "PHONE_NUMBER",
                    "text": btn.get("text", ""),
                    "phone_number": btn.get("phone_number", ""),
                })
        if buttons:
            components.append({"type": "BUTTONS", "buttons": buttons})

    payload = {
        "name": name,
        "language": language,
        "category": category.upper(),
        "components": components,
    }

    url = f"{GRAPH_API_BASE}/{waba_id}/message_templates"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    log.info("Creating template '%s' (%s) in Meta WABA %s", name, language, waba_id)

    try:
        r = await http.post(url, json=payload, headers=headers, timeout=15)
    except httpx.TimeoutException as exc:
        raise MetaTemplateError(0, f"Timeout: {exc}") from exc

    if r.status_code not in (200, 201):
        raise MetaTemplateError(r.status_code, r.text[:500])

    data = r.json()
    meta_id = data.get("id", "")
    status = data.get("status", "PENDING").lower()

    log.info("Template created: meta_id=%s status=%s", meta_id, status)
    return meta_id, status


async def delete_template_in_meta(
    http: httpx.AsyncClient,
    waba_id: str,
    access_token: str,
    name: str,
) -> bool:
    """Delete a template by name from Meta. Returns True if deleted."""
    url = f"{GRAPH_API_BASE}/{waba_id}/message_templates"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"name": name}

    try:
        r = await http.delete(url, headers=headers, params=params, timeout=15)
    except httpx.TimeoutException:
        log.warning("Timeout deleting template '%s'", name)
        return False

    if r.status_code == 200:
        log.info("Template '%s' deleted from Meta", name)
        return True

    log.warning("Failed to delete template '%s': %d %s", name, r.status_code, r.text[:200])
    return False


async def check_template_status(
    http: httpx.AsyncClient,
    waba_id: str,
    access_token: str,
    name: str,
    language: str | None = None,
) -> str | None:
    """Check the current status of a template on Meta.

    Returns status string (approved/rejected/pending/...) or None if not found.
    """
    url = f"{GRAPH_API_BASE}/{waba_id}/message_templates"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"name": name}

    try:
        r = await http.get(url, headers=headers, params=params, timeout=10)
    except Exception as exc:
        log.warning("Failed to check template status '%s': %s", name, exc)
        return None

    if r.status_code != 200:
        return None

    data = r.json()
    templates = data.get("data", [])

    for tmpl in templates:
        if language and tmpl.get("language") != language:
            continue
        return tmpl.get("status", "").lower()

    return None
