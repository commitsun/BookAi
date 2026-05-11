from __future__ import annotations

from ..transports.base import Transport


class UsersRepository:
    def __init__(self, transport: Transport):
        self._transport = transport

    async def search_by_phone(
        self, phone: str
    ) -> dict | None:
        """Search for an Odoo user by phone or mobile.

        Uses the last 9 digits for matching to handle
        different prefix formats (+34, 0034, etc.).
        Returns {id, name, login, phone, mobile} or None.
        """
        suffix = phone[-9:] if len(phone) >= 9 else phone
        pattern = f"%{suffix}%"
        records = await self._transport.search_read(
            "res.users",
            [
                "|",
                ("phone", "ilike", pattern),
                ("mobile", "ilike", pattern),
            ],
            fields=["id", "name", "login", "phone", "mobile"],
            limit=1,
        )
        return records[0] if records else None
