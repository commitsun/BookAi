from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.email_message import EmailAttachment, EmailMessageMetadata
from app.models.message import Message


async def find_last_in_conversation(
    db: AsyncSession, conversation_id: int
) -> EmailMessageMetadata | None:
    """
    Returns the most recent EmailMessageMetadata for any email message in the
    conversation. Used to build In-Reply-To / References headers for outbound replies.
    """
    result = await db.execute(
        select(EmailMessageMetadata)
        .join(EmailMessageMetadata.message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def find_by_provider_message_id(
    db: AsyncSession, provider_message_id: str
) -> EmailMessageMetadata | None:
    result = await db.execute(
        select(EmailMessageMetadata)
        .where(EmailMessageMetadata.provider_message_id == provider_message_id)
        .options(
            selectinload(EmailMessageMetadata.message).selectinload(
                Message.conversation
            )
        )
    )
    return result.scalar_one_or_none()


async def find_recent_by_subject_and_endpoint(
    db: AsyncSession,
    normalized_subject: str,
    channel_endpoint_id: int,
    hours: int = 72,
) -> EmailMessageMetadata | None:
    """
    Strategy 3 (tertiary threading): find a recent email metadata row whose
    normalized subject matches, within the same channel endpoint and time window.
    Returns the most recent match.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    result = await db.execute(
        select(EmailMessageMetadata)
        .join(EmailMessageMetadata.message)
        .where(
            EmailMessageMetadata.subject == normalized_subject,
            Message.channel_endpoint_id == channel_endpoint_id,
            EmailMessageMetadata.created_at >= cutoff,
        )
        .options(
            selectinload(EmailMessageMetadata.message).selectinload(
                Message.conversation
            )
        )
        .order_by(EmailMessageMetadata.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def create(
    db: AsyncSession,
    message_id: int,
    **kwargs,
) -> EmailMessageMetadata:
    meta = EmailMessageMetadata(message_id=message_id, **kwargs)
    db.add(meta)
    await db.flush()
    return meta


async def create_attachment(
    db: AsyncSession,
    message_id: int,
    email_metadata_id: int,
    filename: str,
    content_type: str,
    storage_key: str,
    size_bytes: int | None = None,
    inline: bool = False,
    content_id: str | None = None,
) -> EmailAttachment:
    attachment = EmailAttachment(
        message_id=message_id,
        email_metadata_id=email_metadata_id,
        filename=filename,
        content_type=content_type,
        storage_key=storage_key,
        size_bytes=size_bytes,
        inline=inline,
        content_id=content_id,
    )
    db.add(attachment)
    await db.flush()
    return attachment


# ---------------------------------------------------------------------------
# Threading helpers
# ---------------------------------------------------------------------------

_MESSAGE_ID_RE = re.compile(r"<[^>]+>")


def parse_message_ids(in_reply_to: str | None, references: str | None) -> list[str]:
    """Extract all RFC 2822 Message-IDs from In-Reply-To and References headers."""
    ids: list[str] = []
    for header in (in_reply_to, references):
        if header:
            ids.extend(_MESSAGE_ID_RE.findall(header))
    # deduplicate preserving order
    seen: set[str] = set()
    result = []
    for mid in ids:
        if mid not in seen:
            seen.add(mid)
            result.append(mid)
    return result


_SUBJECT_RE = re.compile(r"^\s*(re|fwd?|aw|sv|vs|ref)\s*:\s*", re.IGNORECASE)


def normalize_subject(subject: str | None) -> str:
    """
    Strip reply/forward prefixes and normalise whitespace for subject-based threading.
    Returns empty string if the subject is None or becomes empty after stripping.
    """
    if not subject:
        return ""
    s = subject.strip()
    while True:
        new = _SUBJECT_RE.sub("", s).strip()
        if new == s:
            break
        s = new
    return s.lower()
