from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import (
    DeliveryStatus,
    Message,
    MessageDirection,
    MessageKind,
    MessageSender,
)


async def find_by_idempotency_key(db: AsyncSession, key: str) -> Message | None:
    result = await db.execute(select(Message).where(Message.idempotency_key == key))
    return result.scalar_one_or_none()


async def find_by_provider_message_id(db: AsyncSession, wa_message_id: str) -> Message | None:
    result = await db.execute(
        select(Message).where(Message.wa_message_id == wa_message_id)
    )
    return result.scalar_one_or_none()


async def create(db: AsyncSession, **kwargs) -> Message:
    msg = Message(**kwargs)
    db.add(msg)
    await db.flush()
    return msg


async def create_note(
    db: AsyncSession,
    conversation_id: int,
    attention_session_id: int,
    sender: MessageSender,
    content: str,
    content_language: str | None = None,
    template_code: str | None = None,
    template_payload: dict | None = None,
) -> Message:
    """Create an internal note — never sent to the channel."""
    return await create(
        db,
        conversation_id=conversation_id,
        attention_session_id=attention_session_id,
        kind=MessageKind.note,
        direction=MessageDirection.outbound,
        sender=sender,
        content=content,
        content_language=content_language,
        template_code=template_code,
        template_payload=template_payload,
        wa_message_type="note",
        delivery_status=DeliveryStatus.skipped,
    )


async def update_delivery(
    db: AsyncSession,
    message: Message,
    status: DeliveryStatus,
    wa_message_id: str | None = None,
    error: str | None = None,
) -> None:
    message.delivery_status = status
    if wa_message_id is not None:
        message.wa_message_id = wa_message_id
    if error is not None:
        message.delivery_error = error
