# core/utils/escalation_messages.py
import random
from typing import List

class EscalationMessages:
    """
    Generador de mensajes de escalación aleatorios y naturales.
    Evita que el sistema suene robótico.
    """

    MESSAGES: List[str] = [
        # Naturales y cercanas
        "Un momento, estoy consultando con el encargado del hotel sobre esto...",
        "Déjame que me ponga en contacto con el equipo para poder ayudarte mejor.",
        "Voy a consultar esto directamente con el gerente del hotel.",
        "Dame un segundo que me comunico con el encargado para darte la mejor respuesta.",
        "Permíteme contactar con el hotel directamente para poder confirmarte esto.",

        # Con emojis suaves
        "🕐 Un momento, voy a hablar con el encargado...",
        "⏳ Déjame contactar con el equipo del hotel, enseguida te respondo.",
        "📞 Estoy contactando con el encargado ahora mismo.",

        # Más profesionales
        "Permíteme comunicarme con nuestro equipo para ofrecerte la mejor solución.",
        "Voy a verificar directamente con el equipo del hotel para asegurar la información.",
        "Déjame confirmar esto con el gestor del hotel.",

        # Variaciones con contexto de prisa
        "Dame un momento que consulto internamente sobre esto...",
        "Contactando con el encargado para darte la respuesta exacta...",
        "Un segundo que me comunico directamente con el equipo...",
    ]

    @staticmethod
    def get_random() -> str:
        """Retorna un mensaje aleatorio de escalación"""
        return random.choice(EscalationMessages.MESSAGES)

    @staticmethod
    def get_by_context(context: str = "general") -> str:
        """
        Retorna mensaje según el contexto
        - "general": Escalación normal
        - "urgent": Escalación urgente
        - "info": Falta de información factual
        """
        if context == "urgent":
            return random.choice([
                "Esto requiere atención inmediata del encargado, dame un momento...",
                "Contactando urgentemente con el equipo del hotel...",
            ])
        elif context == "info":
            return random.choice([
                "Voy a verificar esto con el equipo para darte datos exactos...",
                "Permíteme confirmar los detalles con el encargado...",
            ])
        else:
            return EscalationMessages.get_random()
