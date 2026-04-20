from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class WhatsAppTemplate(Base):
    """
    Concept-level WhatsApp template. Represents a template family identified by a single
    internal code. Each language variant is stored as a WhatsAppTemplateTranslation.

    - code: internal identifier used by Roomdoo when requesting a template send.
    """

    __tablename__ = "whatsapp_templates"
    __table_args__ = (
        UniqueConstraint("code", "instance_id", name="uq_template_code_instance"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    instance_id: Mapped[int] = mapped_column(
        ForeignKey("instances.id"), nullable=False,
    )
    code: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(
        String(20), nullable=False, default="UTILITY",
    )  # UTILITY | MARKETING | AUTHENTICATION
    created_at: Mapped[datetime] = mapped_column(default=func.now(), nullable=False)

    instance: Mapped["Instance"] = relationship()

    translations: Mapped[list["WhatsAppTemplateTranslation"]] = relationship(
        back_populates="template", cascade="all, delete-orphan"
    )


class WhatsAppTemplateTranslation(Base):
    """
    A single language variant of a WhatsApp template registered on the Meta Business Platform.

    Templates are pre-approved by the channel provider per language. Each translation
    carries the provider-facing name and component structure for that specific language.

    - whatsapp_name: the actual name on the Meta platform for this language variant.
    - language: BCP-47 tag (e.g. 'es', 'en', 'zh').
    - components: the Meta-format component structure (header, body, footer, buttons).
    - active: false means this variant is disabled without deleting the row.
    """

    __tablename__ = "whatsapp_template_translations"
    __table_args__ = (
        UniqueConstraint("template_id", "language", name="uq_template_translation_lang"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    template_id: Mapped[int] = mapped_column(
        ForeignKey("whatsapp_templates.id", ondelete="CASCADE"), nullable=False
    )
    whatsapp_name: Mapped[str] = mapped_column(String(255), nullable=False)
    language: Mapped[str] = mapped_column(String(10), nullable=False, default="es")
    components: Mapped[dict] = mapped_column(JSONB, nullable=False, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Meta Cloud API state
    meta_template_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    meta_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft",
    )  # draft | pending | approved | rejected | disabled

    # Template text with placeholders (for Meta creation, different from components)
    header_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    footer_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    button_texts: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(default=func.now(), nullable=False)

    template: Mapped["WhatsAppTemplate"] = relationship(back_populates="translations")
    translation_properties: Mapped[list["TemplateTranslationProperty"]] = relationship(
        back_populates="translation", cascade="all, delete-orphan"
    )


class TemplateTranslationProperty(Base):
    """
    Junction table: which template translations are available for which properties.

    Scoped at the translation level (not the template level) because a property may only
    support certain languages of a given template. A hotel that only operates in Spanish
    would only have the 'es' translation linked, even if the template also has 'en' and 'zh'.
    """

    __tablename__ = "template_translation_properties"

    translation_id: Mapped[int] = mapped_column(
        ForeignKey("whatsapp_template_translations.id", ondelete="CASCADE"), primary_key=True
    )
    property_id: Mapped[int] = mapped_column(
        ForeignKey("properties.id", ondelete="CASCADE"), primary_key=True
    )

    translation: Mapped["WhatsAppTemplateTranslation"] = relationship(
        back_populates="translation_properties"
    )
