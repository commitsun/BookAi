"""
POST /api/v1/setup                         — full instance setup (Odoo calls after install)
POST /api/v1/instances/{id}/sync-properties — bulk sync properties from Odoo via SDK
POST /webhooks/property-updated             — receive property changes from Odoo
"""

import logging
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_instance, get_sdk_registry
from app.models.instance import Instance
from app.repositories import property_repo
from app.services.instance_sdk_registry import InstanceSDKRegistry

log = logging.getLogger("property_webhooks")


# ── Schemas ──────────────────────────────────────────────────────────

class PropertyData(BaseModel):
    id: int
    name: str
    external_code: str
    bookai_mode: str = "disabled"
    tz: str = "UTC"
    email: str | None = None
    phone: str | None = None


class PropertyUpdatedPayload(BaseModel):
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
    payload: PropertyUpdatedPayload,
    instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
) -> dict:
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
    steps: list[dict] = []

    # 0. Store Odoo credentials on the instance
    instance.instance_url = body.odoo_url
    instance.roomdoo_db = body.odoo_db
    instance.roomdoo_username = body.odoo_username
    instance.roomdoo_password = body.odoo_api_key
    await db.flush()

    # Evict any cached client with old credentials
    sdk_registry.evict(instance.id)

    # 1. Verify SDK connection
    client = sdk_registry.get_client(instance)
    if client is None:
        return {
            "status": "error",
            "detail": "Failed to create SDK client with provided credentials.",
            "steps": [],
        }

    try:
        await client._transport.authenticate()
        steps.append({"step": "sdk_connection", "status": "ok"})
    except Exception as exc:
        return {
            "status": "error",
            "detail": f"SDK connection failed: {exc}",
            "steps": [{"step": "sdk_connection", "status": "error", "detail": str(exc)}],
        }

    # Credentials verified — persist them
    await db.commit()

    # 2. Sync properties
    try:
        odoo_properties = await client.properties.list()
        created_count = 0
        synced_properties: list[PropertyOut] = []
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
        loader = await sdk_registry.get_or_load_agents(instance)
        agent_count = loader.count if loader else 0
        steps.append({
            "step": "load_agents",
            "status": "ok",
            "agents_loaded": agent_count,
        })
    except Exception as exc:
        steps.append({"step": "load_agents", "status": "error", "detail": str(exc)})

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
