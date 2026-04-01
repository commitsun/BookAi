"""
🕒 Contexto Temporal Global para los Agentes
--------------------------------------------

Este módulo proporciona una función universal que devuelve la fecha y hora
actuales con formato natural (en español), junto con información de zona horaria.
El objetivo es ofrecer contexto temporal a los modelos de lenguaje sin hardcodear nada.

Se usa en los prompts de los agentes para mejorar su comprensión temporal,
por ejemplo cuando el huésped pregunta “¿qué día es hoy?” o “para este fin de semana”.

Este módulo NO genera respuestas visibles para el usuario.
Solo proporciona información contextual interna.
"""

from datetime import datetime
import pytz
import locale
import logging

log = logging.getLogger("time_context")

# 🌍 Zona horaria y configuración regional por defecto
DEFAULT_TZ = "Europe/Madrid"
DEFAULT_LOCALE = "es_ES.UTF-8"


# Devuelve una cadena con la fecha y hora actuales en formato natural y legible por el LLM.
# Se usa en el flujo de inyección de contexto temporal en prompts para preparar datos, validaciones o decisiones previas.
# Recibe `timezone` como entrada principal según la firma.
# Devuelve un `str` con el resultado de esta operación. Sin efectos secundarios relevantes.
def get_time_context(timezone: str = DEFAULT_TZ) -> str:
    """
    Devuelve una cadena con la fecha y hora actuales en formato natural y legible por el LLM.
    Ejemplo:
        "Hoy es miércoles, 29 de octubre de 2025, y son las 10:15 (CET)."
    """
    try:
        # Intentamos establecer localización española
        try:
            locale.setlocale(locale.LC_TIME, DEFAULT_LOCALE)
        except locale.Error:
            # fallback: puede no existir en contenedores Alpine
            locale.setlocale(locale.LC_TIME, "C")

        tz = pytz.timezone(timezone)
        now = datetime.now(tz)

        fecha = now.strftime("%A, %d de %B de %Y")
        hora = now.strftime("%H:%M")
        zona = now.strftime("%Z")

        return f"Hoy es {fecha}, y son las {hora} ({zona})."

    except Exception as e:
        log.error(f"❌ Error generando contexto temporal: {e}")
        return "La fecha y hora actuales no pudieron obtenerse en este momento."


# Inyecta el contexto temporal al principio de un prompt existente.
# Se usa en el flujo de inyección de contexto temporal en prompts para preparar datos, validaciones o decisiones previas.
# Recibe `base_prompt`, `timezone` como entradas relevantes junto con el contexto inyectado en la firma.
# Devuelve un `str` con el resultado de esta operación. Sin efectos secundarios relevantes.
def inject_time_context(base_prompt: str, timezone: str = DEFAULT_TZ) -> str:
    """
    Inyecta el contexto temporal al principio de un prompt existente.
    Útil para combinar en prompts de agentes o subagentes.
    """
    time_info = get_time_context(timezone)
    return f"{time_info}\n\n{base_prompt.strip()}"
