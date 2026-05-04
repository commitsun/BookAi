"""
Identify the type of caller based on their phone number.

Three types:
- roomdoo: internal Roomdoo team (whitelist)
- internal: hotel staff (found in Odoo as user)
- external_guest: everyone else (default)

Called once when a session is created. The result is cached in
session.caller_type and session.odoo_user_id.
"""

import logging
from dataclasses import dataclass

from app.services.instance_sdk_registry import InstanceSDKRegistry
from app.models.instance import Instance

log = logging.getLogger("caller_identifier")


@dataclass
class CallerIdentity:
    caller_type: str  # "roomdoo" | "internal" | "external_guest"
    odoo_user_id: int | None = None


async def identify_caller(
    phone_code: str,
    sdk_registry: InstanceSDKRegistry,
    instance: Instance,
) -> CallerIdentity:
    """Identify the caller type from their phone number."""
    # 1. Roomdoo staff (whitelist from instance config)
    whitelist = set(instance.roomdoo_staff_phones or [])
    if phone_code in whitelist or phone_code[-9:] in {p[-9:] for p in whitelist}:
        log.info("Caller %s identified as roomdoo (whitelist)", phone_code)
        return CallerIdentity(caller_type="roomdoo")

    # 2. Internal hotel staff (user exists in Odoo)
    client = sdk_registry.get_client(instance)
    if client:
        try:
            users = await client._transport.search_read(
                "res.users",
                ["|", ("phone", "ilike", phone_code[-9:]), ("mobile", "ilike", phone_code[-9:])],
                ["id", "name"],
                limit=1,
            )
            if users:
                log.info(
                    "Caller %s identified as internal (Odoo user: %s)",
                    phone_code, users[0].get("name"),
                )
                return CallerIdentity(
                    caller_type="internal",
                    odoo_user_id=users[0]["id"],
                )
        except Exception as exc:
            log.debug("Odoo user lookup failed for %s: %s", phone_code, exc)

    # 3. Default: external guest
    return CallerIdentity(caller_type="external_guest")
