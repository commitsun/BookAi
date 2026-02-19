"""InfoAgent v5 — AgentExecutor con KB + Google Search.

Este módulo implementa la arquitectura propuesta en la incidencia:
- Tool 1: Base de conocimientos (MCP)
- Tool 2: Búsqueda en Google (placeholder)
El LLM decide qué herramienta usar y solo escala si ambas fallan.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import ClassVar, List, Optional, Tuple

from langchain.agents import AgentExecutor, create_openai_tools_agent
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.tools import BaseTool
from pydantic import BaseModel, Field

from core.config import ModelConfig, ModelTier
from core.language_manager import language_manager
from core.mcp_client import get_tools
from core.utils.normalize_reply import normalize_reply
from core.utils.time_context import get_time_context
from core.utils.utils_prompt import load_prompt
from core.utils.dynamic_context import build_dynamic_context_from_memory

log = logging.getLogger("InfoAgent")

ESCALATION_TOKEN = "ESCALATION_REQUIRED"


async def _invoke_google_search(query: str) -> Optional[str]:
    """
    Consulta la herramienta `google` expuesta por el MCP para InfoAgent.
    Devuelve el texto limpio o None si no hay resultados útiles.
    """
    question = (query or "").strip()
    if not question:
        return None

    try:
        tools = await get_tools(server_name="InfoAgent")
        google_tool = next((t for t in tools if "google" in t.name.lower()), None)
        if not google_tool:
            log.warning("GoogleSearchTool: no se encontró la tool 'google' en el MCP.")
            return None

        raw_reply = await google_tool.ainvoke({"query": question})
        cleaned = normalize_reply(raw_reply, question, "InfoAgent").strip()
        if not cleaned or len(cleaned) < 5:
            return None
        return cleaned

    except Exception as exc:
        log.error("GoogleSearchTool: error consultando MCP: %s", exc, exc_info=True)
        return None


class GoogleSearchInput(BaseModel):
    """Schema para búsqueda en Google."""

    query: str = Field(
        ...,
        description="Consulta a buscar en Google (máximo 100 caracteres).",
        max_length=100,
    )


class GoogleSearchTool(BaseTool):
    """Tool que realiza búsquedas en Google usando un placeholder."""

    name: ClassVar[str] = "google_search"
    description: ClassVar[str] = (
        "Busca información en Google usando Gemini API. Úsalo cuando la base "
        "de conocimientos no tenga respuesta o la información sea insuficiente."
    )
    args_schema: ClassVar[type[BaseModel]] = GoogleSearchInput

    async def _arun(self, query: str) -> str:
        query = (query or "").strip()
        if not query:
            return "Necesito una consulta para buscar en Google."

        try:
            log.info("GoogleSearchTool: consultando MCP para %s", query[:80])
            result_text = await _invoke_google_search(query)
            if not result_text:
                return "Google Search no devolvió resultados útiles."
            return result_text
        except Exception as exc:
            log.error("Error en GoogleSearchTool: %s", exc, exc_info=True)
            return f"Error buscando en Google: {exc}"

    def _run(self, query: str) -> str:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(self._arun(query))


class KBSearchInput(BaseModel):
    query: str = Field(
        ...,
        description="Consulta sobre servicios, políticas, horarios o ubicación del hotel.",
    )


class KBSearchTool(BaseTool):
    """Tool que consulta la base de conocimientos (MCP)."""

    name: ClassVar[str] = "base_conocimientos"
    description: ClassVar[str] = (
        "Busca información en la base de conocimientos del hotel. "
        "Úsalo siempre antes de intentar otras opciones."
    )
    args_schema: ClassVar[type[BaseModel]] = KBSearchInput
    memory_manager: Optional[object] = None
    chat_id: str = ""

    @staticmethod
    def _dedupe_chunks(chunks: List[str]) -> List[str]:
        seen = set()
        out: List[str] = []
        for chunk in chunks:
            value = " ".join((chunk or "").split()).strip()
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(chunk.strip())
        return out

    @staticmethod
    def _pick_top_indices(raw_text: str, max_idx: int, top_k: int) -> List[int]:
        text = (raw_text or "").strip()
        if not text:
            return list(range(min(top_k, max_idx)))

        # Intento 1: JSON estricto
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                idxs = [int(x) for x in parsed if isinstance(x, int) or (isinstance(x, str) and x.isdigit())]
                idxs = [i for i in idxs if 0 <= i < max_idx]
                if idxs:
                    return idxs[:top_k]
        except Exception:
            pass

        # Intento 2: extraer números en texto libre
        numbers = [int(x) for x in re.findall(r"\d+", text)]
        numbers = [i for i in numbers if 0 <= i < max_idx]
        if numbers:
            # mantener orden y quitar duplicados
            seen = set()
            out = []
            for i in numbers:
                if i in seen:
                    continue
                seen.add(i)
                out.append(i)
            if out:
                return out[:top_k]

        return list(range(min(top_k, max_idx)))

    async def _rerank_chunks(self, question: str, chunks: List[str], top_k: int = 3) -> List[str]:
        clean_chunks = self._dedupe_chunks(chunks)
        if not clean_chunks:
            return []
        if len(clean_chunks) <= top_k:
            return clean_chunks
        try:
            llm = ModelConfig.get_llm(ModelTier.SUBAGENT)
            chunk_block = "\n\n".join(f"[{i}] {c}" for i, c in enumerate(clean_chunks))
            raw = await llm.ainvoke(
                [
                    {
                        "role": "system",
                        "content": (
                            "Selecciona los fragmentos MÁS relevantes para responder la pregunta.\n"
                            "Devuelve SOLO un JSON array con índices en orden de relevancia.\n"
                            "Ejemplo: [2,0,1]\n"
                            "No añadas texto adicional."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"Pregunta:\n{question}\n\nFragmentos:\n{chunk_block}",
                    },
                ]
            )
            reply = (getattr(raw, "content", None) or str(raw or "")).strip()
            top_indices = self._pick_top_indices(reply, max_idx=len(clean_chunks), top_k=top_k)
            return [clean_chunks[i] for i in top_indices]
        except Exception as exc:
            log.debug("KBSearchTool: rerank fallback por error: %s", exc)
            return clean_chunks[:top_k]

    async def _arun(self, query: str) -> Optional[str]:
        question = (query or "").strip()
        if not question:
            return "Por favor, formula una pregunta concreta."

        question_norm = (
            question.lower()
            .replace("check-out", "checkout")
            .replace("check out", "checkout")
            .replace("check-in", "checkin")
            .replace("check in", "checkin")
        )

        def _normalize_kb_name(value: Optional[str]) -> Optional[str]:
            if not value:
                return None
            cleaned = str(value).strip()
            cleaned = cleaned.replace("ponferrrada", "ponferrada")
            return cleaned or None

        def _extract_focus_term(text: str) -> Optional[str]:
            normalized = (
                text.lower()
                .replace("check-out", "checkout")
                .replace("check out", "checkout")
                .replace("check-in", "checkin")
                .replace("check in", "checkin")
            )
            words = re.findall(r"[a-záéíóúüñ]+", normalized)
            if not words:
                return None
            # Evitar palabras muy genéricas
            stop = {
                "el", "la", "los", "las", "un", "una", "de", "del", "que", "es", "hay", "tiene",
                "hotel", "como", "funciona", "precio", "opcion", "opciones", "posibilidad", "horario",
            }
            focus = [w for w in words if w not in stop]
            return focus[-1] if focus else words[-1]

        def _preview(text: str, max_len: int = 240) -> str:
            one_line = " ".join((text or "").split())
            return (one_line[:max_len] + "…") if len(one_line) > max_len else one_line

        def _dedupe_keep_order(items: List[str]) -> List[str]:
            seen = set()
            out: List[str] = []
            for item in items:
                value = (item or "").strip()
                if not value:
                    continue
                key = value.lower()
                if key in seen:
                    continue
                seen.add(key)
                out.append(value)
            return out

        try:
            tools = await get_tools(server_name="InfoAgent")
            if not tools:
                log.warning("KBSearchTool: no hay herramientas MCP disponibles.")
                return None

            kb_tool = next((t for t in tools if "conocimiento" in t.name.lower()), None)
            if not kb_tool:
                log.warning("KBSearchTool: no se encontró la herramienta de conocimientos.")
                return None

            detected_lang = "es"
            translated_to_es = ""
            try:
                detected_lang = language_manager.detect_language(question, prev_lang="es")
                if detected_lang != "es":
                    translated_to_es = language_manager.translate_if_needed(question, detected_lang, "es").strip()
            except Exception as lang_exc:
                log.debug("KBSearchTool: no se pudo detectar/traducir idioma: %s", lang_exc)

            query_variants = _dedupe_keep_order([question, translated_to_es])
            if detected_lang != "es":
                log.info(
                    "KBSearchTool: consulta multilenguaje detectada lang=%s variantes=%s",
                    detected_lang,
                    len(query_variants),
                )

            payload = {"input": question}
            if self.memory_manager and self.chat_id:
                try:
                    instance_url = self.memory_manager.get_flag(self.chat_id, "instance_url")
                    property_id = self.memory_manager.get_flag(self.chat_id, "property_id")
                    kb_name = _normalize_kb_name(self.memory_manager.get_flag(self.chat_id, "kb"))
                    if not kb_name:
                        kb_name = _normalize_kb_name(self.memory_manager.get_flag(self.chat_id, "knowledge_base"))
                except Exception:
                    instance_url = None
                    property_id = None
                    kb_name = None

                if instance_url:
                    payload["instance_url"] = instance_url
                if property_id is not None:
                    payload["property_id"] = property_id
                if kb_name:
                    payload["kb"] = kb_name
                    payload["knowledge_base"] = kb_name
                else:
                    log.warning("KBSearchTool: falta kb/knowledge_base en memoria.")
                    return None

                if "instance_url" not in payload or "property_id" not in payload:
                    log.warning("KBSearchTool: falta contexto dinamico (instance_url/property_id).")
                    return None

            base_payload = payload.copy()

            def _is_invalid(text: str) -> Tuple[bool, str]:
                if not text or len(text) < 10:
                    return True, "empty_or_short"
                lowered = text.lower()
                technical_error_patterns = [
                    r"\btraceback\b",
                    r"\bexception\b",
                    r"\bsyntax error\b",
                    r"\brelation .* does not exist\b",
                    r"\bcolumn .* does not exist\b",
                    r"\btool .* error\b",
                    r"\bhttp(?:\s+request)?\s+\d{3}\b",
                ]
                if any(re.search(pattern, lowered) for pattern in technical_error_patterns):
                    return True, "technical_error_pattern"
                no_info_tokens = [
                    "no dispongo",
                    "no tengo información",
                    "no hay resultados",
                    "no encontrado",
                    "no se encontró",
                ]
                if any(tok in lowered for tok in no_info_tokens):
                    return True, "no_info_pattern"
                return False, "ok"

            def _looks_like_no_answer(text: str) -> bool:
                lowered = (text or "").lower()
                markers = [
                    "no dispongo",
                    "no tengo información",
                    "no hay resultados",
                    "no se encontró",
                    "necesito escalar",
                ]
                return any(m in lowered for m in markers)

            for variant in query_variants:
                attempt_payload = base_payload.copy()
                attempt_payload["input"] = variant
                raw_reply = await kb_tool.ainvoke(attempt_payload)
                ranked_chunks: List[str] = []
                if isinstance(raw_reply, list):
                    log.info("KBSearchTool: chunks recuperados=%s", len(raw_reply))
                    normalized_chunks = [
                        normalize_reply(item, variant, "InfoAgent").strip()
                        for item in raw_reply
                    ]
                    normalized_chunks = [c for c in normalized_chunks if c]
                    ranked_chunks = await self._rerank_chunks(variant, normalized_chunks, top_k=3)

                cleaned = (
                    "\n\n".join(ranked_chunks).strip()
                    if ranked_chunks
                    else normalize_reply(raw_reply, variant, "InfoAgent").strip()
                )
                is_invalid, invalid_reason = _is_invalid(cleaned)
                log.info("KBSearchTool: respuesta KB (preview): %s", _preview(cleaned))
                fallback_needed = is_invalid or _looks_like_no_answer(cleaned)

                if not fallback_needed:
                    log.info("KBSearchTool: información obtenida correctamente.")
                    return cleaned

                focus = _extract_focus_term(variant)
                if focus and focus != variant.strip().lower():
                    log.info("KBSearchTool: reintentando KB con término focal '%s'", focus)
                    retry_payload = base_payload.copy()
                    retry_payload["input"] = focus
                    raw_retry = await kb_tool.ainvoke(retry_payload)
                    ranked_retry_chunks: List[str] = []
                    if isinstance(raw_retry, list):
                        log.info("KBSearchTool: chunks recuperados (reintento)=%s", len(raw_retry))
                        normalized_retry_chunks = [
                            normalize_reply(item, focus, "InfoAgent").strip()
                            for item in raw_retry
                        ]
                        normalized_retry_chunks = [c for c in normalized_retry_chunks if c]
                        ranked_retry_chunks = await self._rerank_chunks(focus, normalized_retry_chunks, top_k=3)
                    cleaned_retry = (
                        "\n\n".join(ranked_retry_chunks).strip()
                        if ranked_retry_chunks
                        else normalize_reply(raw_retry, focus, "InfoAgent").strip()
                    )
                    retry_invalid, retry_reason = _is_invalid(cleaned_retry)
                    log.info("KBSearchTool: respuesta KB reintento (preview): %s", _preview(cleaned_retry))
                    if not retry_invalid:
                        log.info("KBSearchTool: información obtenida correctamente (reintento).")
                        return cleaned_retry
                    log.info("KBSearchTool: reintento inválido (motivo=%s).", retry_reason)

                log.info("KBSearchTool: intento inválido (motivo=%s).", invalid_reason)

            log.info("KBSearchTool: KB no tiene información útil tras variantes/reintentos.")
            return None
        except Exception as exc:
            log.error("KBSearchTool error: %s", exc, exc_info=True)
            return None

    def _run(self, query: str) -> Optional[str]:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(self._arun(query))


class InfoAgent:
    """Agente factual basado en AgentExecutor con múltiples herramientas."""

    def __init__(self, memory_manager=None):
        self.memory_manager = memory_manager
        self.llm = ModelConfig.get_llm(ModelTier.SUBAGENT)
        self.tools: List[BaseTool] = [KBSearchTool(), GoogleSearchTool()]

        base_prompt = _load_info_prompt()
        self.base_prompt = base_prompt or (
            "Eres un agente especializado en información del hotel. "
            "Responde solo con datos verificados y evita inventar."
        )

        log.info("InfoAgent v5 inicializado con AgentExecutor y herramientas múltiples.")

    async def ainvoke(
        self,
        user_input: str,
        chat_id: str,
        chat_history: Optional[List] = None,
        context_window: int = 10,
    ) -> str:
        if not user_input:
            return ESCALATION_TOKEN

        kb_tool = KBSearchTool(memory_manager=self.memory_manager, chat_id=chat_id)
        google_tool = GoogleSearchTool()
        try:
            # Flujo determinista: siempre MCP-KB primero, luego fallback a Google.
            kb_output = (await kb_tool._arun(user_input) or "").strip()
            if kb_output and not self._needs_escalation(kb_output):
                reduced = await self._reduce_to_question_scope(
                    question=user_input,
                    source_text=kb_output,
                )
                # Si KB ya tiene información útil y el reducer generó respuesta,
                # no activamos Google.
                if reduced:
                    return reduced
                # Si KB respondió pero no se pudo reducir bien, no buscamos fuera:
                # escalamos para evitar ruido y latencia innecesaria.
                return ESCALATION_TOKEN

            google_output = (await google_tool._arun(user_input) or "").strip()
            if self._google_needs_location_hint(google_output):
                contextual_query = self._build_contextual_google_query(chat_id, user_input)
                if contextual_query and contextual_query.strip() != (user_input or "").strip():
                    log.info("InfoAgent: reintentando Google con contexto de propiedad.")
                    google_output = (await google_tool._arun(contextual_query) or "").strip()
            if (
                google_output
                and "no devolvió resultados útiles" not in google_output.lower()
                and "necesito una consulta para buscar en google" not in google_output.lower()
                and not self._needs_escalation(google_output)
            ):
                reduced = await self._reduce_to_question_scope(
                    question=user_input,
                    source_text=google_output,
                )
                if reduced and await self._verify_answer_supported(
                    question=user_input,
                    answer=reduced,
                    source_text=google_output,
                ):
                    return reduced
        except Exception as exc:
            log.error("Error ejecutando InfoAgent: %s", exc, exc_info=True)
            return f"Error consultando la información del hotel: {exc}"

        return ESCALATION_TOKEN

    async def handle(self, pregunta: str, chat_id: str, chat_history=None, **_) -> str:
        return await self.ainvoke(
            user_input=pregunta,
            chat_id=chat_id,
            chat_history=chat_history,
        )

    @staticmethod
    def _needs_escalation(text: str) -> bool:
        lowered = text.lower()
        triggers = [
            "no tengo información",
            "no dispongo",
            "consultar al encargado",
            "necesito escalar",
            "no puedo confirmarlo",
            "escalar al encargado",
        ]
        return any(token in lowered for token in triggers)

    @staticmethod
    def _google_needs_location_hint(text: str) -> bool:
        lowered = (text or "").lower()
        hints = [
            "necesito que me proporciones la ubicación",
            "necesito la ubicación",
            "indícame el nombre del hotel",
            "indícame la dirección",
            "para poder ayudarte a encontrar",
        ]
        return any(token in lowered for token in hints)

    def _build_contextual_google_query(self, chat_id: str, user_input: str) -> str:
        base = (user_input or "").strip()
        if not base or not self.memory_manager or not chat_id:
            return base
        try:
            property_name = (
                self.memory_manager.get_flag(chat_id, "property_name")
                or self.memory_manager.get_flag(chat_id, "instance_hotel_code")
                or ""
            )
            property_city = self.memory_manager.get_flag(chat_id, "property_city") or ""
            property_address = self.memory_manager.get_flag(chat_id, "property_address") or ""
            location_parts = [str(x).strip() for x in [property_name, property_address, property_city] if str(x or "").strip()]
            if not location_parts:
                return base
            return f"{base} cerca de {' - '.join(location_parts)}"
        except Exception:
            return base

    async def _reduce_to_question_scope(self, question: str, source_text: str) -> Optional[str]:
        q = (question or "").strip()
        src = (source_text or "").strip()
        if not q or not src:
            return None

        system_prompt = (
            "Eres un reductor de alcance para atención hotelera.\n"
            "Tu tarea es responder SOLO lo que el huésped pregunta usando únicamente la fuente dada.\n"
            "Reglas obligatorias:\n"
            "1) No añadas información no pedida.\n"
            "2) No mezcles secciones no relacionadas (bodas, grupos, eventos) salvo que se pidan explícitamente.\n"
            "3) Si la fuente no contiene el dato solicitado, devuelve EXACTAMENTE: INSUFFICIENT_CONTEXT\n"
            "4) Respuesta clara y directa.\n"
            "5) Si la pregunta es amplia (ej. políticas, normas, condiciones), devuelve un resumen estructurado "
            "con puntos clave completos y concretos.\n"
            "6) Si la pregunta es específica (ej. ubicación, horario, precio), responde de forma breve.\n"
            "7) Mantén el idioma del mensaje del huésped.\n"
            "8) No deduzcas, no completes huecos, no uses conocimiento externo.\n"
            "9) Si falta un dato clave para responder correctamente, devuelve EXACTAMENTE: INSUFFICIENT_CONTEXT\n"
            "10) Si la respuesta implica una limitación (no disponible / no regulable / no permitido), "
            "mantén el dato principal y añade una frase breve de ayuda práctica.\n"
            "11) Esa frase de ayuda debe salir de la fuente; si la fuente no aporta alternativa concreta, "
            "ofrece consultar con recepción/equipo sin prometer cambios.\n"
        )
        user_prompt = (
            f"Pregunta del huésped:\n{q}\n\n"
            f"Fuente disponible:\n{src}\n\n"
            "Devuelve solo la respuesta final para el huésped."
        )

        try:
            raw = await self.llm.ainvoke(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
            )
            reduced = (getattr(raw, "content", None) or str(raw or "")).strip()
            if not reduced:
                return None
            if reduced.upper() == "INSUFFICIENT_CONTEXT":
                return None
            if len(reduced) > 1600:
                return reduced[:1600].rstrip()
            return reduced
        except Exception as exc:
            log.warning("InfoAgent reducer: fallo al reducir respuesta: %s", exc)
            return None

    async def _verify_answer_supported(self, question: str, answer: str, source_text: str) -> bool:
        q = (question or "").strip()
        a = (answer or "").strip()
        src = (source_text or "").strip()
        if not q or not a or not src:
            return False
        try:
            raw = await self.llm.ainvoke(
                [
                    {
                        "role": "system",
                        "content": (
                            "Evalúa soporte factual.\n"
                            "Responde SOLO YES o NO.\n"
                            "YES únicamente si TODAS las afirmaciones de la respuesta están soportadas por la fuente."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Pregunta:\n{q}\n\n"
                            f"Respuesta:\n{a}\n\n"
                            f"Fuente:\n{src}\n\n"
                            "¿La respuesta está 100% soportada por la fuente?"
                        ),
                    },
                ]
            )
            verdict = (getattr(raw, "content", None) or str(raw or "")).strip().upper()
            ok = verdict.startswith("YES")
            if not ok:
                log.info("InfoAgent verifier: respuesta rechazada por soporte insuficiente.")
            return ok
        except Exception as exc:
            log.warning("InfoAgent verifier: error validando soporte factual: %s", exc)
            return False


def _load_info_prompt() -> Optional[str]:
    try:
        return load_prompt("info_hotel_prompt.txt").strip()
    except FileNotFoundError:
        return None
    except Exception as exc:
        log.warning("No se pudo cargar info_hotel_prompt: %s", exc)
        return None
