from __future__ import annotations

from ..exceptions import NotFoundError
from ..models.guest import Invoice
from ..transports.base import Transport

_INVOICE_FIELDS = [
    "id", "name", "state", "move_type",
    "partner_id", "invoice_date",
    "amount_total", "amount_residual",
    "payment_state", "pms_property_id",
]


def _m2o_id(data, field):
    val = data.get(field)
    if isinstance(val, (list, tuple)) and val:
        return val[0]
    return None


def _m2o_name(data, field):
    val = data.get(field)
    if isinstance(val, (list, tuple)) and len(val) > 1:
        return val[1]
    return None


class InvoiceRepository:
    def __init__(self, transport: Transport):
        self._transport = transport

    async def list_by_folio(
        self, folio_id: int
    ) -> list[Invoice]:
        """Get invoices linked to a folio."""
        folios = await self._transport.read(
            "pms.folio", [folio_id], fields=["move_ids"]
        )
        if not folios or not folios[0].get("move_ids"):
            return []
        move_ids = folios[0]["move_ids"]
        records = await self._transport.read(
            "account.move", move_ids, fields=_INVOICE_FIELDS
        )
        return [_build_invoice(r) for r in records]

    async def get(self, invoice_id: int) -> Invoice:
        """Get invoice details."""
        records = await self._transport.read(
            "account.move",
            [invoice_id],
            fields=_INVOICE_FIELDS,
        )
        if not records:
            raise NotFoundError(
                f"Invoice {invoice_id} not found"
            )
        return _build_invoice(records[0])

    async def create_from_folio(
        self, folio_id: int
    ) -> list[int]:
        """Create invoices from a folio. Returns invoice IDs."""
        result = await self._transport.call(
            "pms.folio",
            "_create_invoices",
            args=[[folio_id]],
        )
        if isinstance(result, list):
            return result
        return [result] if result else []

    async def validate(self, invoice_id: int) -> None:
        """Validate/post a draft invoice."""
        await self._transport.call(
            "account.move",
            "action_post",
            args=[[invoice_id]],
        )

    async def get_pdf_url(self, invoice_id: int) -> str:
        """Get portal URL for invoice PDF."""
        records = await self._transport.read(
            "account.move",
            [invoice_id],
            fields=["access_url", "access_token"],
        )
        if not records:
            raise NotFoundError(
                f"Invoice {invoice_id} not found"
            )
        url = records[0].get("access_url", "")
        token = records[0].get("access_token", "")
        if token:
            url = f"{url}?access_token={token}"
        return url


def _build_invoice(d: dict) -> Invoice:
    return Invoice(
        id=d["id"],
        name=d.get("name", ""),
        state=d.get("state", ""),
        move_type=d.get("move_type") or None,
        partner_id=_m2o_id(d, "partner_id"),
        partner_name=_m2o_name(d, "partner_id"),
        invoice_date=d.get("invoice_date") or None,
        amount_total=d.get("amount_total", 0.0),
        amount_residual=d.get("amount_residual", 0.0),
        payment_state=d.get("payment_state") or None,
        pms_property_id=_m2o_id(d, "pms_property_id"),
        pms_property_name=_m2o_name(
            d, "pms_property_id"
        ),
    )
