"""
Persisted agent configuration — security source of truth.

Synced from Odoo via SDK on startup and via webhook on changes.
The in-memory AgentLoader cache holds operational data (prompts, KB docs, tools).
This table holds security-critical fields that gate worker delegation.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(primary_key=True)
    instance_id: Mapped[int] = mapped_column(
        ForeignKey("instances.id"), nullable=False,
    )
    odoo_agent_id: Mapped[int] = mapped_column(Integer, nullable=False)
    technical_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_supervisor: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    god_mode: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    caller_type: Mapped[str] = mapped_column(String(50), nullable=False, default="any")

    # Permissions — empty list means "no restriction"
    property_scope_ids: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    allowed_user_ids: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    allowed_agent_names: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("instance_id", "odoo_agent_id", name="uq_agent_instance_odoo_id"),
        UniqueConstraint("instance_id", "technical_name", name="uq_agent_instance_name"),
    )
