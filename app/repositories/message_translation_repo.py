from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message_translation import MessageTranslation


async def find(
    db: AsyncSession, message_id: int, language: str
) -> MessageTranslation | None:
    result = await db.execute(
        select(MessageTranslation).where(
            MessageTranslation.message_id == message_id,
            MessageTranslation.language == language,
        )
    )
    return result.scalar_one_or_none()


async def create(
    db: AsyncSession, message_id: int, language: str, content: str
) -> MessageTranslation:
    translation = MessageTranslation(
        message_id=message_id,
        language=language,
        content=content,
    )
    db.add(translation)
    await db.flush()
    return translation


async def find_all_for_message(
    db: AsyncSession, message_id: int
) -> list[MessageTranslation]:
    result = await db.execute(
        select(MessageTranslation).where(
            MessageTranslation.message_id == message_id
        )
    )
    return list(result.scalars().all())
