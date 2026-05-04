from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.template import (
    TemplateTranslationProperty,
    TemplateTranslationWaba,
    WhatsAppTemplate,
    WhatsAppTemplateTranslation,
)


async def find_by_code_and_instance(
    db: AsyncSession, code: str, instance_id: int,
) -> WhatsAppTemplate | None:
    result = await db.execute(
        select(WhatsAppTemplate)
        .options(
            selectinload(WhatsAppTemplate.translations)
            .selectinload(WhatsAppTemplateTranslation.translation_properties)
        )
        .where(
            WhatsAppTemplate.code == code,
            WhatsAppTemplate.instance_id == instance_id,
        )
    )
    return result.scalar_one_or_none()


async def create_template(
    db: AsyncSession,
    instance_id: int,
    code: str,
    translations: list[dict],
    category: str = "UTILITY",
) -> WhatsAppTemplate:
    """Create a template with translations and property bindings."""
    template = WhatsAppTemplate(
        instance_id=instance_id, code=code, category=category,
    )
    db.add(template)
    await db.flush()

    for t in translations:
        trans = WhatsAppTemplateTranslation(
            template_id=template.id,
            whatsapp_name=t["whatsapp_name"],
            language=t.get("language", "es"),
            components=t.get("components", []),
            active=t.get("active", True),
            body_text=t.get("body_text"),
            header_text=t.get("header_text"),
            footer_text=t.get("footer_text"),
            button_texts=t.get("button_texts"),
            parameters=t.get("parameters"),
            body_example=t.get("body_example"),
            header_example=t.get("header_example"),
        )
        db.add(trans)
        await db.flush()
        for pid in t.get("property_ids", []):
            db.add(TemplateTranslationProperty(
                translation_id=trans.id, property_id=pid,
            ))

    await db.flush()
    return template


async def upsert_translations(
    db: AsyncSession,
    template: WhatsAppTemplate,
    translations: list[dict],
) -> None:
    """Upsert translations on an existing template.

    For each item: if language exists → update; if not → create.
    property_ids replaces existing bindings completely.
    """
    existing_by_lang: dict[str, WhatsAppTemplateTranslation] = {
        t.language: t for t in template.translations
    }

    for t in translations:
        lang = t.get("language", "es")
        trans = existing_by_lang.get(lang)

        if trans:
            trans.whatsapp_name = t["whatsapp_name"]
            if "components" in t:
                trans.components = t["components"]
            if "active" in t:
                trans.active = t["active"]
            if "body_text" in t:
                trans.body_text = t["body_text"]
            if "header_text" in t:
                trans.header_text = t["header_text"]
            if "footer_text" in t:
                trans.footer_text = t["footer_text"]
            if "button_texts" in t:
                trans.button_texts = t["button_texts"]
            if "parameters" in t:
                trans.parameters = t["parameters"]
            if "body_example" in t:
                trans.body_example = t["body_example"]
            if "header_example" in t:
                trans.header_example = t["header_example"]
            await db.flush()
        else:
            trans = WhatsAppTemplateTranslation(
                template_id=template.id,
                whatsapp_name=t.get("whatsapp_name", ""),
                language=lang,
                components=t.get("components", []),
                active=t.get("active", True),
                body_text=t.get("body_text"),
                header_text=t.get("header_text"),
                footer_text=t.get("footer_text"),
                button_texts=t.get("button_texts"),
                parameters=t.get("parameters"),
                body_example=t.get("body_example"),
                header_example=t.get("header_example"),
            )
            db.add(trans)
            await db.flush()

        # Replace property bindings
        if "property_ids" in t:
            await db.execute(
                delete(TemplateTranslationProperty).where(
                    TemplateTranslationProperty.translation_id == trans.id,
                )
            )
            for pid in t["property_ids"]:
                db.add(TemplateTranslationProperty(
                    translation_id=trans.id, property_id=pid,
                ))

    await db.flush()


async def find_translation_for_property_by_prefix(
    db: AsyncSession,
    code: str,
    lang_prefix: str,
    property_id: int,
) -> WhatsAppTemplateTranslation | None:
    """Fallback: find translation whose language starts with prefix (e.g. 'en' matches 'en_US')."""
    result = await db.execute(
        select(WhatsAppTemplateTranslation)
        .join(WhatsAppTemplate, WhatsAppTemplate.id == WhatsAppTemplateTranslation.template_id)
        .join(
            TemplateTranslationProperty,
            TemplateTranslationProperty.translation_id == WhatsAppTemplateTranslation.id,
        )
        .where(
            WhatsAppTemplate.code == code,
            WhatsAppTemplateTranslation.language.like(f"{lang_prefix}_%"),
            WhatsAppTemplateTranslation.active.is_(True),
            TemplateTranslationProperty.property_id == property_id,
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


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


async def upsert_waba_entry(
    db: AsyncSession,
    translation_id: int,
    waba_id: str,
    meta_template_id: str | None = None,
    meta_status: str = "draft",
) -> TemplateTranslationWaba:
    """Create or update a WABA entry for a translation."""
    result = await db.execute(
        select(TemplateTranslationWaba).where(
            TemplateTranslationWaba.translation_id == translation_id,
            TemplateTranslationWaba.waba_id == waba_id,
        )
    )
    entry = result.scalar_one_or_none()

    if entry:
        if meta_template_id:
            entry.meta_template_id = meta_template_id
        if meta_status != "draft":
            entry.meta_status = meta_status
    else:
        entry = TemplateTranslationWaba(
            translation_id=translation_id,
            waba_id=waba_id,
            meta_template_id=meta_template_id,
            meta_status=meta_status,
        )
        db.add(entry)

    await db.flush()
    return entry


async def find_waba_entries(
    db: AsyncSession, translation_id: int,
) -> list[TemplateTranslationWaba]:
    result = await db.execute(
        select(TemplateTranslationWaba).where(
            TemplateTranslationWaba.translation_id == translation_id,
        )
    )
    return list(result.scalars().all())
