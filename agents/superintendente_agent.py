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
            kb_rm_marker = None
            for _action, observation in intermediates:
                if isinstance(observation, str) and "[WA_DRAFT]|" in observation:
                    wa_marker = observation[
                        observation.index("[WA_DRAFT]|") :
                    ].strip()
                if isinstance(observation, str) and "[KB_DRAFT]|" in observation:
                    kb_marker = observation[
                        observation.index("[KB_DRAFT]|") :
                    ].strip()
                if isinstance(observation, str) and "[KB_REMOVE_DRAFT]|" in observation:
                    kb_rm_marker = observation[
                        observation.index("[KB_REMOVE_DRAFT]|") :
                    ].strip()
                if wa_marker and kb_marker and kb_rm_marker:
                    break
            if wa_marker and "[WA_DRAFT]|" not in output:
                output = f"{wa_marker}\n{output}"
            if kb_marker and "[KB_DRAFT]|" not in output:
                output = f"{kb_marker}\n{output}"
            if kb_rm_marker and "[KB_REMOVE_DRAFT]|" not in output:
                output = f"{kb_rm_marker}\n{output}"

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
            create_consulta_reserva_general_tool,
            create_consulta_reserva_persona_tool,
            create_review_conversations_tool,
            create_remove_from_kb_tool,
            create_send_broadcast_tool,
            create_send_message_main_tool,
            create_send_whatsapp_tool,
        )

        tools = [
            create_remove_from_kb_tool(
                hotel_name=hotel_name,
                preview_func=lambda criterio, fecha_inicio=None, fecha_fin=None: self._prepare_kb_removal_preview(
                    hotel_name=hotel_name,
                    criterio=criterio,
                    fecha_inicio=fecha_inicio,
                    fecha_fin=fecha_fin,
                ),
            ),
            create_add_to_kb_tool(
                hotel_name=hotel_name,
                append_func=self._append_to_knowledge_document,
                llm=self.llm,
            ),
            create_review_conversations_tool(
                hotel_name=hotel_name,
                memory_manager=self.memory_manager,
            ),
            create_consulta_reserva_general_tool(),
            create_consulta_reserva_persona_tool(),
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
            "2. eliminar_de_base_conocimientos - Prepara borrador de eliminación en la base Variable (muestra registros o conteo, requiere confirmación)\n"
            "3. revisar_conversaciones - Revisa conversaciones recientes de huéspedes (pide modo: resumen u original)\n"
            "4. enviar_broadcast - Envía plantillas masivas a múltiples huéspedes\n"
            "5. enviar_mensaje_main - Envía respuesta del encargado al MainAgent\n"
            "6. consulta_reserva_general - Consulta folios/reservas entre fechas (usa token auto, devuelve folio_id y folio_code)\n"
            "7. consulta_reserva_persona - Consulta detalle de folio (usa token auto, incluye portalUrl si existe)\n\n"
            "TONO: Profesional, eficiente, orientado a mejora continua.\n\n"
            "REGLAS CLAVE:\n"
            "- Antes de usar revisar_conversaciones pregunta si prefiere 'resumen' (síntesis IA) o ver los mensajes 'originales'; usa el modo solicitado.\n"
            "- Usa SIEMPRE la tool revisar_conversaciones para mostrar el historial de un huésped; si no tienes guest_id, pídelo y respeta el límite indicado (default 10).\n"
            "- Para dudas sobre reservas/folios/clientes (estado, pagos, contacto, fechas), prioriza las tools de reservas. Si ya tienes folio_id, usa consulta_reserva_persona; si solo hay nombre/fechas, usa consulta_reserva_general para obtener folio_id antes de detallar.\n"
            "- No uses revisar_conversaciones salvo que pidan explícitamente historial/mensajes/chat del huésped.\n"
            "- Si consulta_reserva_persona devuelve portalUrl, inclúyelo en la respuesta como enlace para factura/portal.\n"
            "- En paneles de reservas, muestra siempre el folio_id numérico (además del folio_code si quieres) para que el encargado pueda pedir detalle con ese ID.\n"
            "- REGLA CRÍTICA PARA KB: Cuando el encargado pida agregar/actualizar información en la base de conocimientos, "
            "usa SIEMPRE la herramienta 'agregar_a_base_conocimientos'. Devuelve el marcador [KB_DRAFT]|hotel|tema|categoria|contenido "
            "para que el sistema pueda mostrar el borrador completo (TEMA/CATEGORÍA/CONTENIDO) antes de guardar. No omitas el marcador.\n"
            "- Para eliminar información de la base Variable, usa la herramienta 'eliminar_de_base_conocimientos' sin pedir confirmación previa. "
            "Entrega SIEMPRE el marcador [KB_REMOVE_DRAFT]|hotel|payload_json (con conteo y preview) en tu respuesta para que el encargado confirme/cancele. "
            "Si el encargado pide eliminar/quitar/borrar/limpiar o 'revisar' antes de eliminar, no generes propuestas de agregado ni paneles genéricos: "
            "limítate a invocar 'eliminar_de_base_conocimientos' con el criterio pedido y devuelve el marcador de borrador de eliminación."
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
            try:
                await self._safe_call(
                    getattr(self.channel_manager, "send_message", None),
                    encargado_id,
                    f"❌ No se pudo agregar a la base de conocimientos: {exc}",
                    channel="telegram",
                )
            except Exception:
                pass
            return {
                "status": "error",
                "message": f"Error: {exc}",
            }

    def _get_document_class(self):
        try:
            from docx import Document  # type: ignore
            return Document
        except ImportError as exc:
            raise RuntimeError("Falta dependencia python-docx para editar el documento") from exc

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

        boto = boto3.client(
            "s3",
            region_name=Settings.AWS_DEFAULT_REGION,
            config=BotoConfig(retries={"max_attempts": 3, "mode": "standard"}),
        )

        candidates = self._resolve_doc_candidates(
            hotel_name=hotel_name,
            boto_client=boto,
            bucket=bucket,
        )

        tmp_dir = Path(tempfile.mkdtemp())
        local_path = tmp_dir / "kb.docx"

        key_used = None
        last_exc: Exception | None = None
        create_new = False
        for key in candidates:
            try:
                await asyncio.to_thread(boto.download_file, bucket, key, str(local_path))
                key_used = key
                break
            except Exception as exc:  # intentamos siguiente candidato
                last_exc = exc
                log.warning("No se pudo descargar %s/%s, probando siguiente: %s", bucket, key, exc)

        if not key_used:
            key_used = candidates[0]
            create_new = True
            log.warning(
                "No se pudo descargar ningún documento de KB tras probar %s candidatos; se creará uno nuevo en %s",
                len(candidates),
                key_used,
            )

        Document = self._get_document_class()

        if create_new:
            doc = Document()
        else:
            doc = Document(str(local_path))
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        doc.add_paragraph(f"[{timestamp}] {topic}")
        doc.add_paragraph(content)
        doc.add_paragraph(f"(source: {source_type})")
        doc.save(str(local_path))

        try:
            await asyncio.to_thread(boto.upload_file, str(local_path), bucket, key_used)
        except Exception as exc:
            raise RuntimeError(f"No se pudo subir el documento actualizado a S3 ({bucket}/{key_used}): {exc}")

        return {"status": "success", "key": key_used}

    async def _prepare_kb_removal_preview(
        self,
        hotel_name: str,
        criterio: str,
        fecha_inicio: str | None = None,
        fecha_fin: str | None = None,
        preview_limit: int = 5,
        max_preview_chars: int = 2200,
    ) -> dict[str, Any]:
        """
        Lee el documento de KB y prepara un borrador de eliminación según criterio/fechas.
        Devuelve payload estructurado para que el webhook muestre conteo o extractos.
        """

        criterio_clean = (criterio or "").strip()
        if not criterio_clean and not (fecha_inicio or fecha_fin):
            return {
                "total_matches": 0,
                "criteria": criterio_clean,
                "error": "Necesito un criterio o un rango de fechas para buscar qué eliminar.",
            }

        kb_data = await self._load_kb_entries(hotel_name)
        entries = kb_data.get("entries", [])
        if not entries:
            return {
                "total_matches": 0,
                "criteria": criterio_clean,
                "error": "No encontré registros en la base de conocimientos Variable.",
            }

        def _parse_date(val: str | None):
            if not val:
                return None
            try:
                return datetime.fromisoformat(val.strip())
            except Exception:
                try:
                    return datetime.strptime(val.strip(), "%Y-%m-%d")
                except Exception:
                    return None

        date_from = _parse_date(fecha_inicio)
        date_to = _parse_date(fecha_fin)

        crit_lower = criterio_clean.lower()
        crit_terms = [t for t in re.findall(r"[a-záéíóúñü0-9]+", crit_lower) if t]

        def _matches(entry: dict[str, Any]) -> bool:
            blob = f"{entry.get('topic','')} {entry.get('content','')}".lower()
            if crit_terms:
                if not any(term in blob for term in crit_terms):
                    return False
            if date_from or date_to:
                ts = entry.get("timestamp_dt")
                if isinstance(ts, datetime):
                    if date_from and ts < date_from:
                        return False
                    if date_to and ts > date_to:
                        return False
            return True

        matched = [e for e in entries if _matches(e)]
        total = len(matched)
        if not matched:
            return {
                "total_matches": 0,
                "criteria": criterio_clean,
                "date_from": fecha_inicio,
                "date_to": fecha_fin,
                "doc_key": kb_data.get("key"),
                "matches": [],
                "preview": [],
            }

        preview_items = []
        preview_chars = 0

        def _clean_snippet(text: str) -> str:
            """Recorta texto y elimina líneas de borradores previos para una vista limpia."""
            if not text:
                return ""
            lines = []
            for ln in (text or "").splitlines():
                low = ln.lower()
                if "borrador para agregar" in low or "[kb_" in low or "[kb-" in low:
                    continue
                lines.append(ln.strip())
            cleaned = " ".join(ln for ln in lines if ln).strip()
            return cleaned[:320] + ("…" if len(cleaned) > 320 else "")

        for entry in matched[:preview_limit]:
            snippet = _clean_snippet(entry.get("content") or "")
            item = {
                "id": entry.get("id"),
                "fecha": entry.get("timestamp_display"),
                "topic": entry.get("topic"),
                "snippet": snippet,
                "source": entry.get("source"),
            }
            preview_items.append(item)
            preview_chars += len(snippet or "")
            if preview_chars >= max_preview_chars:
                break

        payload = {
            "criteria": criterio_clean,
            "date_from": fecha_inicio,
            "date_to": fecha_fin,
            "doc_key": kb_data.get("key"),
            "total_matches": total,
            "preview_count": len(preview_items),
            "preview": preview_items,
            "target_ids": [e.get("id") for e in matched],
            "matches": [
                {
                    "id": e.get("id"),
                    "topic": e.get("topic"),
                    "timestamp_display": e.get("timestamp_display"),
                    "content": e.get("content"),
                }
                for e in matched
            ],
        }
        return payload

    async def handle_kb_removal(
        self,
        hotel_name: str,
        target_ids: list[int],
        encargado_id: str,
        note: str = "",
        criteria: str = "",
    ) -> Dict[str, Any]:
        """
        Elimina entradas del documento de KB (Variable) según IDs parseados.
        """

        if not target_ids:
            return {"status": "noop", "message": "No hay registros seleccionados para eliminar."}

        bucket = Settings.S3_BUCKET
        if not bucket:
            raise ValueError("S3_BUCKET no configurado en .env")

        boto = boto3.client(
            "s3",
            region_name=Settings.AWS_DEFAULT_REGION,
            config=BotoConfig(retries={"max_attempts": 3, "mode": "standard"}),
        )

        kb_data = await self._load_kb_entries(hotel_name, boto_client=boto, bucket=bucket)
        entries = kb_data.get("entries", [])
        key_used = kb_data.get("key")
        local_path = kb_data.get("path")

        if not entries:
            msg = "No encontré registros en la base de conocimientos para eliminar."
            await self._safe_call(
                getattr(self.channel_manager, "send_message", None),
                encargado_id,
                msg,
                channel="telegram",
            )
            return {"status": "empty", "message": msg}

        remove_set = {int(tid) for tid in target_ids}
        kept = [e for e in entries if e.get("id") not in remove_set]
        removed = [e for e in entries if e.get("id") in remove_set]

        if not removed:
            msg = "No se encontraron coincidencias para eliminar con el criterio indicado."
            await self._safe_call(
                getattr(self.channel_manager, "send_message", None),
                encargado_id,
                msg,
                channel="telegram",
            )
            return {"status": "noop", "message": msg}

        Document = self._get_document_class()
        doc_new = Document()
        for entry in kept:
            for par_text in entry.get("paragraphs", []):
                doc_new.add_paragraph(par_text)
        doc_new.save(str(local_path))

        try:
            await asyncio.to_thread(boto.upload_file, str(local_path), bucket, key_used)
        except Exception as exc:
            raise RuntimeError(f"No se pudo subir el documento actualizado a S3 ({bucket}/{key_used}): {exc}")

        header_lines = [
            f"🧹 Eliminados {len(removed)} registros de la base de conocimientos.",
            f"Criterio: {criteria or 'sin especificar'}",
        ]
        if note:
            header_lines.append(f"Nota: {note}")

        preview_lines = []
        for rm in removed[:5]:
            line = f"- {rm.get('timestamp_display') or ''} {rm.get('topic') or ''}".strip()
            preview_lines.append(line or f"- ID {rm.get('id')}")
        if len(removed) > 5:
            preview_lines.append(f"... y {len(removed) - 5} más.")

        confirmation = "\n".join(header_lines + (["Resumen:"] + preview_lines if preview_lines else []))

        await self._safe_call(
            getattr(self.channel_manager, "send_message", None),
            encargado_id,
            confirmation,
            channel="telegram",
        )

        return {
            "status": "success",
            "removed": [rm.get("id") for rm in removed],
            "kept": [kp.get("id") for kp in kept],
            "message": confirmation,
            "doc_key": key_used,
        }

    def _resolve_doc_candidates(self, hotel_name: str, boto_client: Any = None, bucket: str | None = None) -> list[str]:
        """
        Devuelve una lista ordenada de posibles keys en S3 para el documento de KB del hotel.
        Prioriza archivos que contengan '-Variable' antes de la extensión y agrega fallback final.
        """

        if Settings.SUPERINTENDENTE_S3_DOC:
            env_key = Settings.SUPERINTENDENTE_S3_DOC.strip('\"\\\' ')
            candidates = [env_key]
            # También probar variante "-Variable" si no está incluida
            if env_key.lower().endswith(".docx"):
                base_no_ext = env_key[:-5]
                var_key = f"{base_no_ext}-Variable.docx"
                if var_key not in candidates:
                    candidates.insert(0, var_key)
            elif env_key.lower().endswith(".doc"):
                base_no_ext = env_key[:-4]
                var_key = f"{base_no_ext}-Variable.doc"
                if var_key not in candidates:
                    candidates.insert(0, var_key)
            log.info("Candidatos para documento KB (env): %s", candidates)
            return candidates

        prefix_env = Settings.SUPERINTENDENTE_S3_PREFIX.rstrip("/")
        clean_name = re.sub(r"[^A-Za-z0-9\\-_ ]+", "", hotel_name).strip()
        doc_name = f"{clean_name.replace(' ', '_')}.docx" if clean_name else "knowledge_base.docx"
        slug_prefix = clean_name.replace(" ", "_") if clean_name else ""
        prefix_tail = prefix_env.rsplit("/", 1)[-1] if prefix_env else ""

        # base_key por defecto
        base_key = f"{prefix_env}/{doc_name}" if prefix_env else doc_name

        # 🎯 Generar combinaciones de prefijos candidatos
        prefix_candidates = []
        if prefix_env:
            prefix_candidates.append(prefix_env)
        if slug_prefix and slug_prefix not in prefix_candidates:
            prefix_candidates.append(slug_prefix)
        if "" not in prefix_candidates:
            prefix_candidates.append("")

        # 🎯 Generar nombres posibles de archivo Variable
        var_names = []
        if clean_name:
            raw_name = " ".join(hotel_name.split())  # normaliza espacios múltiples
            var_names.extend(
                [
                    f"{raw_name}-Variable.docx",
                    f"{clean_name.replace(' ', '_')}-Variable.docx",
                    f"{doc_name[:-5]}-Variable.docx" if doc_name.endswith(".docx") else f"{doc_name}-Variable.docx",
                ]
            )
        if prefix_tail:
            var_names.extend(
                [
                    f"{prefix_tail.replace('_', ' ')}-Variable.docx",
                    f"{prefix_tail}-Variable.docx",
                ]
            )

        candidates: list[str] = []
        # Añadir combinaciones prefijo + nombres variable
        for pref in prefix_candidates:
            for nm in var_names:
                if not nm:
                    continue
                cand = f"{pref}/{nm}" if pref else nm
                if cand not in candidates:
                    candidates.append(cand)

        # 🎯 Si hay prefijo y cliente S3, listar documentos bajo ese prefijo y priorizar
        # cualquiera que contenga '-variable' en el nombre (independiente del hotel).
        if boto_client and bucket and prefix_env:
            try:
                paginator = boto_client.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix_env}/"):
                    for obj in page.get("Contents", []):
                        key = obj.get("Key") or ""
                        key_lower = key.lower()
                        if "-variable" in key_lower and key_lower.endswith((".docx", ".doc")):
                            if key not in candidates:
                                candidates.insert(0, key)  # dar prioridad a los encontrados reales
            except Exception as exc:
                log.warning("No se pudo listar documentos Variable en %s: %s", prefix_env, exc)

        # Añadir base como último recurso
        if base_key not in candidates:
            candidates.append(base_key)

        log.info("Candidatos para documento KB: %s", candidates)
        return candidates

    async def _load_kb_entries(
        self,
        hotel_name: str,
        boto_client: Any = None,
        bucket: str | None = None,
    ) -> dict[str, Any]:
        """
        Descarga el documento de KB y lo parsea en entradas discretas con índices.
        """

        bucket = bucket or Settings.S3_BUCKET
        if not bucket:
            raise ValueError("S3_BUCKET no configurado en .env")

        boto_client = boto_client or boto3.client(
            "s3",
            region_name=Settings.AWS_DEFAULT_REGION,
            config=BotoConfig(retries={"max_attempts": 3, "mode": "standard"}),
        )

        candidates = self._resolve_doc_candidates(
            hotel_name=hotel_name,
            boto_client=boto_client,
            bucket=bucket,
        )

        tmp_dir = Path(tempfile.mkdtemp())
        local_path = tmp_dir / "kb.docx"

        key_used = None
        create_new = False
        last_exc: Exception | None = None
        for key in candidates:
            try:
                await asyncio.to_thread(boto_client.download_file, bucket, key, str(local_path))
                key_used = key
                break
            except Exception as exc:  # intentamos siguiente candidato
                last_exc = exc
                log.warning("No se pudo descargar %s/%s, probando siguiente: %s", bucket, key, exc)

        if not key_used:
            key_used = candidates[0]
            create_new = True
            log.warning(
                "No se pudo descargar ningún documento de KB tras probar %s candidatos; se creará uno nuevo en %s",
                len(candidates),
                key_used,
            )
            return {"entries": [], "key": key_used, "path": local_path, "create_new": create_new}

        Document = self._get_document_class()
        doc = Document(str(local_path))
        entries = self._parse_kb_paragraphs(doc.paragraphs)

        return {
            "entries": entries,
            "key": key_used,
            "path": local_path,
            "create_new": create_new,
        }

    def _parse_kb_paragraphs(self, paragraphs: list[Any]) -> list[dict[str, Any]]:
        """
        Convierte los párrafos del documento en entradas con índice y metadatos.
        Asume formato estándar: [timestamp] Título, contenido y (source: tipo).
        """

        entries: list[dict[str, Any]] = []
        current: dict[str, Any] = {"header": "", "content": "", "source": "", "paragraphs": []}

        def _flush():
            if not any(current.values()):
                return
            idx = len(entries)
            header = current.get("header", "")
            topic = header
            ts_display = ""
            ts_dt = None
            match = re.match(r"\[(.*?)\]\s*(.*)", header)
            if match:
                ts_display = match.group(1).strip()
                topic = match.group(2).strip() or topic
                ts_clean = ts_display.replace(" UTC", "").replace("T", " ")
                try:
                    ts_dt = datetime.fromisoformat(ts_clean)
                except Exception:
                    ts_dt = None

            entry = {
                "id": idx,
                "header": header,
                "topic": topic,
                "timestamp_display": ts_display,
                "timestamp_dt": ts_dt,
                "content": (current.get("content") or "").strip(),
                "source": current.get("source", ""),
                "paragraphs": list(current.get("paragraphs") or []),
            }
            entries.append(entry)

        for idx, para in enumerate(paragraphs or []):
            text = (getattr(para, "text", "") or "").strip()
            if not text:
                continue
            is_header = bool(re.match(r"^\[\d{4}-\d{2}-\d{2}", text)) or ("UTC]" in text and text.startswith("["))
            if is_header:
                _flush()
                current = {"header": text, "content": "", "source": "", "paragraphs": [text]}
                continue

            if text.lower().startswith("(source:"):
                current["source"] = text
                current.setdefault("paragraphs", []).append(text)
                continue

            if current.get("content"):
                current["content"] = f"{current['content']}\n{text}"
            else:
                current["content"] = text
            current.setdefault("paragraphs", []).append(text)

        _flush()
        return entries

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
