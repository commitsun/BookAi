from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.channel import ChannelEndpoint
from app.models.instance import Instance, Property


async def find_by_bearer_token(
    db: AsyncSession, token: str
) -> Instance | None:
    result = await db.execute(
        select(Instance).where(
            Instance.bearer_token == token,
            Instance.active.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def find_property_by_roomdoo_code(
    db: AsyncSession, roomdoo_external_code: str, instance_id: int
) -> Property | None:
    result = await db.execute(
        select(Property).where(
            Property.roomdoo_external_code == roomdoo_external_code,
            Property.instance_id == instance_id,
        )
    )
    return result.scalar_one_or_none()


async def find_channel_endpoint_by_external_code(
    db: AsyncSession, external_code: str
) -> ChannelEndpoint | None:
    result = await db.execute(
        select(ChannelEndpoint).where(
            ChannelEndpoint.external_code == external_code
        )
    )
    return result.scalar_one_or_none()


async def find_channel_endpoint_by_verify_token(
    db: AsyncSession, verify_token: str
) -> ChannelEndpoint | None:
    result = await db.execute(
        select(ChannelEndpoint).where(
            ChannelEndpoint.verify_token == verify_token
        )
    )
    return result.scalar_one_or_none()


async def find_property_by_id(
    db: AsyncSession, property_id: int, instance_id: int
) -> Property | None:
    result = await db.execute(
        select(Property).where(
            Property.id == property_id,
            Property.instance_id == instance_id,
        )
    )
    return result.scalar_one_or_none()


async def find_properties_with_channel(
    db: AsyncSession,
    instance_id: int,
) -> list[Property]:
    """
    All properties in the instance that have a WhatsApp channel assigned.
    These are valid transfer destinations: the destination hotel can send
    a template to re-engage the guest even if the number differs.
    """
    result = await db.execute(
        select(Property).where(
            Property.instance_id == instance_id,
            Property.channel_endpoint_id.is_not(None),
        )
    )
    return list(result.scalars().all())


async def find_channel_endpoint_by_id(
    db: AsyncSession, endpoint_id: int
) -> ChannelEndpoint | None:
    result = await db.execute(
        select(ChannelEndpoint).where(ChannelEndpoint.id == endpoint_id)
    )
    return result.scalar_one_or_none()
