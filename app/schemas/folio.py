"""
Pydantic schemas for the folio cache endpoint.
"""

import enum
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from app.models.folio import FolioStatus


class FolioEventType(str, enum.Enum):
    folio_created = "folio_created"
    folio_cancelled = "folio_cancelled"
    folio_modified = "folio_modified"
    payment_registered = "payment_registered"
    precheckin_completed = "precheckin_completed"
    status_changed = "status_changed"


class ModificationType(str, enum.Enum):
    room_added = "room_added"
    room_cancelled = "room_cancelled"
    dates_changed = "dates_changed"
    service_added = "service_added"
    room_changed = "room_changed"


class FolioEventRequest(BaseModel):
    event_type: FolioEventType
    data: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_data(self) -> "FolioEventRequest":
        et = self.event_type
        d = self.data
        if et == FolioEventType.folio_modified:
            if "modification_type" not in d:
                raise ValueError("folio_modified requires 'modification_type' in data")
            valid = {m.value for m in ModificationType}
            if d["modification_type"] not in valid:
                raise ValueError(
                    f"Invalid modification_type '{d['modification_type']}'. "
                    f"Expected one of: {sorted(valid)}"
                )
            if d["modification_type"] == ModificationType.dates_changed:
                if "checkin_date" not in d or "checkout_date" not in d:
                    raise ValueError(
                        "dates_changed requires 'checkin_date' and 'checkout_date' in data"
                    )
        elif et == FolioEventType.payment_registered:
            if "amount" not in d or "currency" not in d:
                raise ValueError(
                    "payment_registered requires 'amount' and 'currency' in data"
                )
        elif et == FolioEventType.precheckin_completed:
            if "guest_name" not in d or "room_number" not in d:
                raise ValueError(
                    "precheckin_completed requires 'guest_name' and 'room_number' in data"
                )
        elif et == FolioEventType.status_changed:
            if "new_status" not in d:
                raise ValueError("status_changed requires 'new_status' in data")
        return self


class FolioEventResponse(BaseModel):
    folio_code: str
    event_type: FolioEventType
    notes_created: int


class FolioUpdateRequest(BaseModel):
    """
    Partial push from Roomdoo when a reservation changes in Odoo.

    Only fields included in the request body are written; absent fields
    are left unchanged. Send null explicitly to clear a field.
    """

    status: FolioStatus | None = Field(
        default=None,
        description=(
            "Reservation status from the PMS. "
            "Allowed values: `draft`, `confirm`, `onboard`, `done`, `cancel`."
        ),
        examples=["onboard"],
    )
    checkin_date: date | None = Field(default=None, examples=["2026-04-01"])
    checkout_date: date | None = Field(default=None, examples=["2026-04-05"])
    pending_payment_amount: Decimal | None = Field(
        default=None,
        examples=[150.00],
        description="Outstanding balance in the folio currency.",
    )
    pending_payment_currency: str | None = Field(
        default=None,
        description="ISO 4217 currency code.",
        examples=["EUR"],
    )


class FolioUpdateResponse(BaseModel):
    odoo_external_code: str
    status: FolioStatus | None
    checkin_date: date | None
    checkout_date: date | None
    pending_payment_amount: Decimal | None
    pending_payment_currency: str | None
    synced_at: str | None
