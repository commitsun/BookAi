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
    wa_phone_number_id: str | None = None,
    wa_access_token: str | None = None,
    wa_account_id: str | None = None,
    wa_verify_token: str | None = None,
    wa_display_number: str | None = None,
) -> tuple[Property, bool]:
    """Create or update a property from Odoo data.

    If WhatsApp channel data is provided, creates/updates the ChannelEndpoint
    and links it to the property automatically.

    Returns (property, created) where created=True if new.
    """
    prop = await find_by_odoo_id(db, odoo_property_id, instance_id)
    if prop is None and external_code:
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

    # Sync WhatsApp channel endpoint
    if wa_phone_number_id and wa_access_token:
        endpoint = await _upsert_channel_endpoint(
            db, wa_phone_number_id, wa_access_token,
            wa_account_id, wa_verify_token, wa_display_number,
        )
        prop.channel_endpoint_id = endpoint.id
    elif not wa_phone_number_id:
        prop.channel_endpoint_id = None

    await db.flush()
    return prop, created


async def _upsert_channel_endpoint(
    db: AsyncSession,
    phone_number_id: str,
    access_token: str,
    account_id: str | None = None,
    verify_token: str | None = None,
    display_number: str | None = None,
) -> "ChannelEndpoint":
    """Create or update a WhatsApp ChannelEndpoint by phone_number_id."""
    from app.models.channel import ChannelEndpoint

    result = await db.execute(
        select(ChannelEndpoint).where(
            ChannelEndpoint.external_code == phone_number_id,
        )
    )
    endpoint = result.scalar_one_or_none()

    if endpoint is None:
        endpoint = ChannelEndpoint(
            channel="whatsapp",
            external_code=phone_number_id,
            access_token=access_token,
            account_id=account_id,
            verify_token=verify_token,
            display_number=display_number,
        )
        db.add(endpoint)
    else:
        endpoint.access_token = access_token
        if account_id:
            endpoint.account_id = account_id
        if verify_token:
            endpoint.verify_token = verify_token
        if display_number:
            endpoint.display_number = display_number

    await db.flush()
    return endpoint
