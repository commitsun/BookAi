"""Constantes compartidas en los handlers y utilidades."""

# Palabras clave para confirmar o cancelar envíos en WhatsApp/Telegram
WA_CONFIRM_WORDS = {
    "enviar",
    "ok",
    "confirmar",
    "si",
    "sí",
    "sii",
    "siii",
    "dale",
    "manda",
}

WA_CANCEL_WORDS = {"cancel", "cancelar", "no"}

# Hotel por defecto usado en el pipeline y en el modo Superintendente
DEFAULT_HOTEL_NAME = "Hotel Default"
