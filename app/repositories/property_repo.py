"""
Property repository — CRUD for properties synced from Odoo.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instance import Property


async def find_by_odoo_id(
    db: AsyncSession, odoo_property_id: int, instance_id: int,
) -> Property | None:
    result = await db.execute(
        select(Property).where(
            Property.odoo_property_id == odoo_property_id,
            Property.instance_id == instance_id,
        )
    )
    return result.scalar_one_or_none()


async def find_by_external_code(
    db: AsyncSession, external_code: str, instance_id: int,
) -> Property | None:
    result = await db.execute(
        select(Property).where(
            Property.roomdoo_external_code == external_code,
            Property.instance_id == instance_id,
        )
    )
    return result.scalar_one_or_none()


async def upsert_from_odoo(
    db: AsyncSession,
    instance_id: int,
    odoo_property_id: int,
    name: str,
    external_code: str,
    bookai_mode: str = "disabled",
    tz: str | None = None,
    email: str | None = None,
    phone: str | None = None,
) -> tuple[Property, bool]:
    """Create or update a property from Odoo data.

    Returns (property, created) where created=True if new.
    """
    prop = await find_by_odoo_id(db, odoo_property_id, instance_id)
    if prop is None:
        prop = await find_by_external_code(db, external_code, instance_id)

    created = False
    if prop is None:
        prop = Property(
            instance_id=instance_id,
            odoo_property_id=odoo_property_id,
            name=name,
            roomdoo_external_code=external_code,
            bookai_mode=bookai_mode,
            tz=tz,
            email=email,
            phone=phone,
        )
        db.add(prop)
        created = True
    else:
        prop.odoo_property_id = odoo_property_id
        prop.name = name
        prop.roomdoo_external_code = external_code
        prop.bookai_mode = bookai_mode
        prop.tz = tz
        prop.email = email
        prop.phone = phone

    await db.flush()
    return prop, created
