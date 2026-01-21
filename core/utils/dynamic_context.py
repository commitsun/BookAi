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
    property_name = memory_manager.get_flag(chat_id, "property_name")
    guest_number = (
        memory_manager.get_flag(chat_id, "guest_number")
        or memory_manager.get_flag(chat_id, "whatsapp_number")
        or chat_id
    )

    base_block = build_dynamic_context_block(
        instance_url=instance_url,
        property_id=property_id,
        kb=kb,
        guest_number=guest_number,
    )

    temp_block = ""
    try:
        from core.db import fetch_kb_daily_cache

        entries = fetch_kb_daily_cache(
            property_id=property_id,
            kb_name=kb,
            property_name=property_name,
        )
    except Exception:
        entries = []

    if entries:
        lines = ["-**TEMP_KB (pendiente de vectorizar):**"]
        for entry in entries:
            topic = (entry.get("topic") or "").strip()
            category = (entry.get("category") or "").strip()
            content = (entry.get("content") or "").strip()
            header = topic or category
            if header:
                lines.append(f"{header}: {content}")
            else:
                lines.append(content)
        temp_block = "\n".join(lines)

    if temp_block:
        return f"{base_block}\n\n{temp_block}"
    return base_block
