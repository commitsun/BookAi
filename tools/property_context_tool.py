"""
Property Context Tool
=====================
Resuelve y fija el contexto de property (property_id + hotel_code) usando
los webhooks de n8n/Supabase. Deja el contexto en MemoryManager para que
otras tools lo usen.
"""

from __future__ import annotations

import logging
from typing import Optional, Any

from pydantic import BaseModel, Field
from langchain.tools import StructuredTool

from core.instance_context import (
    DEFAULT_PROPERTY_TABLE,
    fetch_property_by_code,
    fetch_property_by_id,
    fetch_property_by_name,
    fetch_instance_by_code,
)

log = logging.getLogger("PropertyContextTool")


class PropertyContextInput(BaseModel):
    """Input schema para resolver el contexto de property."""

    hotel_code: Optional[str] = Field(
        default=None,
        description=(
            "Codigo o nombre del hotel/property. Usalo cuando el cliente diga el nombre del hotel "
            "o quieras fijar el contexto de la property."
        ),
    )
    property_id: Optional[int] = Field(
        default=None,
        description="ID de propiedad (property_id) si ya se conoce.",
    )
    property_table: Optional[str] = Field(
        default=None,
        description="Nombre de la tabla de properties (opcional).",
    )


def _hotel_code_variants(raw: Optional[str]) -> list[str]:
    clean = (raw or "").strip()
    if not clean:
        return []
    variants = [clean]
    for prefix in ("Hotel ", "Hostal "):
        if not clean.lower().startswith(prefix.lower()):
            variants.append(f"{prefix}{clean}")
    return list(dict.fromkeys(variants))


class PropertyContextTool:
    """Herramienta para fijar el contexto de property en memoria."""

    def __init__(self, memory_manager=None, chat_id: str = ""):
        self.memory_manager = memory_manager
        self.chat_id = chat_id
        log.info("✅ PropertyContextTool inicializado para chat %s", chat_id)

    def _resolve_table(self, property_table: Optional[str]) -> str:
        if property_table:
            return str(property_table)
        if self.memory_manager and self.chat_id:
            try:
                table = self.memory_manager.get_flag(self.chat_id, "property_table")
                if table:
                    return str(table)
            except Exception:
                pass
        return DEFAULT_PROPERTY_TABLE

    def _set_flags(
        self,
        property_id: Optional[Any],
        hotel_code: Optional[str],
        property_table: Optional[str] = None,
    ) -> None:
        if not self.memory_manager or not self.chat_id:
            return

        if property_table:
            self.memory_manager.set_flag(self.chat_id, "property_table", property_table)

        if property_id is not None:
            self.memory_manager.set_flag(self.chat_id, "property_id", property_id)
            self.memory_manager.set_flag(self.chat_id, "wa_context_property_id", property_id)
        if hotel_code:
            self.memory_manager.set_flag(self.chat_id, "property_name", hotel_code)
            self.memory_manager.set_flag(self.chat_id, "wa_context_hotel_code", str(hotel_code))

        # Guardar constancia en historial con property_id ya fijado (sin exponer IDs al usuario)
        try:
            if hotel_code:
                note = f"Contexto de propiedad actualizado: {hotel_code}."
            else:
                note = "Contexto de propiedad actualizado."
            self.memory_manager.save(self.chat_id, "system", note)
        except Exception:
            log.debug("No se pudo guardar constancia de property en historial", exc_info=True)

    async def _run_async(
        self,
        hotel_code: Optional[str] = None,
        property_id: Optional[int] = None,
        property_table: Optional[str] = None,
    ) -> str:
        if not self.memory_manager or not self.chat_id:
            return "No tengo memoria configurada para fijar la propiedad."

        table = self._resolve_table(property_table)
        resolved_property_id: Optional[Any] = property_id
        resolved_hotel_code = (hotel_code or "").strip() or None

        log.info(
            "PropertyContextTool start chat_id=%s property_id=%s hotel_code=%s table=%s",
            self.chat_id,
            resolved_property_id,
            resolved_hotel_code,
            table,
        )

        if resolved_property_id is None and resolved_hotel_code:
            for variant in _hotel_code_variants(resolved_hotel_code):
                payload = fetch_property_by_code(table, variant)
                prop_id = payload.get("property_id") if payload else None
                if prop_id is None:
                    payload = fetch_property_by_name(table, variant)
                    prop_id = payload.get("property_id") if payload else None
                if prop_id is not None:
                    resolved_property_id = prop_id
                    resolved_hotel_code = payload.get("hotel_code") or payload.get("name") or variant
                    break

        if resolved_property_id is not None and not resolved_hotel_code:
            payload = fetch_property_by_id(table, resolved_property_id)
            if payload:
                resolved_hotel_code = payload.get("hotel_code") or payload.get("name")

        if resolved_hotel_code:
            # Intentar fijar credenciales de instancia si existen
            try:
                inst_payload = fetch_instance_by_code(str(resolved_hotel_code))
                for key in ("whatsapp_phone_id", "whatsapp_token", "whatsapp_verify_token"):
                    val = inst_payload.get(key) if inst_payload else None
                    if val:
                        self.memory_manager.set_flag(self.chat_id, key, val)
            except Exception:
                log.debug("No se pudieron fijar credenciales de instancia", exc_info=True)

        if resolved_property_id is None and resolved_hotel_code is None:
            return (
                "Necesito el codigo o nombre del hotel para identificar la propiedad."
            )

        if resolved_property_id is None:
            # Guardar al menos el hotel_code como contexto
            self._set_flags(None, resolved_hotel_code, table)
            return (
                f"Listo, ya tengo el contexto del hotel {resolved_hotel_code}."
                if resolved_hotel_code
                else "Contexto del hotel actualizado."
            )

        self._set_flags(resolved_property_id, resolved_hotel_code, table)
        if resolved_hotel_code:
            return f"Perfecto, ya identifique el hotel {resolved_hotel_code}."
        return "Perfecto, ya identifique la propiedad."

    def _run(
        self,
        hotel_code: Optional[str] = None,
        property_id: Optional[int] = None,
        property_table: Optional[str] = None,
    ) -> str:
        import asyncio

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        if loop.is_running():
            import nest_asyncio

            nest_asyncio.apply()

        return loop.run_until_complete(
            self._run_async(
                hotel_code=hotel_code,
                property_id=property_id,
                property_table=property_table,
            )
        )

    def as_tool(self) -> StructuredTool:
        return StructuredTool(
            name="identificar_property",
            description=(
                "Identifica y fija el contexto de la property/hotel (property_id y hotel_code) en memoria. "
                "Usala cuando el cliente mencione el hotel, una propiedad especifica o quieras filtrar por property."
            ),
            func=self._run,
            coroutine=self._run_async,
            args_schema=PropertyContextInput,
        )


def create_property_context_tool(memory_manager=None, chat_id: str = "") -> StructuredTool:
    tool_instance = PropertyContextTool(memory_manager=memory_manager, chat_id=chat_id)
    return tool_instance.as_tool()
