"""
Background service that checks for timed-out escalations and sends
WhatsApp notifications to the property's responsible contacts.

Runs every ~60 seconds via an async loop started from main.py.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import escalation_repo, template_repo
from app.services.instance_sdk_registry import InstanceSDKRegistry
from app.services.whatsapp_client import WhatsAppClient

log = logging.getLogger("escalation_notifier")


async def check_and_notify(
    db: AsyncSession,
    wa_client: WhatsAppClient,
    sdk_registry: InstanceSDKRegistry,
) -> int:
    """Check pending escalations and send timeout notifications.

    Returns count of escalations notified.
    """
    pending = await escalation_repo.find_pending_unnotified(db)
    if not pending:
        return 0

    notified = 0
    now = datetime.now(timezone.utc)

    # Group by property to cache SDK calls
    by_property: dict[int, list] = {}
    for esc in pending:
        prop = esc.session.property if esc.session else None
        if not prop:
            continue
        by_property.setdefault(prop.id, []).append((esc, prop))

    for property_id, items in by_property.items():
        prop = items[0][1]
        instance = prop.instance

        # Get escalation config from SDK
        client = sdk_registry.get_client(instance) if instance else None
        if not client or not prop.odoo_property_id:
            continue

        try:
            pdata = await client.properties.get(prop.odoo_property_id)
        except Exception as exc:
            log.warning("Failed to get property %d config: %s", prop.id, exc)
            continue

        timeout = getattr(pdata, "bookai_escalation_timeout", 0) or 0
        template_code = getattr(pdata, "bookai_escalation_template_code", None)
        contacts = getattr(pdata, "bookai_escalation_contacts", None) or []
        app_url = getattr(pdata, "bookai_app_url", None)

        if timeout <= 0 or not template_code or not contacts:
            continue

        threshold = now - timedelta(minutes=timeout)

        # Resolve template translation
        translation = await template_repo.find_translation_for_property(
            db, template_code, "es", prop.id,
        )
        # Fallback: try without property binding
        if not translation:
            translation = await template_repo.find_translation_for_property_by_prefix(
                db, template_code, "es", prop.id,
            )
        if not translation:
            log.warning(
                "Template '%s' not found for property %d — skipping notifications",
                template_code, prop.id,
            )
            continue

        # Get channel endpoint for sending
        if not prop.channel_endpoint_id:
            continue
        from app.repositories import instance_repo
        channel_endpoint = await instance_repo.find_channel_endpoint_by_id(
            db, prop.channel_endpoint_id,
        )
        if not channel_endpoint:
            continue

        for esc, _ in items:
            if esc.created_at.replace(tzinfo=timezone.utc) > threshold:
                continue  # Not timed out yet

            # Get guest info
            contact = esc.conversation.contact if esc.conversation else None
            guest_name = contact.display_name if contact else "Huésped"
            guest_phone = contact.phone_code if contact else ""

            # Build chat_url
            chat_url = ""
            if app_url and guest_phone:
                chat_url = f"{app_url}/chat/{property_id}?chatId={guest_phone}"

            # Build template components
            components = [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": pdata.name or prop.name},
                        {"type": "text", "text": guest_name},
                        {"type": "text", "text": (esc.reason or "")[:200]},
                        {"type": "text", "text": chat_url or "Ver en la app"},
                    ],
                }
            ]

            # Send to each responsible contact
            sent = False
            for resp in contacts:
                phone = resp.get("phone")
                if not phone:
                    continue
                try:
                    from app.services.phone_utils import normalize_phone
                    phone_normalized = normalize_phone(phone)
                except Exception:
                    phone_normalized = phone.replace("+", "").replace(" ", "")

                try:
                    await wa_client.send_template(
                        to=phone_normalized,
                        channel_endpoint=channel_endpoint,
                        template_name=translation.whatsapp_name,
                        language=translation.language,
                        components=components,
                    )
                    sent = True
                    log.info(
                        "Escalation %d: notified %s (%s)",
                        esc.id, resp.get("name", "?"), phone,
                    )
                except Exception as exc:
                    log.warning(
                        "Failed to notify %s for escalation %d: %s",
                        phone, esc.id, exc,
                    )

            if sent:
                esc.notified_at = now
                notified += 1

    if notified:
        await db.commit()

    return notified
