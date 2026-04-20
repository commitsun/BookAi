"""
Seed email channel demo data.

Creates:
  - An email ChannelEndpoint (mock_mode=True) for demo use
  - Email addresses on contacts 1 (María García) and 2 (Jean Dupont)
  - Mixed WA + email messages in conversations 1 and 2
  - Email-only contact Sophie Martin with her own conversation

Run with:
  docker exec bookai python scripts/seed_email_demo.py
"""

import asyncio

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.channel import ChannelEndpoint
from app.models.contact import Contact
from app.models.conversation import Conversation, ConversationChannelState
from app.models.email_message import EmailMessageMetadata
from app.models.instance import Property
from app.models.message import (
    DeliveryStatus,
    Message,
    MessageDirection,
    MessageKind,
    MessageSender,
)
from app.models.session import AttentionSession, SessionStatus

EMAIL_ENDPOINT_CODE = "reservas@demo-bookai.com"
EMAIL_ENDPOINT_DISPLAY = "Hotel Demo Reservas"


async def _get_or_create_email_endpoint(db) -> ChannelEndpoint:
    ep = (
        await db.execute(
            select(ChannelEndpoint).where(
                ChannelEndpoint.external_code == EMAIL_ENDPOINT_CODE
            )
        )
    ).scalar_one_or_none()
    if ep is None:
        ep = ChannelEndpoint(
            channel="email",
            external_code=EMAIL_ENDPOINT_CODE,
            access_token="mock-email-no-token",
            mock_mode=True,
            display_number=EMAIL_ENDPOINT_DISPLAY,
            config={
                "mailgun_domain": "mg.demo-bookai.com",
                "api_key": "key-demo-mock",
                "signing_key": "signing-key-demo-mock",
            },
        )
        db.add(ep)
        await db.flush()
        print(f"  Created email endpoint id={ep.id} ({EMAIL_ENDPOINT_CODE})")
    else:
        print(f"  Email endpoint already exists id={ep.id}")
    return ep


async def _set_contact_email(db, contact_id: int, email: str) -> Contact:
    contact = (
        await db.execute(select(Contact).where(Contact.id == contact_id))
    ).scalar_one()
    if contact.email != email:
        contact.email = email
        print(f"  Set contact id={contact_id} ({contact.display_name}) email={email}")
    return contact


async def _ensure_channel_state(db, conv_id: int, ep_id: int) -> None:
    exists = (
        await db.execute(
            select(ConversationChannelState).where(
                ConversationChannelState.conversation_id == conv_id,
                ConversationChannelState.channel_endpoint_id == ep_id,
            )
        )
    ).scalar_one_or_none()
    if exists is None:
        db.add(
            ConversationChannelState(
                conversation_id=conv_id,
                channel_endpoint_id=ep_id,
            )
        )
        await db.flush()


async def _add_email_msg(
    db,
    *,
    conv_id: int,
    ep_id: int,
    session_id: int | None,
    direction: MessageDirection,
    sender: MessageSender,
    content: str,
    subject: str,
    from_address: str,
    to_address: str,
    provider_message_id: str,
    in_reply_to: str | None = None,
    references: str | None = None,
    delivery_status: DeliveryStatus = DeliveryStatus.delivered,
) -> Message:
    msg = Message(
        conversation_id=conv_id,
        channel_endpoint_id=ep_id,
        attention_session_id=session_id,
        kind=MessageKind.message,
        direction=direction,
        sender=sender,
        content=content,
        delivery_status=delivery_status,
        wa_message_type="email",
    )
    db.add(msg)
    await db.flush()

    to_entries = [{"email": to_address, "name": ""}]
    meta = EmailMessageMetadata(
        message_id=msg.id,
        provider_message_id=provider_message_id,
        in_reply_to=in_reply_to,
        references=references,
        subject=subject,
        from_address=from_address,
        to_addresses=to_entries,
        text_body=content,
    )
    db.add(meta)
    await db.flush()
    return msg


async def _active_session_id(db, conv_id: int) -> int | None:
    session = (
        await db.execute(
            select(AttentionSession).where(
                AttentionSession.conversation_id == conv_id,
                AttentionSession.status == SessionStatus.active,
            )
        )
    ).scalars().first()
    return session.id if session else None


async def main() -> None:
    async with SessionLocal() as db:

        # ── Email channel endpoint ──────────────────────────────────────────
        print("Setting up email channel endpoint...")
        ep = await _get_or_create_email_endpoint(db)

        # ── Contact email addresses ─────────────────────────────────────────
        print("Adding email addresses to contacts...")
        await _set_contact_email(db, 1, "maria.garcia@demo.com")
        await _set_contact_email(db, 2, "jean.dupont@demo.com")

        # ── Conv 1 — María García: mixed WA + email ─────────────────────────
        print("Seeding Conv 1 (María García) — mixed WA + email...")
        await _ensure_channel_state(db, 1, ep.id)
        sid1 = await _active_session_id(db, 1)

        await _add_email_msg(
            db,
            conv_id=1,
            ep_id=ep.id,
            session_id=sid1,
            direction=MessageDirection.outbound,
            sender=MessageSender.system,
            content=(
                "Estimada María, adjuntamos la confirmación de su reserva "
                "para el 1 de mayo. El check-in es a las 15:00h. ¡Le esperamos!"
            ),
            subject="Confirmación de reserva – Hotel Demo",
            from_address=EMAIL_ENDPOINT_CODE,
            to_address="maria.garcia@demo.com",
            provider_message_id="<demo-email-001@mg.demo-bookai.com>",
            delivery_status=DeliveryStatus.delivered,
        )

        await _add_email_msg(
            db,
            conv_id=1,
            ep_id=ep.id,
            session_id=sid1,
            direction=MessageDirection.inbound,
            sender=MessageSender.guest,
            content=(
                "Muchas gracias por la confirmación. "
                "¿Ofrecen servicio de parking en el hotel?"
            ),
            subject="Re: Confirmación de reserva – Hotel Demo",
            from_address="maria.garcia@demo.com",
            to_address=EMAIL_ENDPOINT_CODE,
            provider_message_id="<demo-email-002@gmail.com>",
            in_reply_to="<demo-email-001@mg.demo-bookai.com>",
            references="<demo-email-001@mg.demo-bookai.com>",
            delivery_status=DeliveryStatus.delivered,
        )

        # ── Conv 2 — Jean Dupont: mixed WA + email ──────────────────────────
        print("Seeding Conv 2 (Jean Dupont) — mixed WA + email...")
        await _ensure_channel_state(db, 2, ep.id)
        sid2 = await _active_session_id(db, 2)

        await _add_email_msg(
            db,
            conv_id=2,
            ep_id=ep.id,
            session_id=sid2,
            direction=MessageDirection.outbound,
            sender=MessageSender.system,
            content=(
                "Bonjour Jean, voici le récapitulatif de vos dates modifiées : "
                "arrivée le 10 mai, départ le 14 mai. Cordialement."
            ),
            subject="Modification de votre réservation – Hotel Demo",
            from_address=EMAIL_ENDPOINT_CODE,
            to_address="jean.dupont@demo.com",
            provider_message_id="<demo-email-003@mg.demo-bookai.com>",
            delivery_status=DeliveryStatus.delivered,
        )

        await _add_email_msg(
            db,
            conv_id=2,
            ep_id=ep.id,
            session_id=sid2,
            direction=MessageDirection.inbound,
            sender=MessageSender.guest,
            content=(
                "Merci pour la mise à jour. Avez-vous des chambres communicantes "
                "disponibles pour ces dates?"
            ),
            subject="Re: Modification de votre réservation – Hotel Demo",
            from_address="jean.dupont@demo.com",
            to_address=EMAIL_ENDPOINT_CODE,
            provider_message_id="<demo-email-004@gmail.com>",
            in_reply_to="<demo-email-003@mg.demo-bookai.com>",
            references="<demo-email-003@mg.demo-bookai.com>",
            delivery_status=DeliveryStatus.delivered,
        )

        # ── Sophie Martin: email-only contact + conversation ────────────────
        print("Creating Sophie Martin (email-only contact)...")
        sophie_email = "sophie.martin@demo.com"
        sophie_phone = f"email:{sophie_email}"

        sophie = (
            await db.execute(
                select(Contact).where(Contact.phone_code == sophie_phone)
            )
        ).scalar_one_or_none()
        if sophie is None:
            sophie = Contact(
                phone_code=sophie_phone,
                email=sophie_email,
                display_name="Sophie Martin",
            )
            db.add(sophie)
            await db.flush()
            print(f"  Created contact id={sophie.id}")

        sophie_conv = (
            await db.execute(
                select(Conversation).where(Conversation.contact_id == sophie.id)
            )
        ).scalar_one_or_none()
        if sophie_conv is None:
            sophie_conv = Conversation(contact_id=sophie.id)
            db.add(sophie_conv)
            await db.flush()
            print(f"  Created conversation id={sophie_conv.id}")

        await _ensure_channel_state(db, sophie_conv.id, ep.id)

        # Attach Sophie's conversation to property 1 (Hotel Costa Brava)
        prop1 = (
            await db.execute(
                select(Property).where(
                    Property.roomdoo_external_code == "HCBR01"
                )
            )
        ).scalar_one()

        sophie_session = (
            await db.execute(
                select(AttentionSession).where(
                    AttentionSession.conversation_id == sophie_conv.id,
                    AttentionSession.status == SessionStatus.active,
                )
            )
        ).scalars().first()
        if sophie_session is None:
            sophie_session = AttentionSession(
                conversation_id=sophie_conv.id,
                property_id=prop1.id,
                status=SessionStatus.active,
            )
            db.add(sophie_session)
            await db.flush()

        sid_sophie = sophie_session.id

        await _add_email_msg(
            db,
            conv_id=sophie_conv.id,
            ep_id=ep.id,
            session_id=sid_sophie,
            direction=MessageDirection.inbound,
            sender=MessageSender.guest,
            content=(
                "Bonjour, je souhaiterais réserver une chambre double "
                "pour les dates du 15 au 18 juin. Avez-vous des disponibilités?"
            ),
            subject="Demande de disponibilité – juin",
            from_address=sophie_email,
            to_address=EMAIL_ENDPOINT_CODE,
            provider_message_id="<demo-email-005@gmail.com>",
            delivery_status=DeliveryStatus.delivered,
        )

        await _add_email_msg(
            db,
            conv_id=sophie_conv.id,
            ep_id=ep.id,
            session_id=sid_sophie,
            direction=MessageDirection.outbound,
            sender=MessageSender.agent,
            content=(
                "Bonjour Sophie, oui nous avons des disponibilités pour ces dates. "
                "Je vous envoie une offre personnalisée dans les prochaines heures."
            ),
            subject="Re: Demande de disponibilité – juin",
            from_address=EMAIL_ENDPOINT_CODE,
            to_address=sophie_email,
            provider_message_id="<demo-email-006@mg.demo-bookai.com>",
            in_reply_to="<demo-email-005@gmail.com>",
            references="<demo-email-005@gmail.com>",
            delivery_status=DeliveryStatus.sent,
        )

        await db.commit()
        print()
        print("✅ Email demo data seeded successfully.")
        print(f"   Email endpoint id: {ep.id}  ({EMAIL_ENDPOINT_CODE})")
        print(f"   Sophie Martin conv id: {sophie_conv.id}")
        print("   Channels with email: conv 1 (mixed), conv 2 (mixed), Sophie (email-only)")
        print("   No email channel: convs 3-5 and property 4 convs")


if __name__ == "__main__":
    asyncio.run(main())
