"""
Static multilingual templates for folio lifecycle event notes.

Notes are internal only — never sent to WhatsApp.
Templates are rendered in the instance's default language at creation time.
Translation to other languages is lazy (deferred until requested via SDK).
"""

from __future__ import annotations

_TEMPLATES: dict[str, dict[str, str]] = {
    "folio_created": {
        "es": "📋 Nueva reserva creada.",
        "gl": "📋 Nova reserva creada.",
        "pt": "📋 Nova reserva criada.",
        "en": "📋 New reservation created.",
        "fr": "📋 Nouvelle réservation créée.",
    },
    "folio_cancelled": {
        "es": "❌ Reserva cancelada.",
        "gl": "❌ Reserva cancelada.",
        "pt": "❌ Reserva cancelada.",
        "en": "❌ Reservation cancelled.",
        "fr": "❌ Réservation annulée.",
    },
    "folio_modified.room_added": {
        "es": "🏨 Nueva habitación añadida a la reserva.",
        "gl": "🏨 Nova habitación engadida á reserva.",
        "pt": "🏨 Novo quarto adicionado à reserva.",
        "en": "🏨 New room added to reservation.",
        "fr": "🏨 Nouvelle chambre ajoutée à la réservation.",
    },
    "folio_modified.room_cancelled": {
        "es": "🏨 Habitación cancelada en la reserva.",
        "gl": "🏨 Habitación cancelada na reserva.",
        "pt": "🏨 Quarto cancelado na reserva.",
        "en": "🏨 Room cancelled in reservation.",
        "fr": "🏨 Chambre annulée dans la réservation.",
    },
    "folio_modified.dates_changed": {
        "es": "📅 Fechas modificadas: {checkin_date} – {checkout_date}.",
        "gl": "📅 Datas modificadas: {checkin_date} – {checkout_date}.",
        "pt": "📅 Datas alteradas: {checkin_date} – {checkout_date}.",
        "en": "📅 Dates changed: {checkin_date} – {checkout_date}.",
        "fr": "📅 Dates modifiées : {checkin_date} – {checkout_date}.",
    },
    "folio_modified.service_added": {
        "es": "➕ Servicio añadido a la reserva.",
        "gl": "➕ Servizo engadido á reserva.",
        "pt": "➕ Serviço adicionado à reserva.",
        "en": "➕ Service added to reservation.",
        "fr": "➕ Service ajouté à la réservation.",
    },
    "folio_modified.room_changed": {
        "es": "🔄 Cambio de habitación en la reserva.",
        "gl": "🔄 Cambio de habitación na reserva.",
        "pt": "🔄 Mudança de quarto na reserva.",
        "en": "🔄 Room change in reservation.",
        "fr": "🔄 Changement de chambre dans la réservation.",
    },
    "payment_registered": {
        "es": "💳 Pago registrado: {amount} {currency}.",
        "gl": "💳 Pago rexistrado: {amount} {currency}.",
        "pt": "💳 Pagamento registado: {amount} {currency}.",
        "en": "💳 Payment registered: {amount} {currency}.",
        "fr": "💳 Paiement enregistré : {amount} {currency}.",
    },
    "precheckin_completed": {
        "es": "✅ Pre check-in completado: {guest_name} → habitación {room_number}.",
        "gl": "✅ Pre check-in completado: {guest_name} → habitación {room_number}.",
        "pt": "✅ Pré check-in concluído: {guest_name} → quarto {room_number}.",
        "en": "✅ Pre check-in completed: {guest_name} → room {room_number}.",
        "fr": "✅ Pré check-in effectué : {guest_name} → chambre {room_number}.",
    },
    "status_changed": {
        "es": "🔄 Estado actualizado: {new_status}.",
        "gl": "🔄 Estado actualizado: {new_status}.",
        "pt": "🔄 Estado atualizado: {new_status}.",
        "en": "🔄 Status updated: {new_status}.",
        "fr": "🔄 Statut mis à jour : {new_status}.",
    },
    "transfer.outgoing": {
        "es": "🔁 Conversación traspasada a {dest_name}: {agent_note}",
        "gl": "🔁 Conversa traspasada a {dest_name}: {agent_note}",
        "pt": "🔁 Conversa transferida para {dest_name}: {agent_note}",
        "en": "🔁 Conversation transferred to {dest_name}: {agent_note}",
        "fr": "🔁 Conversation transférée vers {dest_name} : {agent_note}",
    },
    "transfer.incoming": {
        "es": "🔁 Conversación traspasada desde {origin_name}: {agent_note}",
        "gl": "🔁 Conversa traspasada dende {origin_name}: {agent_note}",
        "pt": "🔁 Conversa transferida de {origin_name}: {agent_note}",
        "en": "🔁 Conversation transferred from {origin_name}: {agent_note}",
        "fr": "🔁 Conversation transférée depuis {origin_name} : {agent_note}",
    },
}

SUPPORTED_LANGUAGES = frozenset(_TEMPLATES[next(iter(_TEMPLATES))].keys())

_UNROUTED_LABEL: dict[str, str] = {
    "es": "bandeja central",
    "gl": "bandexa central",
    "pt": "caixa central",
    "en": "central inbox",
    "fr": "boîte centrale",
}


def unrouted_label(lang: str) -> str:
    """Return the localised label for the unrouted (property:0) inbox."""
    return _UNROUTED_LABEL.get(lang) or _UNROUTED_LABEL["es"]


def render_note(template_key: str, lang: str, **context: object) -> str:
    """Render a note template in the given language with interpolated context.

    Raises KeyError if the template_key is not found.
    Falls back to 'es' if lang is not available for this template.
    """
    variants = _TEMPLATES[template_key]
    template = variants.get(lang) or variants["es"]
    return template.format(**context)
