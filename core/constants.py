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
    "envialo",
    "enviarlo",
    "envíalo",
    "envíaselo",
    "enviaselo",
}

WA_CANCEL_WORDS = {"cancel", "cancelar", "no"}

# Único hotel activo en el pipeline y modo Superintendente
ACTIVE_HOTEL_NAME = "Alda Centro Ponferrada"
