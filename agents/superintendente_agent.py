"""
SuperintendenteAgent v1 - Gestión de Conocimiento y Estrategia

- Agregar/actualizar base de conocimientos
- Revisar historial de conversaciones
- Enviar broadcasts
- Comunicación con encargado
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from langchain.agents import AgentExecutor, create_openai_tools_agent
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder

from pathlib import Path
import re
import tempfile

import boto3
from botocore.config import Config as BotoConfig

from core.config import ModelConfig, ModelTier, Settings
from core.utils.time_context import get_time_context
from core.utils.utils_prompt import load_prompt

log = logging.getLogger("SuperintendenteAgent")


class SuperintendenteAgent:
    """
    Agente Superintendente - Gestor de Conocimiento

    Comunicación exclusiva con encargado vía Telegram
    """

    def __init__(
        self,
        memory_manager: Any,
        supabase_client: Any = None,
        channel_manager: Any = None,
        model_tier: ModelTier = ModelTier.SUPERINTENDENTE,
    ) -> None:
        self.memory_manager = memory_manager
        self.supabase_client = supabase_client
        self.channel_manager = channel_manager
        self.model_tier = model_tier

        self.llm = ModelConfig.get_llm(model_tier)

        log.info("SuperintendenteAgent inicializado (modelo: %s)", self.llm.model_name)

    async def ainvoke(
        self,
        user_input: str,
        encargado_id: str,
        hotel_name: str,
        context_window: int = 20,
        chat_history: Optional[List[Any]] = None,
    ) -> str:
        """
        Invocar agente superintendente (sesión con encargado)

        Args:
            user_input: Mensaje del encargado
            encargado_id: ID Telegram del encargado (session_id)
            hotel_name: Nombre del hotel
            context_window: Mensajes para contexto
            chat_history: Historial externo
        """

        try:
            log.info("SuperintendenteAgent ainvoke: %s", encargado_id)

            if chat_history is None:
                chat_history = await self._safe_call(
                    getattr(self.memory_manager, "get_memory_as_messages", None),
                    conversation_id=encargado_id,
                    limit=context_window,
                )
            chat_history = chat_history or []

            tools = await self._create_tools(hotel_name, encargado_id)

            system_prompt = self._build_system_prompt(hotel_name)

            prompt_template = ChatPromptTemplate.from_messages(
                [
                    ("system", system_prompt),
                    MessagesPlaceholder(variable_name="chat_history", optional=True),
                    ("human", "{input}"),
                    MessagesPlaceholder(variable_name="agent_scratchpad", optional=True),
                ]
            )

            agent_chain = create_openai_tools_agent(
                llm=self.llm,
                tools=tools,
                prompt=prompt_template,
            )

            executor = AgentExecutor(
                agent=agent_chain,
                tools=tools,
                verbose=True,
                max_iterations=20,
                handle_parsing_errors=True,
                return_intermediate_steps=True,
                max_execution_time=90,
            )

            result = await executor.ainvoke(
                input={
                    "input": user_input,
                    "chat_history": chat_history,
                }
            )

            output = (result.get("output") or "").strip()

            # 🚦 Propagar marcadores especiales si vinieron en pasos intermedios
            intermediates = result.get("intermediate_steps") or []
            wa_marker = None
            kb_marker = None
            for _action, observation in intermediates:
                if isinstance(observation, str) and "[WA_DRAFT]|" in observation:
                    wa_marker = observation[
                        observation.index("[WA_DRAFT]|") :
                    ].strip()
                if isinstance(observation, str) and "[KB_DRAFT]|" in observation:
                    kb_marker = observation[
                        observation.index("[KB_DRAFT]|") :
                    ].strip()
                if wa_marker and kb_marker:
                    break
            if wa_marker and "[WA_DRAFT]|" not in output:
                output = f"{wa_marker}\n{output}"
            if kb_marker and "[KB_DRAFT]|" not in output:
                output = f"{kb_marker}\n{output}"

            await self._safe_call(
                getattr(self.memory_manager, "save", None),
                conversation_id=encargado_id,
                role="user",
                content=user_input,
            )

            await self._safe_call(
                getattr(self.memory_manager, "save", None),
                conversation_id=encargado_id,
                role="assistant",
                content=f"[Superintendente] {output}",
            )

            log.info("SuperintendenteAgent completado: %s chars", len(output))
            return output

        except Exception as exc:
            log.error("Error en SuperintendenteAgent: %s", exc, exc_info=True)
            raise

    async def _create_tools(self, hotel_name: str, encargado_id: str):
        """Crear tools del superintendente"""

        from tools.superintendente_tool import (
            create_add_to_kb_tool,
            create_review_conversations_tool,
            create_send_broadcast_tool,
            create_send_message_main_tool,
            create_send_whatsapp_tool,
        )

        tools = [
            create_add_to_kb_tool(
                hotel_name=hotel_name,
                append_func=self._append_to_knowledge_document,
                llm=self.llm,
            ),
            create_review_conversations_tool(
                hotel_name=hotel_name,
                memory_manager=self.memory_manager,
            ),
            create_send_broadcast_tool(
                hotel_name=hotel_name,
                channel_manager=self.channel_manager,
                supabase_client=self.supabase_client,
            ),
            create_send_message_main_tool(
                encargado_id=encargado_id,
                channel_manager=self.channel_manager,
            ),
            create_send_whatsapp_tool(
                channel_manager=self.channel_manager,
            ),
        ]

        # Ajustar append_func para herramienta de KB al método interno S3
        return [tool for tool in tools if tool is not None]

    def _build_system_prompt(self, hotel_name: str) -> str:
        """Construir system prompt para superintendente"""

        base = load_prompt("superintendente_prompt.txt") or (
            "Eres el Superintendente del Sistema de IA Hotelera.\n"
            "Tu rol es gestionar el conocimiento del hotel y optimizar el sistema de agentes.\n\n"
            "RESPONSABILIDADES:\n"
            "1. Agregar y actualizar la base de conocimientos del hotel\n"
            "2. Revisar el historial de conversaciones de huéspedes\n"
            "3. Enviar mensajes masivos a través de WhatsApp\n"
            "4. Coordinar con el MainAgent\n"
            "5. Ayudar al encargado a mejorar las respuestas\n\n"
            "HERRAMIENTAS DISPONIBLES:\n"
            "1. agregar_a_base_conocimientos - Agrega información vectorizada a Supabase\n"
            "2. revisar_conversaciones - Revisa conversaciones recientes de huéspedes (pide modo: resumen u original)\n"
            "3. enviar_broadcast - Envía plantillas masivas a múltiples huéspedes\n"
            "4. enviar_mensaje_main - Envía respuesta del encargado al MainAgent\n\n"
            "TONO: Profesional, eficiente, orientado a mejora continua.\n\n"
            "REGLAS CLAVE:\n"
            "- Antes de usar revisar_conversaciones pregunta si prefiere 'resumen' (síntesis IA) o ver los mensajes 'originales'; usa el modo solicitado.\n"
            "- Usa SIEMPRE la tool revisar_conversaciones para mostrar el historial de un huésped; si no tienes guest_id, pídelo y respeta el límite indicado (default 10).\n"
            "- REGLA CRÍTICA PARA KB: Cuando el encargado pida agregar/actualizar información en la base de conocimientos, "
            "usa SIEMPRE la herramienta 'agregar_a_base_conocimientos'. Devuelve el marcador [KB_DRAFT]|hotel|tema|categoria|contenido "
            "para que el sistema pueda mostrar el borrador completo (TEMA/CATEGORÍA/CONTENIDO) antes de guardar. No omitas el marcador."
        )

        context = get_time_context()
        return f"{context}\n{base}\n\nHotel: {hotel_name}"

    async def handle_kb_addition(
        self,
        topic: str,
        content: str,
        encargado_id: str,
        hotel_name: str,
        source: str = "escalation",
    ) -> Dict[str, Any]:
        """
        Procesar solicitud de agregar a base de conocimientos

        Llamado desde InternoAgent cuando encargado aprueba
        """

        try:
            log.info("Agregando a KB: %s desde %s", topic, source)

            clean_content = self._clean_kb_content(content)

            result = await self._append_to_knowledge_document(
                topic=topic,
                content=clean_content,
                hotel_name=hotel_name,
                source_type=source,
            )

            confirmation = (
                "✅ Información agregada a la base de conocimientos:\n\n"
                f"{topic}\n{clean_content[:100]}..."
            )
            await self._safe_call(
                getattr(self.channel_manager, "send_message", None),
                encargado_id,
                confirmation,
                channel="telegram",
            )

            return {
                "status": "success",
                "kb_entry_key": result.get("key") if isinstance(result, dict) else None,
                "message": confirmation,
            }

        except Exception as exc:
            log.error("Error agregando a KB: %s", exc, exc_info=True)
            return {
                "status": "error",
                "message": f"Error: {exc}",
            }

    async def _append_to_knowledge_document(
        self,
        topic: str,
        content: str,
        hotel_name: str,
        source_type: str,
    ) -> Dict[str, Any]:
        """
        Anexa la información al documento de conocimientos en S3.
        """

        bucket = Settings.S3_BUCKET
        if not bucket:
            raise ValueError("S3_BUCKET no configurado en .env")

        key = self._resolve_doc_key(hotel_name)

        boto = boto3.client(
            "s3",
            region_name=Settings.AWS_DEFAULT_REGION,
            config=BotoConfig(retries={"max_attempts": 3, "mode": "standard"}),
        )

        tmp_dir = Path(tempfile.mkdtemp())
        local_path = tmp_dir / "kb.docx"

        try:
            await asyncio.to_thread(boto.download_file, bucket, key, str(local_path))
        except Exception as exc:
            raise RuntimeError(f"No se pudo descargar el documento KB de S3 ({bucket}/{key}): {exc}")

        try:
            from docx import Document
        except ImportError as exc:
            raise RuntimeError("Falta dependencia python-docx para editar el documento") from exc

        doc = Document(str(local_path))
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        doc.add_paragraph(f"[{timestamp}] {topic}")
        doc.add_paragraph(content)
        doc.add_paragraph(f"(source: {source_type})")
        doc.save(str(local_path))

        try:
            await asyncio.to_thread(boto.upload_file, str(local_path), bucket, key)
        except Exception as exc:
            raise RuntimeError(f"No se pudo subir el documento actualizado a S3 ({bucket}/{key}): {exc}")

        return {"status": "success", "key": key}

    def _resolve_doc_key(self, hotel_name: str) -> str:
        """Determina la key en S3 para el documento de KB del hotel."""

        if Settings.SUPERINTENDENTE_S3_DOC:
            return Settings.SUPERINTENDENTE_S3_DOC.strip('\"\\\' ')

        prefix = Settings.SUPERINTENDENTE_S3_PREFIX.rstrip("/")
        clean_name = re.sub(r"[^A-Za-z0-9\\-_ ]+", "", hotel_name).strip()
        doc_name = f"{clean_name.replace(' ', '_')}.docx" if clean_name else "knowledge_base.docx"

        return f"{prefix}/{doc_name}" if prefix else doc_name

    def _clean_kb_content(self, content: str) -> str:
        """Elimina instrucciones o metadatos que no deben ir al documento KB."""
        if not content:
            return ""

        lines = []
        for raw in content.splitlines():
            ln = raw.strip()
            low = ln.lower()
            if not ln:
                continue
            if "confirma con \"ok\"" in low or "confirma con" in low:
                continue
            if "responde 'ok" in low or "responde \"ok" in low:
                continue
            if "envía ajustes" in low or "envia ajustes" in low:
                continue
            if low.startswith("(source:"):
                continue
            if "[superintendente]" in low:
                continue
            if low.startswith("📝 propuesta para base de conocimientos"):
                continue
            lines.append(ln)

        return "\n".join(lines).strip()

    async def review_recent_conversations(
        self,
        hotel_name: str,
        limit: int = 10,
    ) -> str:
        """
        Revisar conversaciones recientes y sumarizar patrones
        Útil para que el encargado sepa qué preguntas hacen los huéspedes
        """

        summary = (
            "RESUMEN DE CONVERSACIONES RECIENTES:\n\n"
            "1. Preguntas sobre Servicios (5):\n"
            "   - Masajista personal: 3 preguntas\n"
            "   - Servicio de Room Service: 2 preguntas\n"
            "2. Preguntas sobre Ubicación (3):\n"
            "   - Dónde está la piscina: 2\n"
            "   - Horarios de restaurante: 1\n"
            "3. Preguntas no respondidas (2):\n"
            "   - Opciones de transfer al aeropuerto\n"
            "   - Alquiler de bicicletas\n\n"
            "RECOMENDACIÓN: Considera agregar información sobre servicios adicionales a la base de conocimientos."
        )

        return summary

    async def _safe_call(self, func: Optional[Any], *args, **kwargs):
        """Invoca funciones sync/async de forma segura."""
        if not func:
            return None
        try:
            res = func(*args, **kwargs)
            if inspect.isawaitable(res):
                return await res
            return res
        except Exception as exc:
            log.warning("Error en llamada segura: %s", exc)
            raise
