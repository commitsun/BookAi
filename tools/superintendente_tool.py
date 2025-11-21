"""
Herramientas para el Superintendente (implementación simple con StructuredTool)
"""

import asyncio
import logging
from typing import Any, Optional, Callable

from langchain.tools import StructuredTool
from pydantic import BaseModel, Field

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


class SendMessageMainInput(BaseModel):
    message: str = Field(
        ...,
        description="Mensaje que el encargado quiere enviar al MainAgent",
    )


class SendWhatsAppInput(BaseModel):
    guest_id: str = Field(..., description="ID del huésped en WhatsApp (con prefijo país)")
    message: str = Field(..., description="Mensaje de texto a enviar (sin plantilla)")


def create_add_to_kb_tool(hotel_name: str, append_func: Callable[[str, str, str, str], Any]):
    async def _add_to_kb(topic: str, content: str, category: str = "general") -> str:
        log.info("Agregando a KB (S3): %s (categoría: %s)", topic, category)
        try:
            await append_func(
                topic=topic,
                content=content,
                hotel_name=hotel_name,
                source_type=category,
            )
            return f"✅ Información '{topic}' agregada correctamente al documento de conocimientos"
        except Exception as exc:
            log.error("Error agregando a KB: %s", exc)
            return f"❌ Error: {exc}"

    return StructuredTool.from_function(
        name="agregar_a_base_conocimientos",
        description=(
            "Agrega información a la base de conocimientos (documento en S3). "
            "Usada cuando el encargado proporciona información que debe estar "
            "disponible para futuras preguntas de huéspedes."
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
    async def _review_conversations(limit: int = 10) -> str:
        try:
            if not memory_manager:
                return "⚠️ No hay gestor de memoria configurado."

            convos = await asyncio.to_thread(
                memory_manager.get_memory, hotel_name, limit
            )
            count = len(convos) if convos else 0
            return (
                f"🧠 Resumen de conversaciones recientes ({count})\n"
                "Funcionalidad detallada pendiente de implementar."
            )
        except Exception as exc:
            log.error("Error revisando conversaciones: %s", exc)
            return f"❌ Error: {exc}"

    return StructuredTool.from_function(
        name="revisar_conversaciones",
        description=(
            "Resume conversaciones recientes de huéspedes para identificar patrones, "
            "preguntas frecuentes y oportunidades de mejorar la base de conocimientos."
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
            "Requiere el ID/phone del huésped (con prefijo de país)."
        ),
        coroutine=_send_whatsapp,
        args_schema=SendWhatsAppInput,
    )
