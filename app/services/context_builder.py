"""
Assemble the LLM prompt from an agent's config, KB documents, and conversation history.

Pure functions — no I/O, no DB, no network. Easy to unit-test.
"""

from __future__ import annotations

from dataclasses import dataclass

from roomdoo_sdk.models import AgentConfig, KBDocument

from app.models.message import Message, MessageDirection


@dataclass
class LLMMessage:
    role: str    # "system" | "user" | "assistant"
    content: str


def build_prompt(
    agent: AgentConfig,
    docs: list[KBDocument],
    conversation_history: list[Message],
    current_message: str,
    property_name: str = "",
) -> list[LLMMessage]:
    """Build the message list ready for an LLM chat completion call.

    Args:
        agent: The selected agent configuration from Odoo.
        docs: KB documents linked to this agent.
        conversation_history: Previous messages ordered oldest-first.
        current_message: The new inbound message text.
        property_name: Hotel name for ``{pms_property_name}`` substitution.

    Returns:
        List of LLMMessage with system prompt + history + current message.
    """
    # 1. Concatenate inject_always KB docs
    kb_parts: list[str] = []
    for doc in docs:
        if doc.inject_always and doc.content:
            kb_parts.append(f"### {doc.name}\n{doc.content}")
    kb_context = "\n\n".join(kb_parts)

    # 2. Build system prompt with variable substitution
    system_text = _build_system_text(agent, kb_context, property_name)

    messages: list[LLMMessage] = [LLMMessage(role="system", content=system_text)]

    # 3. Map conversation history
    for msg in conversation_history:
        if not msg.content:
            continue
        if msg.direction == MessageDirection.inbound:
            messages.append(LLMMessage(role="user", content=msg.content))
        else:
            messages.append(LLMMessage(role="assistant", content=msg.content))

    # 4. Current message
    messages.append(LLMMessage(role="user", content=current_message))

    return messages


def _build_system_text(
    agent: AgentConfig,
    kb_context: str,
    property_name: str,
) -> str:
    """Apply template substitution to produce the final system prompt."""
    if agent.context_template and "{kb_context}" in agent.context_template:
        text = agent.context_template.replace("{kb_context}", kb_context)
    elif kb_context:
        text = (
            f"{agent.system_prompt}\n\n"
            f"## Información relevante\n{kb_context}"
        )
    else:
        text = agent.system_prompt

    # Substitute remaining variables in the final text
    text = text.replace("{pms_property_name}", property_name)
    if "{kb_context}" in text:
        text = text.replace("{kb_context}", kb_context)

    return text
