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
        "Un momento, voy a consultarlo...",
        "Déjame revisarlo para poder ayudarte mejor.",
        "Voy a comprobarlo ahora mismo.",
        "Dame un segundo, lo consulto y te digo.",
        "Permíteme verificarlo para poder confirmártelo.",

        # Con emojis suaves
        "🕐 Un momento, voy a revisarlo...",
        "⏳ Déjame comprobarlo, enseguida te respondo.",
        "📞 Lo estoy consultando ahora mismo.",

        # Más profesionales
        "Permíteme revisarlo para ofrecerte la mejor solución.",
        "Voy a verificarlo para asegurar la información.",
        "Déjame confirmarlo y te digo.",

        # Variaciones con contexto de prisa
        "Dame un momento que lo consulto...",
        "Consultándolo para darte la respuesta exacta...",
        "Un segundo que lo reviso...",
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
                "Esto requiere revisión inmediata, dame un momento...",
                "Lo estoy revisando con prioridad...",
            ])
        elif context == "info":
            return random.choice([
                "Voy a verificarlo para darte datos exactos...",
                "Permíteme confirmar los detalles...",
            ])
        else:
            return EscalationMessages.get_random()
