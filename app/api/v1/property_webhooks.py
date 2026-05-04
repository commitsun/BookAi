"""
POST /api/v1/instances/register            — register new instance (provisioning key)
POST /api/v1/setup                         — full instance setup (Odoo calls after install)
POST /api/v1/instances/{id}/sync-properties — bulk sync properties from Odoo via SDK
POST /webhooks/property-updated             — receive property changes from Odoo
"""

import logging
import secrets
from typing import Literal

import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_instance, get_sdk_registry
from app.core.config import settings
from app.models.instance import Instance
from app.repositories import property_repo
from app.services.instance_sdk_registry import InstanceSDKRegistry

log = logging.getLogger("property_webhooks")


# ── Schemas ──────────────────────────────────────────────────────────

class PropertyData(BaseModel):
    model_config = {"extra": "ignore"}

    id: int
    name: str
    external_code: str = ""
    bookai_mode: str = "disabled"
    tz: str = "UTC"
    email: str | None = None
    phone: str | None = None
    # WhatsApp channel
    bookai_wa_phone_number_id: str | None = None
    bookai_wa_access_token: str | None = None
    bookai_wa_account_id: str | None = None
    bookai_wa_verify_token: str | None = None
    bookai_wa_display_number: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _false_to_none(cls, data):
        """Odoo sends False for empty fields instead of null."""
        if isinstance(data, dict):
            for k, v in data.items():
                if v is False:
                    data[k] = None
        return data


class PropertyUpdatedPayload(BaseModel):
    model_config = {"extra": "ignore"}
    type: Literal["property_updated"]
    property_id: int
    property_data: PropertyData


class PropertyOut(BaseModel):
    id: int
    odoo_property_id: int | None
    name: str
    roomdoo_external_code: str
    bookai_mode: str
    tz: str | None
    email: str | None
    phone: str | None


# ── Webhook: property-updated ────────────────────────────────────────

webhook_router = APIRouter(prefix="/webhooks", tags=["property-webhooks"])


@webhook_router.post(
    "/property-updated",
    status_code=200,
    summary="Receive property changes from Odoo",
    description=(
        "Called by the pms_bookai Odoo module when a property's relevant "
        "fields change (name, bookai_mode, timezone, contact info). "
        "Creates the property if it doesn't exist in BookAI yet."
    ),
)
async def property_updated(
    request: Request,
    instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
) -> dict:
    raw = await request.json()
    log.info("property-updated payload: %s", json.dumps(raw, default=str)[:1000])
    try:
        payload = PropertyUpdatedPayload(**raw)
    except Exception as exc:
        log.error("property-updated validation failed: %s", exc)
        return {"status": "error", "detail": str(exc)}
    data = payload.property_data
    prop, created = await property_repo.upsert_from_odoo(
        db,
        instance_id=instance.id,
        odoo_property_id=data.id,
        name=data.name,
        external_code=data.external_code,
        bookai_mode=data.bookai_mode,
        tz=data.tz,
        email=data.email,
        phone=data.phone,
        wa_phone_number_id=data.bookai_wa_phone_number_id,
        wa_access_token=data.bookai_wa_access_token,
        wa_account_id=data.bookai_wa_account_id,
        wa_verify_token=data.bookai_wa_verify_token,
        wa_display_number=data.bookai_wa_display_number,
    )
    await db.commit()

    action = "created" if created else "updated"
    log.info(
        "Property %s %s (odoo_id=%d, mode=%s)",
        prop.name, action, data.id, data.bookai_mode,
    )
    return {"status": "ok", "action": action, "property_id": prop.id}


# ── Sync: bulk load from Odoo ────────────────────────────────────────

api_router = APIRouter(prefix="/instances", tags=["instances"])


@api_router.post(
    "/{instance_id}/sync-properties",
    summary="Sync properties from Odoo via SDK",
    description=(
        "Loads all properties from the Odoo instance via roomdoo-sdk "
        "and upserts them into BookAI. Use this after registering a new "
        "instance or to force a full re-sync."
    ),
)
async def sync_properties(
    instance_id: int,
    instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
    sdk_registry: InstanceSDKRegistry = Depends(get_sdk_registry),
) -> dict:
    client = sdk_registry.get_client(instance)
    if client is None:
        return {
            "status": "error",
            "detail": "Instance has no Odoo SDK configuration",
        }

    odoo_properties = await client.properties.list()

    results: list[PropertyOut] = []
    created_count = 0
    for odoo_prop in odoo_properties:
        prop, created = await property_repo.upsert_from_odoo(
            db,
            instance_id=instance.id,
            odoo_property_id=odoo_prop.id,
            name=odoo_prop.name,
            external_code=odoo_prop.external_code or odoo_prop.pms_property_code or "",
            bookai_mode=odoo_prop.bookai_mode,
            tz=odoo_prop.tz,
            email=odoo_prop.email,
            phone=odoo_prop.phone,
        )
        if created:
            created_count += 1
        results.append(PropertyOut(
            id=prop.id,
            odoo_property_id=prop.odoo_property_id,
            name=prop.name,
            roomdoo_external_code=prop.roomdoo_external_code,
            bookai_mode=prop.bookai_mode,
            tz=prop.tz,
            email=prop.email,
            phone=prop.phone,
        ))

    await db.commit()

    log.info(
        "Synced %d properties for instance %d (%d created, %d updated)",
        len(results), instance.id, created_count, len(results) - created_count,
    )
    return {
        "status": "ok",
        "synced": len(results),
        "created": created_count,
        "updated": len(results) - created_count,
        "properties": [p.model_dump() for p in results],
    }


# ── Setup: full instance initialization ──────────────────────────────


class SetupPayload(BaseModel):
    odoo_url: str
    odoo_db: str
    odoo_username: str
    odoo_api_key: str


async def _run_setup_steps(
    instance: Instance,
    db: AsyncSession,
    sdk_registry: InstanceSDKRegistry,
) -> tuple[list[dict], list["PropertyOut"]]:
    """Shared setup logic: verify SDK, sync properties, load agents.

    Returns (steps, synced_properties).
    """
    steps: list[dict] = []
    synced_properties: list[PropertyOut] = []

    # 1. Verify SDK connection
    client = sdk_registry.get_client(instance)
    if client is None:
        steps.append({"step": "sdk_connection", "status": "error", "detail": "Failed to create SDK client"})
        return steps, synced_properties

    try:
        await client._transport.authenticate()
        steps.append({"step": "sdk_connection", "status": "ok"})
    except Exception as exc:
        steps.append({"step": "sdk_connection", "status": "error", "detail": str(exc)})
        return steps, synced_properties

    await db.commit()

    # 2. Sync properties
    try:
        odoo_properties = await client.properties.list()
        log.info("SDK returned %d properties", len(odoo_properties))
        created_count = 0
        for odoo_prop in odoo_properties:
            log.info(
                "Syncing property: name=%s odoo_id=%s code=%s mode=%s",
                odoo_prop.name, odoo_prop.id,
                odoo_prop.external_code or odoo_prop.pms_property_code or "",
                odoo_prop.bookai_mode,
            )
            try:
                prop, created = await property_repo.upsert_from_odoo(
                    db,
                    instance_id=instance.id,
                    odoo_property_id=odoo_prop.id,
                    name=odoo_prop.name,
                    external_code=odoo_prop.external_code or odoo_prop.pms_property_code or "",
                    bookai_mode=odoo_prop.bookai_mode,
                    tz=odoo_prop.tz,
                    email=odoo_prop.email,
                    phone=odoo_prop.phone,
                    wa_phone_number_id=getattr(odoo_prop, "bookai_wa_phone_number_id", None),
                    wa_access_token=getattr(odoo_prop, "bookai_wa_access_token", None),
                    wa_account_id=getattr(odoo_prop, "bookai_wa_account_id", None),
                    wa_verify_token=getattr(odoo_prop, "bookai_wa_verify_token", None),
                    wa_display_number=getattr(odoo_prop, "bookai_wa_display_number", None),
                )
                log.info("  → property id=%d created=%s", prop.id, created)
            except Exception as prop_exc:
                log.error("Failed to sync property %s (odoo_id=%d): %s", odoo_prop.name, odoo_prop.id, prop_exc)
                continue
            if created:
                created_count += 1
            synced_properties.append(PropertyOut(
                id=prop.id,
                odoo_property_id=prop.odoo_property_id,
                name=prop.name,
                roomdoo_external_code=prop.roomdoo_external_code,
                bookai_mode=prop.bookai_mode,
                tz=prop.tz,
                email=prop.email,
                phone=prop.phone,
            ))
        await db.commit()
        steps.append({
            "step": "sync_properties",
            "status": "ok",
            "synced": len(synced_properties),
            "created": created_count,
        })
    except Exception as exc:
        steps.append({"step": "sync_properties", "status": "error", "detail": str(exc)})

    # 3. Pre-load agents into cache
    try:
        loader = await sdk_registry.get_or_load_agents(instance, db)
        agent_count = loader.count if loader else 0
        await db.commit()
        steps.append({
            "step": "load_agents",
            "status": "ok",
            "agents_loaded": agent_count,
        })
    except Exception as exc:
        steps.append({"step": "load_agents", "status": "error", "detail": str(exc)})

    return steps, synced_properties


# ── Register: create new instance ──────────────────────────────────────


@api_router.post(
    "/register",
    summary="Register a new Odoo instance — requires provisioning key",
    description=(
        "Creates a new BookAI instance for an Odoo installation. "
        "Requires the master provisioning key (not a bearer token). "
        "Returns a bearer_token that Odoo must persist for all future calls."
    ),
)
async def register_instance(
    body: SetupPayload,
    db: AsyncSession = Depends(get_db),
    sdk_registry: InstanceSDKRegistry = Depends(get_sdk_registry),
    x_provisioning_key: str | None = Header(default=None),
) -> dict:
    # Verify provisioning key
    if not settings.provisioning_key:
        raise HTTPException(status_code=503, detail="Provisioning not configured")
    if x_provisioning_key != settings.provisioning_key:
        raise HTTPException(status_code=401, detail="Invalid provisioning key")

    # Generate bearer token
    bearer_token = secrets.token_urlsafe(32)

    # Create instance
    instance = Instance(
        instance_url=body.odoo_url,
        bearer_token=bearer_token,
        bookai_enabled=True,
        active=True,
        roomdoo_db=body.odoo_db,
        roomdoo_username=body.odoo_username,
        roomdoo_password=body.odoo_api_key,
    )
    db.add(instance)
    await db.commit()

    log.info("Registered new instance %d for %s", instance.id, body.odoo_url)

    # Run setup steps
    sdk_registry.evict(instance.id)
    steps, synced_properties = await _run_setup_steps(instance, db, sdk_registry)

    all_ok = all(s["status"] == "ok" for s in steps)
    log.info(
        "Register %s for instance %d (%s): %s",
        "completed" if all_ok else "partial",
        instance.id, body.odoo_url,
        ", ".join(f'{s["step"]}={s["status"]}' for s in steps),
    )

    return {
        "status": "ok" if all_ok else "partial",
        "instance_id": instance.id,
        "bearer_token": bearer_token,
        "steps": steps,
        "properties": [p.model_dump() for p in synced_properties] if all_ok else [],
    }


# ── Setup: update existing instance ────────────────────────────────────


@api_router.post(
    "/setup",
    summary="Full instance setup — called by Odoo after module install",
    description=(
        "Odoo calls this endpoint after the pms_bookai module is installed "
        "and configured. Receives Odoo connection credentials, stores them, "
        "verifies the SDK connection, syncs all properties, and pre-loads "
        "AI agents into cache."
    ),
)
async def instance_setup(
    body: SetupPayload,
    instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
    sdk_registry: InstanceSDKRegistry = Depends(get_sdk_registry),
) -> dict:
    # Update Odoo credentials
    instance.instance_url = body.odoo_url
    instance.roomdoo_db = body.odoo_db
    instance.roomdoo_username = body.odoo_username
    instance.roomdoo_password = body.odoo_api_key
    await db.flush()
    sdk_registry.evict(instance.id)

    # Run shared setup steps
    steps, synced_properties = await _run_setup_steps(instance, db, sdk_registry)

    all_ok = all(s["status"] == "ok" for s in steps)
    log.info(
        "Setup %s for instance %d (%s): %s",
        "completed" if all_ok else "partial",
        instance.id, instance.instance_url,
        ", ".join(f'{s["step"]}={s["status"]}' for s in steps),
    )

    return {
        "status": "ok" if all_ok else "partial",
        "instance_id": instance.id,
        "steps": steps,
        "properties": [p.model_dump() for p in synced_properties] if all_ok else [],
    }
