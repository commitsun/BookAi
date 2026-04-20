"""
Select the best agent for an inbound message.

- If session has active_agent_id → use that agent directly
- If single candidate → use it directly (no LLM call)
- If multiple candidates → LLM router selects the best one
"""

import logging

from app.services.agent_loader import CachedAgent
from app.services.llm_client import LLMProvider

log = logging.getLogger("agent_selector")


async def select_agent(
    message: str,
    candidates: list[CachedAgent],
    active_agent_id: int | None,
    llm_client: LLMProvider,
    router_config: dict,
) -> CachedAgent | None:
    """Select the best agent for the given message.

    Args:
        message: The inbound message text.
        candidates: Available agents for this caller type.
        active_agent_id: Pinned agent ID from session (skip selection).
        llm_client: LLM provider for the router call.
        router_config: {provider, api_key, model} from Instance.

    Returns:
        Selected CachedAgent, or None if no suitable agent.
    """
    if not candidates:
        return None

    # Pinned agent — use directly
    if active_agent_id is not None:
        for c in candidates:
            if c.config.id == active_agent_id:
                return c
        # Pinned agent no longer in cache — fall through to selection

    # Single candidate — no need for router
    if len(candidates) == 1:
        return candidates[0]

    # Filter out the router agent itself
    non_router = [c for c in candidates if c.config.technical_name != "router"]
    if len(non_router) == 1:
        return non_router[0]
    if not non_router:
        return candidates[0]

    # Multiple candidates — use LLM to select
    if not router_config.get("api_key") or not router_config.get("model"):
        log.warning("No router LLM config — defaulting to first agent")
        return non_router[0]

    agent_list = "\n".join(
        f"- {c.config.technical_name}: {c.config.description}"
        for c in non_router
    )
    prompt = (
        "Eres un router de agentes. Dado el siguiente mensaje de un huésped "
        "y la lista de agentes disponibles, devuelve SOLO el technical_name "
        "del agente más apropiado para responder. No expliques tu decisión.\n\n"
        f"Agentes disponibles:\n{agent_list}\n\n"
        f"Mensaje del huésped: {message}\n\n"
        "technical_name:"
    )

    try:
        response = await llm_client.chat(
            messages=[{"role": "user", "content": prompt}],
            provider=router_config["provider"],
            api_key=router_config["api_key"],
            model=router_config["model"],
            temperature=0.0,
            max_tokens=100,
        )
        selected = (response.content or "").strip().strip("'\"")
        for c in non_router:
            if c.config.technical_name == selected:
                log.info("Router selected agent: %s", selected)
                return c
        log.warning("Router returned '%s' — not in candidates, using first", selected)
    except Exception as exc:
        log.error("Router LLM call failed: %s — using first agent", exc)

    return non_router[0]
