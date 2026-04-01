"""Utilidades comunes para formateo y metadatos de mensajes."""

from __future__ import annotations

import logging
import re

from core import escalation_db as escalation_db_store
from tools.interno_tool import ESCALATIONS_STORE

log = logging.getLogger("MessageUtils")


# Recupera metadatos de una escalación desde memoria o DB.
# Se usa en el flujo de sanitización de mensajes y utilidades de borradores para preparar datos, validaciones o decisiones previas.
# Recibe `escalation_id` como entrada principal según la firma.
# Devuelve un `dict` con el resultado de esta operación. Sin efectos secundarios relevantes.
def get_escalation_metadata(escalation_id: str) -> dict:
    """Recupera metadatos de una escalación desde memoria o DB."""
    if not escalation_id:
        return {}

    try:
        esc = ESCALATIONS_STORE.get(escalation_id)
        if esc:
            return {
                "type": esc.escalation_type,
                "reason": esc.escalation_reason,
                "context": esc.context,
            }
    except Exception:
        pass

    try:
        record = escalation_db_store.get_escalation(escalation_id)
        if record:
            return {
                "type": record.get("escalation_type"),
                "reason": record.get("escalation_reason") or record.get("reason", ""),
                "context": record.get("context", ""),
            }
    except Exception as exc:
        log.warning("No se pudo obtener metadatos de escalación %s: %s", escalation_id, exc)

    return {}


# Devuelve solo el borrador limpio generado por el InternoAgent,.
# Se usa en el flujo de sanitización de mensajes y utilidades de borradores para preparar datos, validaciones o decisiones previas.
# Recibe `text` como entrada principal según la firma.
# Devuelve un `str` con el resultado de esta operación. Sin efectos secundarios relevantes.
def extract_clean_draft(text: str) -> str:
    """
    Devuelve solo el borrador limpio generado por el InternoAgent,
    eliminando razonamiento intermedio o metadata que no debería ver el encargado.
    """
    if not text:
        return text

    draft_markers = [
        "📝 *BORRADOR DE RESPUESTA PROPUESTO:*",
        "📝 *Nuevo borrador generado",
        "📝 BORRADOR",
    ]

    metadata_markers = [
        "[- Origen:",
        "- Origen:",
        "- Acción requerida:",
        "- Contenido:",
        "- Evidencia:",
        "- Estado:",
        "Utilizo la herramienta",
        "¿Desea que esta directriz",
    ]

    lines = text.splitlines()
    clean_lines = []
    in_draft = False
    skip_next_blank = False

    for line in lines:
        stripped = line.strip()

        if any(marker in line for marker in draft_markers):
            in_draft = True
            clean_lines.append(line)
            skip_next_blank = False
            continue

        if any(marker in line for marker in metadata_markers):
            skip_next_blank = True
            continue

        if skip_next_blank and not stripped:
            skip_next_blank = False
            continue

        if in_draft:
            clean_lines.append(line)
        elif not any(marker in line for marker in metadata_markers):
            clean_lines.append(line)

    result = "\n".join(clean_lines).strip()

    if not in_draft:
        return text

    return result or text


# Devuelve un mensaje corto y limpio para WhatsApp.
# Se usa en el flujo de sanitización de mensajes y utilidades de borradores para preparar datos, validaciones o decisiones previas.
# Recibe `msg` como entrada principal según la firma.
# Devuelve un `str` con el resultado de esta operación. Sin efectos secundarios relevantes.
def sanitize_wa_message(msg: str) -> str:
    """
    Devuelve un mensaje corto y limpio para WhatsApp.
    Junta todas las líneas útiles en una sola, eliminando comillas y espacios extra.
    """
    if not msg:
        return msg

    lines = [ln.strip() for ln in msg.splitlines() if ln.strip()]
    joined = " ".join(lines) if lines else str(msg).strip()
    compact = " ".join(joined.split())
    return compact.strip().strip('\"“”')


# Aplica un formato ligero y conversacional a las respuestas del Superintendente.
# Se usa en el flujo de sanitización de mensajes y utilidades de borradores para preparar datos, validaciones o decisiones previas.
# Recibe `text` como entrada principal según la firma.
# Devuelve un `str` con el resultado de esta operación. Sin efectos secundarios relevantes.
def format_superintendente_message(text: str) -> str:
    """Aplica un formato ligero y conversacional a las respuestas del Superintendente."""
    if not text:
        return text

    lines = [ln.rstrip() for ln in text.strip().splitlines()]
    compact = []
    blank_seen = False
    for ln in lines:
        if not ln.strip():
            if blank_seen:
                continue
            blank_seen = True
            compact.append("")
            continue
        blank_seen = False
        stripped = ln.strip()
        if stripped.lower().startswith("[superintendente]"):
            stripped = stripped.split("]", 1)[-1].strip()
        compact.append(stripped)

    return "\n".join(compact).strip()


# Determina si parecido a new instruction cumple la condición necesaria en este punto del flujo.
# Se usa en el flujo de sanitización de mensajes y utilidades de borradores para preparar datos, validaciones o decisiones previas.
# Recibe `text` como entrada principal según la firma.
# Devuelve un booleano que gobierna la rama de ejecución siguiente. Sin efectos secundarios relevantes.
def looks_like_new_instruction(text: str) -> bool:
    if not text:
        return False
    action_terms = {
        "mandale",
        "mándale",
        "enviale",
        "envíale",
        "manda",
        "mensaje",
        "whatsapp",
        "consulta",
        "reservas",
        "disponibilidad",
        "precio",
        "precios",
        "tarifa",
        "tarifas",
        "folio",
        "checkin",
        "checkout",
        "historial",
        "convers",
        "broadcast",
        "plantilla",
        "resumen",
        "agrega",
        "añade",
        "anade",
        "elimina",
        "borra",
    }
    lowered = text.lower()
    return any(term in lowered for term in action_terms)


# Previsualización consistente para propuestas de KB.
# Se usa en el flujo de sanitización de mensajes y utilidades de borradores para preparar datos, validaciones o decisiones previas.
# Recibe `topic`, `category`, `content` como entradas relevantes junto con el contexto inyectado en la firma.
# Devuelve un `str` con el resultado de esta operación. Sin efectos secundarios relevantes.
def build_kb_preview(topic: str, category: str, content: str) -> str:
    """Previsualización consistente para propuestas de KB."""
    return (
        "📝 Propuesta para base de conocimientos:\n"
        f"TEMA: {topic}\n"
        f"CATEGORÍA: {category}\n"
        f"CONTENIDO:\n{content}\n\n"
        "✅ Responde 'ok' para agregarla.\n"
        "📝 Envía ajustes si quieres editarla.\n"
        "❌ Responde 'no' para descartarla."
    )


# Extrae campos básicos de KB desde un texto de respuesta.
# Se usa en el flujo de sanitización de mensajes y utilidades de borradores para preparar datos, validaciones o decisiones previas.
# Recibe `response` como dependencias o servicios compartidos inyectados desde otras capas, y `hotel_name` como datos de contexto o entrada de la operación.
# Devuelve el resultado calculado para que el siguiente paso lo consuma. Sin efectos secundarios relevantes.
def extract_kb_fields(response: str, hotel_name: str):
    """Extrae campos básicos de KB desde un texto de respuesta."""
    topic = "Información"
    category = "general"
    content_block = response

    if not response:
        return topic, category, content_block

    topic_match = re.search(r"tema\s*:\s*(.+)", response, flags=re.IGNORECASE)
    if topic_match:
        topic = topic_match.group(1).strip()

    category_match = re.search(r"categor[ií]a\s*:\s*(.+)", response, flags=re.IGNORECASE)
    if category_match:
        category = category_match.group(1).strip()

    content_match = re.search(r"contenido\s*:\s*(.+)", response, flags=re.IGNORECASE | re.DOTALL)
    if content_match:
        content_block = content_match.group(1).strip()

    return topic or "Información", category or "general", content_block or response
