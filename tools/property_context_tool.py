"""
Property Context Tool
=====================
Resuelve y fija el contexto de property (property_id + instance_id) usando
los webhooks de n8n/Supabase. Deja el contexto en MemoryManager para que
otras tools lo usen.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Optional, Any

from pydantic import BaseModel, Field
from langchain.tools import StructuredTool

from core.instance_context import (
    DEFAULT_PROPERTY_TABLE,
    fetch_property_by_code,
    fetch_property_by_id,
    fetch_property_by_name,
    fetch_properties_by_code,
    fetch_properties_by_query,
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

def _clean_hotel_input(raw: Optional[str]) -> Optional[str]:
    text = (raw or "").strip()
    if not text:
        return None
    lowered = text.lower()
    for prefix in (
        "para el ",
        "para la ",
        "para los ",
        "para las ",
        "para ",
        "en el ",
        "en la ",
        "en los ",
        "en las ",
        "en ",
    ):
        if lowered.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    lowered = text.lower()
    for prefix in ("el ", "la ", "los ", "las "):
        if lowered.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    return text or None

def _is_valid_hotel_label(raw: Optional[str]) -> bool:
    clean = (raw or "").strip().lower()
    if not clean or len(clean) < 3:
        return False
    generic = {"hotel", "hostal", "alojamiento", "propiedad"}
    if clean in generic:
        return False
    banned = {
        "reserva",
        "reservar",
        "quiero",
        "hacer",
        "otra",
        "nueva",
        "precio",
        "precios",
        "disponibilidad",
        "oferta",
    }
    if any(term in clean for term in banned):
        return False
    return True

def _normalize_match_text(value: Optional[str]) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    stop = {"hotel", "hostal", "alda", "el", "la", "los", "las", "de", "del"}
    tokens = [t for t in text.split() if t and t not in stop]
    return " ".join(tokens)


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
        display_name: Optional[str] = None,
        instance_id: Optional[str] = None,
    ) -> None:
        if not self.memory_manager or not self.chat_id:
            return

        if property_table:
            self.memory_manager.set_flag(self.chat_id, "property_table", property_table)

        if property_id is not None:
            self.memory_manager.set_flag(self.chat_id, "property_id", property_id)
            self.memory_manager.set_flag(self.chat_id, "wa_context_property_id", property_id)
        if instance_id:
            self.memory_manager.set_flag(self.chat_id, "instance_id", str(instance_id))
            self.memory_manager.set_flag(self.chat_id, "instance_hotel_code", str(instance_id))
            self.memory_manager.set_flag(self.chat_id, "wa_context_instance_id", str(instance_id))
        elif hotel_code:
            self.memory_manager.set_flag(self.chat_id, "wa_context_hotel_code", str(hotel_code))
        if display_name:
            self.memory_manager.set_flag(self.chat_id, "property_display_name", str(display_name))
            self.memory_manager.set_flag(self.chat_id, "property_name", str(display_name))
        elif hotel_code:
            self.memory_manager.set_flag(self.chat_id, "property_name", str(hotel_code))

        # Guardar constancia en historial con property_id ya fijado (sin exponer IDs al usuario)
        try:
            label = display_name or hotel_code
            if label:
                note = f"Contexto de propiedad actualizado: {label}."
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
        resolved_hotel_code = _clean_hotel_input(hotel_code) or None
        resolved_display_name: Optional[str] = None
        resolved_instance_id: Optional[str] = None

        log.info(
            "PropertyContextTool start chat_id=%s property_id=%s hotel_code=%s table=%s",
            self.chat_id,
            resolved_property_id,
            resolved_hotel_code,
            table,
        )

        # Preferir lista MCP por instance_id (si existe) para resolver nombres parciales
        if resolved_property_id is None and resolved_hotel_code and self.memory_manager and self.chat_id:
            instance_code = self.memory_manager.get_flag(self.chat_id, "instance_id") or self.memory_manager.get_flag(self.chat_id, "instance_hotel_code")
            if instance_code:
                resolved_instance_id = str(instance_code)
                inst_candidates = fetch_properties_by_code(table, str(instance_code))
                log.info(
                    "PropertyContextTool MCP fallback chat_id=%s instance_code=%s candidates=%s",
                    self.chat_id,
                    instance_code,
                    len(inst_candidates) if isinstance(inst_candidates, list) else "n/a",
                )
                if inst_candidates:
                    target = _normalize_match_text(resolved_hotel_code)
                    matched = []
                    for row in inst_candidates:
                        name = row.get("name") or row.get("property_name") or ""
                        name_norm = _normalize_match_text(name)
                        if target and target in name_norm:
                            matched.append(row)
                    if len(matched) == 1:
                        payload = matched[0]
                        resolved_property_id = payload.get("property_id")
                        resolved_hotel_code = payload.get("name") or payload.get("property_name") or resolved_hotel_code
                        resolved_instance_id = payload.get("instance_id") or resolved_instance_id
                        resolved_display_name = payload.get("name") or payload.get("property_name")
                        log.info(
                            "PropertyContextTool MCP fallback matched chat_id=%s property_id=%s name=%s",
                            self.chat_id,
                            resolved_property_id,
                            resolved_display_name or resolved_hotel_code,
                        )
                    elif len(matched) > 1:
                        self.memory_manager.set_flag(
                            self.chat_id,
                            "property_disambiguation_candidates",
                            [
                                {
                                    "property_id": row.get("property_id"),
                                    "name": row.get("name") or row.get("property_name"),
                                    "instance_id": row.get("instance_id"),
                                    "city": row.get("city"),
                                    "street": row.get("street"),
                                }
                                for row in matched
                            ],
                        )
                        self.memory_manager.set_flag(
                            self.chat_id,
                            "property_disambiguation_instance_id",
                            resolved_hotel_code,
                        )
                        return (
                            "He encontrado varios hoteles parecidos. "
                            "¿Podrías indicarme el nombre del hotel (aprox)?"
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
                    resolved_hotel_code = payload.get("name") or payload.get("property_name") or variant
                    resolved_instance_id = payload.get("instance_id") or resolved_instance_id
                    resolved_display_name = payload.get("name") or payload.get("property_name")
                    break

        if resolved_property_id is None and resolved_hotel_code and len(resolved_hotel_code) >= 3:
            candidates = fetch_properties_by_query(table, resolved_hotel_code)
            if candidates:
                if len(candidates) == 1:
                    payload = candidates[0]
                    resolved_property_id = payload.get("property_id")
                    resolved_hotel_code = payload.get("name") or payload.get("property_name") or resolved_hotel_code
                    resolved_instance_id = payload.get("instance_id") or resolved_instance_id
                    resolved_display_name = payload.get("name") or payload.get("property_name")
                else:
                    if self.memory_manager and self.chat_id:
                        self.memory_manager.set_flag(
                            self.chat_id,
                            "property_disambiguation_candidates",
                            [
                                {
                                    "property_id": row.get("property_id"),
                                    "name": row.get("name") or row.get("property_name"),
                                    "instance_id": row.get("instance_id"),
                                    "city": row.get("city"),
                                    "street": row.get("street"),
                                }
                                for row in candidates
                            ],
                        )
                        self.memory_manager.set_flag(
                            self.chat_id,
                            "property_disambiguation_instance_id",
                            resolved_hotel_code,
                        )
                    preview = candidates[:5]
                    lines = []
                    for row in preview:
                        name = row.get("name") or row.get("property_name") or "Hotel"
                        city = row.get("city") or ""
                        street = row.get("street") or ""
                        address = ", ".join([part for part in [street, city] if part])
                        if address:
                            lines.append(f"- {name} — {address}")
                        else:
                            lines.append(f"- {name}")
                    extra = ""
                    if len(candidates) > len(preview):
                        extra = f"\nY {len(candidates) - len(preview)} más."
                    return (
                        "He encontrado varios hoteles parecidos. "
                        "Indícame el nombre del hotel (aprox):\n"
                        + "\n".join(lines)
                        + extra
                    )

        if resolved_property_id is not None and not resolved_hotel_code:
            payload = fetch_property_by_id(table, resolved_property_id)
            if payload:
                resolved_display_name = payload.get("name") or payload.get("property_name")
                resolved_hotel_code = resolved_display_name
                resolved_instance_id = payload.get("instance_id") or resolved_instance_id

        if resolved_instance_id or resolved_hotel_code:
            # Intentar fijar credenciales de instancia si existen
            try:
                instance_key = resolved_instance_id or resolved_hotel_code
                inst_payload = fetch_instance_by_code(str(instance_key)) if instance_key else None
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
            if not _is_valid_hotel_label(resolved_hotel_code):
                return "Necesito el codigo o nombre del hotel para identificar la propiedad."
            # Guardar al menos el hotel_code como contexto si parece válido
            self._set_flags(None, resolved_hotel_code, table, display_name=resolved_display_name, instance_id=resolved_instance_id)
            return (
                f"Listo, ya tengo el contexto del hotel {resolved_hotel_code}."
                if resolved_hotel_code
                else "Contexto del hotel actualizado."
            )

        log.info(
            "PropertyContextTool resolved chat_id=%s property_id=%s hotel_code=%s display_name=%s",
            self.chat_id,
            resolved_property_id,
            resolved_hotel_code,
            resolved_display_name,
        )
        self._set_flags(
            resolved_property_id,
            resolved_hotel_code,
            table,
            display_name=resolved_display_name,
            instance_id=resolved_instance_id,
        )
        if resolved_hotel_code:
            label = resolved_display_name or resolved_hotel_code
            return f"Perfecto, ya identifique el hotel {label}."
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
