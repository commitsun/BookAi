"""
Gestor de confirmaciones para escalaciones manuales.
===============================================
Centraliza la lógica que solicita confirmación al huésped antes de
escalar una conversación al encargado del hotel.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Optional

import logging

log = logging.getLogger("EscalationConsentManager")


@dataclass
class PendingEscalationConsent:
    """Estado de una escalación pendiente de confirmación por el huésped."""

    chat_id: str
    guest_message: str
    escalation_type: str
    reason: str
    context: str
    requested_at: datetime


class EscalationConsentManager:
    """Gestiona las confirmaciones de escalación pendientes por chat."""

    _TTL_SECONDS = 15 * 60  # 15 minutos

    def __init__(self):
        self._pending: Dict[str, PendingEscalationConsent] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def request_consent(
        self,
        chat_id: str,
        guest_message: str,
        escalation_type: str,
        reason: str,
        context: str,
    ) -> None:
        """Registra una nueva solicitud de confirmación."""

        payload = PendingEscalationConsent(
            chat_id=str(chat_id),
            guest_message=guest_message,
            escalation_type=escalation_type,
            reason=reason,
            context=context,
            requested_at=datetime.utcnow(),
        )

        with self._lock:
            self._pending[payload.chat_id] = payload

        log.info(
            "📝 Consentimiento de escalación registrado: %s (%s)",
            payload.chat_id,
            payload.escalation_type,
        )

    # ------------------------------------------------------------------
    def get_pending(self, chat_id: str) -> Optional[PendingEscalationConsent]:
        """Devuelve la escalación pendiente si no ha expirado."""

        cid = str(chat_id)
        with self._lock:
            pending = self._pending.get(cid)

        if not pending:
            return None

        if datetime.utcnow() - pending.requested_at > timedelta(seconds=self._TTL_SECONDS):
            log.info("⌛ Consentimiento expirado para %s", cid)
            self.clear(cid)
            return None

        return pending

    # ------------------------------------------------------------------
    def clear(self, chat_id: str) -> None:
        """Elimina cualquier confirmación pendiente para el chat dado."""

        cid = str(chat_id)
        with self._lock:
            existed = self._pending.pop(cid, None) is not None

        if existed:
            log.info("🧹 Consentimiento de escalación limpiado para %s", cid)

    # ------------------------------------------------------------------
    @staticmethod
    def classify_reply(text: str) -> str:
        """Clasifica la respuesta del huésped como afirmativa, negativa o desconocida."""

        if not text:
            return "unknown"

        normalized = re.sub(r"[^\wáéíóúüñ\s]", "", text.lower()).strip()
        if not normalized:
            return "unknown"

        positive_markers = {
            "si",
            "sí",
            "claro",
            "por supuesto",
            "adelante",
            "hazlo",
            "ok",
            "okay",
            "vale",
            "correcto",
        }
        negative_markers = {
            "no",
            "negativo",
            "mejor no",
            "ahora no",
            "gracias",
            "otro momento",
        }

        tokens = set(normalized.split())
        if tokens & positive_markers:
            return "yes"
        if tokens & negative_markers:
            return "no"

        # Buscar frases completas
        for marker in positive_markers:
            if marker in normalized:
                return "yes"
        for marker in negative_markers:
            if marker in normalized:
                return "no"

        return "unknown"


# Instancia global reutilizable
consent_manager = EscalationConsentManager()
