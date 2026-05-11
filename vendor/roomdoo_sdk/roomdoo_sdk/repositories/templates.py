from __future__ import annotations

import logging

from ..transports.base import Transport

_logger = logging.getLogger(__name__)


class TemplateRepository:
    def __init__(self, transport: Transport):
        self._transport = transport

    async def update_translation_status(
        self,
        template_code: str,
        language: str,
        meta_status: str,
        meta_template_id: str | None = None,
        waba_id: str | None = None,
    ) -> bool:
        """Update a WhatsApp translation status in Odoo.

        Called by BooKAI when Meta sends a template status
        change (approved, rejected, etc.).

        Returns True if the translation was found and updated.
        """
        domain: list = [
            (
                "template_id.bookai_template_code",
                "=",
                template_code,
            ),
            ("language", "=", language),
        ]
        if waba_id:
            domain.append(
                ("wa_account_id.waba_id", "=", waba_id)
            )

        records = await self._transport.search_read(
            "bookai.whatsapp.translation",
            domain,
            fields=["id"],
            limit=1,
        )
        if not records:
            _logger.warning(
                "Translation not found: %s/%s/%s",
                template_code,
                language,
                waba_id,
            )
            return False

        vals: dict = {"meta_status": meta_status}
        if meta_template_id:
            vals["meta_template_id"] = meta_template_id

        await self._transport.write(
            "bookai.whatsapp.translation",
            [records[0]["id"]],
            vals,
        )
        _logger.info(
            "Updated translation %s/%s → %s",
            template_code,
            language,
            meta_status,
        )
        return True
