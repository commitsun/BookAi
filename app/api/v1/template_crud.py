"""
POST  /api/v1/whatsapp/templates        — create a template with translations
PATCH /api/v1/whatsapp/templates/{code} — update translations of an existing template
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status as http_status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_instance
from app.models.instance import Instance
from app.repositories import template_repo

log = logging.getLogger("template_crud")

router = APIRouter(prefix="/whatsapp/templates", tags=["templates"])


# ── Schemas ──────────────────────────────────────────────────────────

class TranslationInput(BaseModel):
    whatsapp_name: str = Field(..., description="Meta platform template name")
    language: str = Field(default="es", description="BCP-47 language tag")
    components: list[dict] = Field(
        default_factory=list,
        description="Meta-format component array (header, body, footer, buttons)",
    )
    active: bool = True
    property_ids: list[int] = Field(
        default_factory=list,
        description="Properties this translation is available for",
    )


class CreateTemplateRequest(BaseModel):
    code: str = Field(..., description="Internal template code (unique per instance)")
    translations: list[TranslationInput] = Field(
        ..., min_length=1,
        description="At least one language translation",
    )


class TranslationPatchInput(BaseModel):
    whatsapp_name: str = Field(..., description="Meta platform template name")
    language: str = Field(default="es", description="BCP-47 language tag")
    components: list[dict] | None = Field(
        default=None,
        description="If provided, replaces components. If null/absent, keeps existing.",
    )
    active: bool | None = None
    property_ids: list[int] | None = Field(
        default=None,
        description="If provided, replaces property bindings. If null/absent, keeps existing.",
    )


class UpdateTemplateRequest(BaseModel):
    translations: list[TranslationPatchInput] = Field(
        ..., min_length=1,
        description="Translations to upsert (by language)",
    )


class TranslationOut(BaseModel):
    id: int
    whatsapp_name: str
    language: str
    components: list[dict]
    active: bool
    property_ids: list[int]


class TemplateOut(BaseModel):
    id: int
    code: str
    instance_id: int
    translations: list[TranslationOut]


# ── POST — create ────────────────────────────────────────────────────


@router.post(
    "",
    response_model=TemplateOut,
    status_code=201,
    summary="Create a WhatsApp template",
    description=(
        "Creates a template scoped to the authenticated instance, "
        "with one or more language translations. Each translation "
        "can be linked to specific properties."
    ),
    responses={
        409: {"description": "Template code already exists for this instance"},
    },
)
async def create_template(
    body: CreateTemplateRequest,
    instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
) -> TemplateOut:
    existing = await template_repo.find_by_code_and_instance(
        db, body.code, instance.id,
    )
    if existing:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"Template '{body.code}' already exists for this instance",
        )

    template = await template_repo.create_template(
        db,
        instance_id=instance.id,
        code=body.code,
        translations=[t.model_dump() for t in body.translations],
    )
    await db.commit()

    # Reload with relationships
    template = await template_repo.find_by_code_and_instance(
        db, body.code, instance.id,
    )
    return _to_out(template)


# ── PATCH — update ───────────────────────────────────────────────────


@router.patch(
    "/{code}",
    response_model=TemplateOut,
    summary="Update a WhatsApp template's translations",
    description=(
        "Upserts translations by language: existing translations are "
        "updated, new languages are added. property_ids replaces "
        "the existing bindings completely for each translation."
    ),
    responses={
        404: {"description": "Template not found for this instance"},
    },
)
async def update_template(
    code: str,
    body: UpdateTemplateRequest,
    instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
) -> TemplateOut:
    template = await template_repo.find_by_code_and_instance(
        db, code, instance.id,
    )
    if template is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Template '{code}' not found for this instance",
        )

    await template_repo.upsert_translations(
        db, template,
        [t.model_dump(exclude_unset=True) for t in body.translations],
    )
    await db.commit()

    # Reload with fresh relationships
    template = await template_repo.find_by_code_and_instance(
        db, code, instance.id,
    )
    return _to_out(template)


# ── Helpers ──────────────────────────────────────────────────────────

def _to_out(template) -> TemplateOut:
    return TemplateOut(
        id=template.id,
        code=template.code,
        instance_id=template.instance_id,
        translations=[
            TranslationOut(
                id=t.id,
                whatsapp_name=t.whatsapp_name,
                language=t.language,
                components=t.components,
                active=t.active,
                property_ids=[tp.property_id for tp in t.translation_properties],
            )
            for t in template.translations
        ],
    )
