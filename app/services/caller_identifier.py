"""
Identify the type of caller based on their phone number.

Three types:
- roomdoo: internal Roomdoo team (whitelist)
- internal: hotel staff (found in Odoo as user)
- external_guest: everyone else (default)

Called once when a session is created. The result is cached in
session.caller_type so subsequent messages don't re-check.
"""

import logging

from app.services.instance_sdk_registry import InstanceSDKRegistry
from app.models.instance import Instance

log = logging.getLogger("caller_identifier")

# Roomdoo staff whitelist — for Phase 1 we hardcode known numbers.
# In production this would come from Instance config or a DB table.
ROOMDOO_WHITELIST: set[str] = set()


async def identify_caller(
    phone_code: str,
    sdk_registry: InstanceSDKRegistry,
    instance: Instance,
) -> str:
    """Identify the caller type from their phone number.

    Returns: "roomdoo" | "internal" | "external_guest"
    """
    # 1. Roomdoo staff (whitelist)
    if phone_code in ROOMDOO_WHITELIST:
        log.info("Caller %s identified as roomdoo (whitelist)", phone_code)
        return "roomdoo"

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
                return "internal"
        except Exception as exc:
            log.debug("Odoo user lookup failed for %s: %s", phone_code, exc)

    # 3. Default: external guest
    return "external_guest"
