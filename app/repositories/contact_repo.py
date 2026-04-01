from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact import Contact


async def get_or_create(
    db: AsyncSession,
    phone_code: str,
    display_name: str | None = None,
) -> tuple[Contact, bool]:
    result = await db.execute(select(Contact).where(Contact.phone_code == phone_code))
    contact = result.scalar_one_or_none()
    if contact:
        if display_name and not contact.display_name:
            contact.display_name = display_name
        return contact, False
    contact = Contact(phone_code=phone_code, display_name=display_name)
    db.add(contact)
    await db.flush()
    return contact, True
