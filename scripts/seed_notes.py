"""
Seed demo notes into existing demo conversations.

Run with:
  docker exec bookai python scripts/seed_notes.py
"""

import asyncio

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.instance import Instance
from app.schemas.folio import FolioEventRequest, FolioEventType
from app.services import folio_event_service


async def main() -> None:
    async with SessionLocal() as db:
        instance = (
            await db.execute(
                select(Instance).where(
                    Instance.bearer_token == "dev-token-demo-2026"
                )
            )
        ).scalar_one()

        # ── Conv 1 — María García (folio 206_26_001) ─────────────────────────
        for event in [
            FolioEventRequest(
                event_type=FolioEventType.folio_created, data={}
            ),
            FolioEventRequest(
                event_type=FolioEventType.status_changed,
                data={"new_status": "confirm"},
            ),
            FolioEventRequest(
                event_type=FolioEventType.payment_registered,
                data={"amount": "500.00", "currency": "EUR"},
            ),
            FolioEventRequest(
                event_type=FolioEventType.status_changed,
                data={"new_status": "onboard"},
            ),
        ]:
            await folio_event_service.process_folio_event(
                db, instance, "206_26_001", event
            )

        # ── Conv 2 — Jean Dupont (folio 206_26_002) ──────────────────────────
        for event in [
            FolioEventRequest(
                event_type=FolioEventType.folio_created, data={}
            ),
            FolioEventRequest(
                event_type=FolioEventType.folio_modified,
                data={
                    "modification_type": "dates_changed",
                    "checkin_date": "2026-05-10",
                    "checkout_date": "2026-05-14",
                },
            ),
        ]:
            await folio_event_service.process_folio_event(
                db, instance, "206_26_002", event
            )

        # ── Conv 3 — James Smith (folio 206_26_003) ──────────────────────────
        for event in [
            FolioEventRequest(
                event_type=FolioEventType.folio_created, data={}
            ),
            FolioEventRequest(
                event_type=FolioEventType.precheckin_completed,
                data={"guest_name": "James Smith", "room_number": "302"},
            ),
        ]:
            await folio_event_service.process_folio_event(
                db, instance, "206_26_003", event
            )

        # ── Conv 4 — Li Wei (folio 206_26_004) ───────────────────────────────
        for event in [
            FolioEventRequest(
                event_type=FolioEventType.folio_created, data={}
            ),
            FolioEventRequest(
                event_type=FolioEventType.payment_registered,
                data={"amount": "1200.00", "currency": "EUR"},
            ),
        ]:
            await folio_event_service.process_folio_event(
                db, instance, "206_26_004", event
            )

        # ── Conv 5 — Carlos Martínez (folio 206_26_005) ──────────────────────
        for event in [
            FolioEventRequest(
                event_type=FolioEventType.folio_created, data={}
            ),
            FolioEventRequest(
                event_type=FolioEventType.status_changed,
                data={"new_status": "confirm"},
            ),
        ]:
            await folio_event_service.process_folio_event(
                db, instance, "206_26_005", event
            )

        await db.commit()
        print("✅ Demo notes seeded successfully.")


if __name__ == "__main__":
    asyncio.run(main())
