from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.template import (
    TemplateTranslationProperty,
    WhatsAppTemplate,
    WhatsAppTemplateTranslation,
)


async def find_translation_for_property(
    db: AsyncSession,
    code: str,
    language: str,
    property_id: int,
) -> WhatsAppTemplateTranslation | None:
    """
    Find the active translation for a template (code + language) scoped to a property.

    A translation is available for a property only if a TemplateTranslationProperty row
    links them. This allows different properties to support different language subsets
    of the same template.
    """
    result = await db.execute(
        select(WhatsAppTemplateTranslation)
        .join(WhatsAppTemplate, WhatsAppTemplate.id == WhatsAppTemplateTranslation.template_id)
        .join(
            TemplateTranslationProperty,
            TemplateTranslationProperty.translation_id == WhatsAppTemplateTranslation.id,
        )
        .where(
            WhatsAppTemplate.code == code,
            WhatsAppTemplateTranslation.language == language,
            WhatsAppTemplateTranslation.active.is_(True),
            TemplateTranslationProperty.property_id == property_id,
        )
    )
    return result.scalar_one_or_none()
