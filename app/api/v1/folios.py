"""
Folio endpoints:
  PATCH /api/v1/folios/{odoo_external_code}       — folio cache update from Roomdoo
  POST  /api/v1/folios/{odoo_external_code}/events — folio lifecycle event notification
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_instance
from app.models.instance import Instance
from app.repositories import folio_repo
from app.schemas.folio import (
    FolioEventRequest,
    FolioEventResponse,
    FolioUpdateRequest,
    FolioUpdateResponse,
)
from app.services import folio_event_service

log = logging.getLogger("folios")
router = APIRouter(prefix="/folios", tags=["folios"])

_SUMMARY = "Update cached folio fields"

_CODE_FORMAT_NOTE = """
**External code (`odoo_external_code`):** stored as-is from the PMS
(e.g. `206/26/003`). The endpoint uses a path parameter, so slashes
in the code are supported natively.
"""

_DESCRIPTION = """
Called by Roomdoo whenever a reservation changes in Odoo.

BookAI caches the dynamic fields (status, dates, pending payment) so the
app can search across reservations without querying Odoo directly.

Only fields present in the request body are updated; absent fields are
left unchanged. The folio must already exist (created during the
`send-template` flow) — returns **404** if not found.
""" + _CODE_FORMAT_NOTE

_EVENTS_DESCRIPTION = """
Called by Roomdoo when a reservation changes state. BookAI creates an
internal note in all active sessions linked to the folio.

Returns `notes_created=0` (not an error) if no active sessions are found.
""" + _CODE_FORMAT_NOTE


@router.patch(
    "/{odoo_external_code:path}",
    response_model=FolioUpdateResponse,
    status_code=status.HTTP_200_OK,
    summary=_SUMMARY,
    description=_DESCRIPTION,
    responses={
        401: {"description": "Missing or invalid Bearer token"},
        404: {"description": "Folio not found"},
    },
)
async def update_folio_cache(
    odoo_external_code: str,
    body: FolioUpdateRequest,
    _instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
) -> FolioUpdateResponse:
    folio = await folio_repo.find_by_code(db, odoo_external_code)
    if folio is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Folio '{odoo_external_code}' not found",
        )

    await folio_repo.update_cache(
        db, folio, body.model_dump(exclude_unset=True)
    )
    await db.commit()
    await db.refresh(folio)

    log.info(
        "Folio cache updated: code=%s status=%s synced_at=%s",
        folio.odoo_external_code,
        folio.status,
        folio.synced_at,
    )

    return FolioUpdateResponse(
        odoo_external_code=folio.odoo_external_code,
        status=folio.status,
        checkin_date=folio.checkin_date,
        checkout_date=folio.checkout_date,
        pending_payment_amount=folio.pending_payment_amount,
        pending_payment_currency=folio.pending_payment_currency,
        synced_at=folio.synced_at.isoformat() if folio.synced_at else None,
    )


@router.post(
    "/{odoo_external_code:path}/events",
    response_model=FolioEventResponse,
    status_code=status.HTTP_200_OK,
    summary="Notify a folio lifecycle event",
    description=_EVENTS_DESCRIPTION,
    responses={
        401: {"description": "Missing or invalid Bearer token"},
        404: {"description": "Folio not found"},
        422: {"description": "Invalid event payload"},
    },
)
async def folio_event(
    odoo_external_code: str,
    body: FolioEventRequest,
    instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
) -> FolioEventResponse:
    notes_created = await folio_event_service.process_folio_event(
        db, instance, odoo_external_code, body
    )
    await db.commit()
    return FolioEventResponse(
        folio_code=odoo_external_code,
        event_type=body.event_type,
        notes_created=notes_created,
    )
