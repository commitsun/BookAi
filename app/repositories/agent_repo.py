"""
Repository for the persisted Agent table — security source of truth.

The agents table mirrors permission-critical fields from Odoo's bookai.agent model.
It is synced via SDK on startup and via webhook on individual changes.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.agent import Agent

log = logging.getLogger("agent_repo")


async def upsert_from_odoo(
    db: AsyncSession,
    instance_id: int,
    odoo_agent_id: int,
    technical_name: str,
    is_supervisor: bool,
    god_mode: bool,
    caller_type: str,
    property_scope_ids: list,
    allowed_user_ids: list,
    allowed_agent_names: list,
    active: bool = True,
) -> tuple[Agent, bool]:
    """Create or update an agent row. Returns (agent, created)."""
    now = datetime.now(timezone.utc)
    stmt = pg_insert(Agent).values(
        instance_id=instance_id,
        odoo_agent_id=odoo_agent_id,
        technical_name=technical_name,
        is_supervisor=is_supervisor,
        god_mode=god_mode,
        caller_type=caller_type,
        property_scope_ids=property_scope_ids,
        allowed_user_ids=allowed_user_ids,
        allowed_agent_names=allowed_agent_names,
        active=active,
        synced_at=now,
    ).on_conflict_do_update(
        constraint="uq_agent_instance_odoo_id",
        set_={
            "technical_name": technical_name,
            "is_supervisor": is_supervisor,
            "god_mode": god_mode,
            "caller_type": caller_type,
            "property_scope_ids": property_scope_ids,
            "allowed_user_ids": allowed_user_ids,
            "allowed_agent_names": allowed_agent_names,
            "active": active,
            "synced_at": now,
        },
    ).returning(Agent.id)
    result = await db.execute(stmt)
    agent_id = result.scalar_one()

    # Determine if created (no prior row) — simplified: just fetch
    agent = await db.get(Agent, agent_id)
    created = agent.synced_at == now and agent_id is not None
    return agent, created


async def find_by_instance(db: AsyncSession, instance_id: int) -> list[Agent]:
    result = await db.execute(
        select(Agent).where(Agent.instance_id == instance_id)
    )
    return list(result.scalars().all())


async def find_by_technical_name(
    db: AsyncSession, instance_id: int, technical_name: str,
) -> Agent | None:
    result = await db.execute(
        select(Agent).where(
            Agent.instance_id == instance_id,
            Agent.technical_name == technical_name,
        )
    )
    return result.scalar_one_or_none()


async def count_for_instance(db: AsyncSession, instance_id: int) -> int:
    result = await db.execute(
        select(func.count()).select_from(Agent).where(
            Agent.instance_id == instance_id
        )
    )
    return result.scalar_one()


async def sync_all_from_sdk(
    db: AsyncSession,
    instance_id: int,
    agent_configs: list,
) -> None:
    """Bulk-sync agents from SDK to DB.

    Resolves allowed_agent_ids (Odoo IDs) to allowed_agent_names
    (technical_name strings) using the full agent list.
    """
    id_to_name = {ac.id: ac.technical_name for ac in agent_configs}

    for ac in agent_configs:
        allowed_names = [
            id_to_name[aid] for aid in (ac.allowed_agent_ids or [])
            if aid in id_to_name
        ]
        await upsert_from_odoo(
            db,
            instance_id=instance_id,
            odoo_agent_id=ac.id,
            technical_name=ac.technical_name,
            is_supervisor=ac.is_supervisor,
            god_mode=ac.god_mode,
            caller_type=ac.caller_type,
            property_scope_ids=ac.property_scope_ids or [],
            allowed_user_ids=ac.allowed_user_ids or [],
            allowed_agent_names=allowed_names,
            active=ac.active,
        )

    # Deactivate agents no longer in the active set
    active_ids = {ac.id for ac in agent_configs if ac.active}
    await _deactivate_missing(db, instance_id, active_ids)
    await db.flush()

    log.info(
        "Synced %d agents for instance %d", len(agent_configs), instance_id,
    )


async def _deactivate_missing(
    db: AsyncSession, instance_id: int, active_odoo_ids: set[int],
) -> None:
    """Mark agents as inactive if their odoo_agent_id is no longer active."""
    all_agents = await find_by_instance(db, instance_id)
    for agent in all_agents:
        if agent.odoo_agent_id not in active_odoo_ids and agent.active:
            agent.active = False


async def find_permitted_workers(
    db: AsyncSession,
    instance_id: int,
    caller_type: str,
    odoo_property_id: int | None,
    odoo_user_id: int | None,
    supervisor_technical_name: str,
) -> list[Agent]:
    """Return workers that pass all 5 permission layers.

    Layer 1: caller_type match (agent.caller_type in [caller_type, "any"])
    Layer 2: property_scope (if set, odoo_property_id must be in list)
    Layer 3: allowed_users (if set, odoo_user_id must be identified AND in list)
    Layer 4: delegation (if supervisor has allowed_agent_names, worker must be in list)
    Layer 5: god_mode hard-block for external_guest
    """
    all_agents = await find_by_instance(db, instance_id)

    # Resolve supervisor's delegation list
    supervisor = next(
        (a for a in all_agents if a.technical_name == supervisor_technical_name),
        None,
    )
    sup_allowed = (
        supervisor.allowed_agent_names
        if supervisor and supervisor.allowed_agent_names
        else []
    )

    result = []
    for agent in all_agents:
        if not agent.active:
            continue
        if agent.is_supervisor:
            continue

        # Layer 1: caller_type (roomdoo has access to all)
        if caller_type != "roomdoo" and agent.caller_type not in (caller_type, "any"):
            continue

        # Layer 2: property_scope
        if agent.property_scope_ids and odoo_property_id not in agent.property_scope_ids:
            continue

        # Layer 3: allowed_users
        if agent.allowed_user_ids:
            if odoo_user_id is None or odoo_user_id not in agent.allowed_user_ids:
                continue

        # Layer 4: supervisor delegation
        if sup_allowed and agent.technical_name not in sup_allowed:
            continue

        # Layer 5: god_mode hard-block for external guests
        if agent.god_mode and caller_type == "external_guest":
            continue

        result.append(agent)

    return result
