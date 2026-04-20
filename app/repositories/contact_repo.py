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


async def find_by_email(db: AsyncSession, email: str) -> Contact | None:
    result = await db.execute(
        select(Contact).where(Contact.email == email.lower())
    )
    return result.scalar_one_or_none()


async def get_or_create_by_email(
    db: AsyncSession,
    email: str,
    display_name: str | None = None,
) -> tuple[Contact, bool]:
    """
    Look up a contact by email address (case-insensitive). Creates one if not found.

    The contact may have no phone_code yet (email-only contacts in Phase 1 are
    stored with a synthetic placeholder so the NOT NULL constraint is satisfied).
    """
    normalized = email.lower()
    contact = await find_by_email(db, normalized)
    if contact:
        if display_name and not contact.display_name:
            contact.display_name = display_name
        return contact, False
    # Synthetic phone_code so the NOT NULL / unique constraint is satisfied.
    # Prefix "email:" makes collisions with real phone numbers impossible.
    synthetic_phone = f"email:{normalized}"
    contact = Contact(
        phone_code=synthetic_phone,
        email=normalized,
        display_name=display_name,
    )
    db.add(contact)
    await db.flush()
    return contact, True
