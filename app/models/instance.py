from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Instance(Base):
    """
    A Roomdoo/Odoo installation. Authentication anchor for all inbound calls from that tenant.
    One instance can have multiple properties (hotels).
    """

    __tablename__ = "instances"

    id: Mapped[int] = mapped_column(primary_key=True)
    instance_url: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    bearer_token: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    bookai_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    default_language: Mapped[str] = mapped_column(String(5), nullable=False, default="es")

    # Roomdoo SDK connection (instance_url is reused as Odoo base URL)
    roomdoo_db: Mapped[str | None] = mapped_column(String(255), nullable=True)
    roomdoo_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    roomdoo_password: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Router LLM credentials (used by AgentSelector to pick the right agent)
    router_llm_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    router_llm_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    router_llm_api_base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    router_llm_model: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        default=func.now(), onupdate=func.now(), nullable=False
    )

    properties: Mapped[list["Property"]] = relationship(back_populates="instance")


class Property(Base):
    """
    A hotel. Belongs to one Instance. Optionally linked to a ChannelEndpoint (WhatsApp number).
    A ChannelEndpoint can be shared by multiple properties (hotel chains).
    """

    __tablename__ = "properties"

    id: Mapped[int] = mapped_column(primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("instances.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    roomdoo_external_code: Mapped[str] = mapped_column(String(255), nullable=False)
    channel_endpoint_id: Mapped[int | None] = mapped_column(
        ForeignKey("channel_endpoints.id"), nullable=True
    )
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        default=func.now(), onupdate=func.now(), nullable=False
    )

    instance: Mapped["Instance"] = relationship(back_populates="properties")
    channel_endpoint: Mapped["ChannelEndpoint | None"] = relationship(
        back_populates="properties"
    )
    attention_sessions: Mapped[list["AttentionSession"]] = relationship(
        back_populates="property"
    )
