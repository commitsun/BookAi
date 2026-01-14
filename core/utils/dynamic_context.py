"""Helpers to inject dynamic instance context into prompts."""

from __future__ import annotations

from typing import Any, Optional


def _stringify(value: Any) -> str:
    if value is None or value == "":
        return "N/A"
    return str(value)


def build_dynamic_context_block(
    *,
    instance_url: Optional[str] = None,
    property_id: Optional[Any] = None,
    kb: Optional[str] = None,
    guest_number: Optional[str] = None,
) -> str:
    """Return formatted dynamic context block or empty string when unused."""
    return (
        "-**CONTEXTO:**\n"
        f"Instance_url: {_stringify(instance_url)},\n"
        f"Property_id: {_stringify(property_id)},\n"
        f"Kb: {_stringify(kb)},\n"
        f"Guest_number: {_stringify(guest_number)}"
    )


def build_dynamic_context_from_memory(memory_manager, chat_id: str) -> str:
    """Collect dynamic context from MemoryManager flags."""
    if not memory_manager or not chat_id:
        return ""

    instance_url = memory_manager.get_flag(chat_id, "instance_url")
    property_id = memory_manager.get_flag(chat_id, "property_id")
    kb = memory_manager.get_flag(chat_id, "kb")
    guest_number = (
        memory_manager.get_flag(chat_id, "guest_number")
        or memory_manager.get_flag(chat_id, "whatsapp_number")
        or chat_id
    )

    return build_dynamic_context_block(
        instance_url=instance_url,
        property_id=property_id,
        kb=kb,
        guest_number=guest_number,
    )
