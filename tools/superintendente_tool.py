"""
Herramientas para el Superintendente (implementación simple con StructuredTool)
"""

import asyncio
import logging
import re
from datetime import datetime
from typing import Any, Optional, Callable

from langchain.tools import StructuredTool
from pydantic import BaseModel, Field

from core.db import get_conversation_history

log = logging.getLogger("SuperintendenteTools")


class AddToKBInput(BaseModel):
    topic: str = Field(..., description="Tema o categoría (ej: 'Servicios de Spa')")
    content: str = Field(..., description="Contenido detallado de la información")
    category: str = Field(
        default="general",
        description="Categoría: servicios, ubicación, politicas, etc",
    )


class SendBroadcastInput(BaseModel):
    template_id: str = Field(..., description="ID de la plantilla de WhatsApp")
    guest_ids: str = Field(..., description="IDs de huéspedes separados por comas")
    parameters: Optional[dict] = Field(
        None,
        description="Parámetros de la plantilla (JSON)",
    )


class ReviewConversationsInput(BaseModel):
    limit: int = Field(
        default=10,
        description="Cantidad de conversaciones recientes a revisar",
    )
    guest_id: Optional[str] = Field(
        default=None,
        description="ID del huésped/WhatsApp (incluye prefijo de país, ej: +34123456789)",
    )
    mode: Optional[str] = Field(
        default=None,
        description="Modo de entrega: 'resumen' (síntesis IA) u 'original' (mensajes tal cual)",
    )


class SendMessageMainInput(BaseModel):
    message: str = Field(
        ...,
        description="Mensaje que el encargado quiere enviar al MainAgent",
    )


class SendWhatsAppInput(BaseModel):
    guest_id: str = Field(..., description="ID del huésped en WhatsApp (con prefijo país)")
    message: str = Field(..., description="Mensaje de texto a enviar (sin plantilla)")


def create_add_to_kb_tool(
    hotel_name: str,
    append_func: Callable[[str, str, str, str], Any],
    llm: Any = None,
):
    async def _rewrite_with_ai(topic: str, category: str, content: str) -> tuple[str, str, str]:
        """
        Reformula el borrador con IA para que sea apto para huéspedes y devuelva
        campos estructurados. Se usa un prompt ligero para no inventar datos.
        """
        if not llm:
            return topic, category, content

        try:
            prompt = (
                "Eres el redactor de la base de conocimientos del hotel. "
                "Reescribe el contenido en tono neutro y claro para huéspedes, sin emojis. "
                "Devuelve siempre este formato exacto:\n"
                "TEMA: <título breve>\n"
                "CATEGORÍA: <categoría>\n"
                "CONTENIDO:\n"
                "<texto en 3-6 frases cortas, solo hechos confirmados>"
            )
            user_msg = (
                f"Hotel: {hotel_name}\n"
                f"Tema propuesto: {topic}\n"
                f"Categoría: {category}\n"
                f"Notas del encargado:\n{content}"
            )

            response = await llm.ainvoke(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_msg},
                ]
            )

            text = (getattr(response, "content", None) or "").strip()
            if not text:
                return topic, category, content

            topic_match = re.search(r"tema\s*:\s*(.+)", text, flags=re.IGNORECASE)
            category_match = re.search(r"categor[ií]a\s*:\s*(.+)", text, flags=re.IGNORECASE)
            content_match = re.search(r"contenido\s*:\s*(.+)", text, flags=re.IGNORECASE | re.DOTALL)

            new_topic = topic_match.group(1).strip() if topic_match else topic
            new_category = category_match.group(1).strip() if category_match else category
            new_content = content_match.group(1).strip() if content_match else content

            # Evita pipes que rompen el marcador [KB_DRAFT]
            new_topic = new_topic.replace("|", "/")
            new_category = new_category.replace("|", "/")
            new_content = new_content.replace("|", "/")

            return new_topic or topic, new_category or category, new_content or content
        except Exception as exc:
            log.warning("No se pudo reformular KB con IA: %s", exc)
            return topic, category, content

    async def _add_to_kb(topic: str, content: str, category: str = "general") -> str:
        """
        Genera un borrador pendiente de confirmación para agregar a la KB.
        La confirmación la gestionará el webhook de Telegram antes de llamar a append_func.
        """
        log.info("Preparando borrador de KB (S3): %s (categoría: %s)", topic, category)
        safe_content = (content or "").replace("|", "/").strip()
        safe_topic = (topic or "").replace("|", "/").strip()[:200]
        safe_category = (category or "general").replace("|", "/").strip() or "general"

        ai_topic, ai_category, ai_content = await _rewrite_with_ai(safe_topic, safe_category, safe_content)

        final_topic = (ai_topic or safe_topic).strip()[:200]
        final_category = (ai_category or safe_category).strip() or "general"
        final_content = (ai_content or safe_content).strip()

        preview = (
            "📝 Borrador para base de conocimientos (revisado con IA).\n"
            "Confirma con 'OK' para guardar o envía ajustes para que los aplique.\n"
            f"[KB_DRAFT]|{hotel_name}|{final_topic}|{final_category}|{final_content}"
        )
        return preview

    return StructuredTool.from_function(
        name="agregar_a_base_conocimientos",
        description=(
            "Genera un borrador para agregar información a la base de conocimientos (documento en S3). "
            "El encargado debe confirmar antes de que se guarde."
        ),
        coroutine=_add_to_kb,
        args_schema=AddToKBInput,
    )


def create_send_broadcast_tool(hotel_name: str, channel_manager: Any, supabase_client: Any):
    async def _send_broadcast(template_id: str, guest_ids: str, parameters: Optional[dict] = None) -> str:
        try:
            ids = [gid.strip() for gid in guest_ids.split(",") if gid.strip()]
            if not channel_manager:
                return "⚠️ Canal de envío no configurado."

            success_count = 0
            for guest_id in ids:
                try:
                    await channel_manager.send_template_message(
                        guest_id,
                        template_id,
                        parameters=parameters,
                    )
                    success_count += 1
                except Exception as exc:
                    log.warning("Error enviando a %s: %s", guest_id, exc)

            return f"✅ Broadcast enviado a {success_count}/{len(ids)} huéspedes"
        except Exception as exc:
            log.error("Error en broadcast: %s", exc)
            return f"❌ Error: {exc}"

    return StructuredTool.from_function(
        name="enviar_broadcast",
        description=(
            "Envía un mensaje plantilla de WhatsApp a múltiples huéspedes. "
            "Ideal para comunicados masivos (ej: 'Cafetería cerrada por mantenimiento')."
        ),
        coroutine=_send_broadcast,
        args_schema=SendBroadcastInput,
    )


def create_review_conversations_tool(hotel_name: str, memory_manager: Any):
    async def _review_conversations(
        limit: int = 10,
        guest_id: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> str:
        try:
            if not memory_manager:
                return "⚠️ No hay gestor de memoria configurado."

            if not guest_id:
                return (
                    "⚠️ Para revisar una conversación necesito el ID del huésped "
                    "(guest_id). Ejemplo: +34683527049"
                )

            normalized_mode = (mode or "").strip().lower()
            if not normalized_mode:
                return (
                    "🤖 ¿Quieres un resumen IA o la conversación tal cual?\n"
                    "Responde 'resumen' para que sintetice los puntos clave o 'original' si quieres ver los mensajes completos."
                )

            valid_summary = {"resumen", "summary", "sintesis", "síntesis"}
            valid_raw = {"original", "historial", "completo", "raw", "crudo", "mensajes"}
            if normalized_mode not in valid_summary | valid_raw:
                return (
                    "⚠️ Modo no reconocido. Usa 'resumen' para síntesis o 'original' para ver los mensajes completos."
                )

            clean_id = str(guest_id).replace("+", "").strip()

            # Recupera de Supabase (limit extendido) y combina con memoria en RAM
            db_msgs = await asyncio.to_thread(
                get_conversation_history,
                clean_id,
                limit * 3,  # pedir más por si hay ruido o system messages
                None,
            )
            runtime_msgs = []
            try:
                runtime_msgs = memory_manager.runtime_memory.get(clean_id, [])
            except Exception:
                runtime_msgs = []

            combined = (db_msgs or []) + (runtime_msgs or [])

            def _parse_ts(ts: Any) -> float:
                try:
                    if isinstance(ts, datetime):
                        return ts.timestamp()
                    ts_str = str(ts).replace("Z", "")
                    return datetime.fromisoformat(ts_str).timestamp()
                except Exception:
                    return 0.0

            combined_sorted = sorted(combined, key=lambda m: _parse_ts(m.get("created_at")))
            convos = combined_sorted[-limit:] if combined_sorted else []

            # 🚫 Evita duplicados exactos (rol + contenido + timestamp)
            seen = set()
            deduped = []
            for msg in convos:
                key = (
                    msg.get("role", "assistant"),
                    (msg.get("content") or "").strip(),
                    str(msg.get("created_at")),
                )
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(msg)

            convos = deduped
            count = len(convos)

            if not convos:
                return f"🧠 Resumen de conversaciones recientes (0)\nNo hay mensajes recientes para {guest_id}."

            def _fmt_ts(ts: Any) -> str:
                try:
                    if isinstance(ts, datetime):
                        return ts.strftime("%d/%m %H:%M")
                    ts_str = str(ts).replace("Z", "")
                    return datetime.fromisoformat(ts_str).strftime("%d/%m %H:%M")
                except Exception:
                    return ""

            lines = []
            for msg in convos:
                role = msg.get("role", "assistant")
                prefix = {"user": "Huésped", "assistant": "Asistente", "system": "Sistema", "tool": "Tool"}.get(
                    role, "Asistente"
                )
                ts = _fmt_ts(msg.get("created_at"))
                ts_suffix = f" · {ts}" if ts else ""
                content = msg.get("content", "").strip()
                lines.append(f"- {prefix}{ts_suffix}: {content}")

            formatted = "\n".join(lines)
            if normalized_mode in valid_raw:
                return f"🗂️ Conversación recuperada ({count})\n{formatted}"

            return (
                "🧠 Historial recuperado para resumir\n"
                f"Mensajes ({count}):\n{formatted}\n"
                "➡️ Genera un resumen claro para el encargado con los puntos clave, dudas y acciones pendientes."
            )
        except Exception as exc:
            log.error("Error revisando conversaciones: %s", exc)
            return f"❌ Error: {exc}"

    return StructuredTool.from_function(
        name="revisar_conversaciones",
        description=(
            "Revisa conversaciones recientes de un huésped específico. "
            "Pregunta primero si el encargado quiere 'resumen' (síntesis IA) u 'original' (mensajes tal cual). "
            "Debes indicar el guest_id (por ejemplo +34683527049)."
        ),
        coroutine=_review_conversations,
        args_schema=ReviewConversationsInput,
    )


def create_send_message_main_tool(encargado_id: str, channel_manager: Any):
    async def _send_message_main(message: str) -> str:
        try:
            if not channel_manager:
                return "⚠️ Canal de envío no configurado."

            await channel_manager.send_message(
                encargado_id,
                f"📨 Mensaje enviado al MainAgent:\n{message}",
                channel="telegram",
            )
            return "✅ Mensaje enviado al MainAgent."
        except Exception as exc:
            log.error("Error enviando mensaje al MainAgent: %s", exc)
            return f"❌ Error: {exc}"

    return StructuredTool.from_function(
        name="enviar_mensaje_main",
        description=(
            "Envía un mensaje del encargado al MainAgent para coordinar respuestas o "
            "reactivar escalaciones."
        ),
        coroutine=_send_message_main,
        args_schema=SendMessageMainInput,
    )


def create_send_whatsapp_tool(channel_manager: Any):
    async def _send_whatsapp(guest_id: str, message: str) -> str:
        """
        Genera un borrador para envío por WhatsApp.
        La app principal gestionará confirmación/ajustes antes de enviar.
        """
        return f"[WA_DRAFT]|{guest_id}|{message}"

    return StructuredTool.from_function(
        name="enviar_mensaje_whatsapp",
        description=(
            "Genera un borrador de mensaje de texto directo por WhatsApp a un huésped, "
            "sin plantilla (proceso de confirmación requerido). "
            "Requiere el ID/phone del huésped (con prefijo de país). "
            "Úsala solo cuando el encargado pida explícitamente enviar un mensaje; no la uses para ajustes de KB ni para reinterpretar feedback."
        ),
        coroutine=_send_whatsapp,
        args_schema=SendWhatsAppInput,
    )
