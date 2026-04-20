"""
POST /webhooks/property-updated  — receive property changes from Odoo
POST /api/v1/instances/{id}/sync-properties — bulk sync properties from Odoo via SDK
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
