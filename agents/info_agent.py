"""
📚 InfoAgent v3 (modo factual y sin invenciones)
==========================================================================================
Responde preguntas generales sobre el hotel: servicios, horarios, políticas, etc.
Usa exclusivamente la base de conocimientos (API HTTP del MCP Server)
y escala al encargado si no hay información válida.
"""

import re
import logging
import asyncio
from langchain_openai import ChatOpenAI

from core.language_manager import language_manager
from core.utils.normalize_reply import normalize_reply
from core.mcp_client import call_knowledge_base  # 👈 usamos el nuevo método HTTP
from core.utils.time_context import get_time_context
from agents.interno_agent import InternoAgent  # 👈 Escalación interna

log = logging.getLogger("InfoAgent")

ESCALATE_SENTENCE = (
    "🕓 Un momento por favor, voy a consultarlo con el encargado. "
    "Permíteme contactar con el encargado."
)

# =====================================================
# 🔍 Helper: detectar si parece volcado técnico interno
# =====================================================
def _looks_like_internal_dump(text: str) -> bool:
    """
    Detecta si el texto parece un volcado técnico o contenido interno,
    pero permite Markdown normal de la base de conocimientos.
    Se ha ajustado para no escalar cuando el texto contiene términos reales del hotel.
    """
    if not text:
        return False

    dump_patterns = [
        "traceback", "error", "exception",
        "{", "}", "SELECT ", "INSERT ", "sql", "schema"
    ]
    if any(pat.lower() in text.lower() for pat in dump_patterns):
        return True

    keywords_ok = [
        "gimnasio", "desayuno", "recepción", "parking",
        "mascotas", "wifi", "check-in", "restaurante",
        "habitaciones", "coworking", "lavandería"
    ]
    if len(text.split()) > 1200 and not any(k in text.lower() for k in keywords_ok):
        return True

    return False


# =====================================================
# 🧩 Tool principal (consulta HTTP factual)
# =====================================================
async def hotel_information_tool(query: str) -> str:
    """
    Devuelve respuesta directamente desde la base de conocimientos (API HTTP del MCP Server),
    sin generación adicional ni resumen.
    """
    try:
        q = (query or "").strip()
        if not q:
            return ESCALATE_SENTENCE

        # 👇 Nueva llamada directa al endpoint HTTP del servidor MCP
        result = await call_knowledge_base(q)

        if not result or "error" in result:
            log.error(f"❌ Error o respuesta nula desde knowledge_base: {result}")
            return ESCALATE_SENTENCE

        if not result.get("data"):
            log.warning("⚠️ La base de conocimientos no devolvió resultados.")
            return ESCALATE_SENTENCE

        # ✅ Tomamos el contenido textual de los documentos
        docs = result.get("data", [])
        cleaned_text = "\n".join(d.get("content", "") for d in docs if isinstance(d, dict))

        if not cleaned_text.strip():
            log.warning("⚠️ Respuesta vacía o sin texto válido.")
            return ESCALATE_SENTENCE

        cleaned = normalize_reply(cleaned_text, q, "InfoAgent").strip()

        if not cleaned or len(cleaned) < 10:
            log.warning("⚠️ Respuesta demasiado corta en KB.")
            return ESCALATE_SENTENCE
        if _looks_like_internal_dump(cleaned):
            log.warning("⚠️ Dump técnico detectado, escalando.")
            return ESCALATE_SENTENCE
        if "no hay resultados" in cleaned.lower() or "no encontrado" in cleaned.lower():
            return ESCALATE_SENTENCE

        cleaned = re.sub(r"[*#>\-]+", "", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()

        log.info(f"✅ [InfoAgent] Respuesta factual KB: {cleaned[:200]}")
        return cleaned

    except Exception as e:
        log.error(f"❌ Error en hotel_information_tool: {e}", exc_info=True)
        return ESCALATE_SENTENCE


# =====================================================
# 🏨 Clase InfoAgent (con memoria integrada, sin AgentExecutor)
# =====================================================
class InfoAgent:
    """
    Subagente que responde preguntas generales sobre el hotel.
    Escala automáticamente al encargado si no hay información útil.
    Ahora integra memoria persistente por chat_id.
    """

    def __init__(self, model_name: str = "gpt-4.1-mini", memory_manager=None):
        self.model_name = model_name
        self.llm = ChatOpenAI(model=self.model_name, temperature=0.2)
        self.interno_agent = InternoAgent(memory_manager=memory_manager)
        self.memory_manager = memory_manager
        log.info("✅ InfoAgent inicializado (modo factual).")

    # --------------------------------------------------
    def _sync_run(self, coro, *args, **kwargs):
        """Ejecuta funciones async en contexto sync."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        if loop.is_running():
            import nest_asyncio
            nest_asyncio.apply()
        return loop.run_until_complete(coro(*args, **kwargs))

    # --------------------------------------------------
    async def invoke(self, user_input: str, chat_history: list = None, chat_id: str = None) -> str:
        """
        Entrada principal del subagente (modo factual).
        Si no hay información en la KB → escalación automática.
        """
        log.info(f"📩 [InfoAgent] Consulta: {user_input}")
        lang = language_manager.detect_language(user_input)
        chat_history = chat_history or []

        try:
            respuesta_final = await hotel_information_tool(user_input)
            respuesta_final = language_manager.ensure_language(respuesta_final, lang)

            # 💾 Guardar en memoria (consulta y respuesta)
            if self.memory_manager and chat_id:
                self.memory_manager.update_memory(
                    chat_id,
                    role="assistant",
                    content=f"[InfoAgent] Entrada: {user_input}\n\nRespuesta factual: {respuesta_final}"
                )

            # 🚨 Detección de falta de información
            no_info = any(
                p in respuesta_final.lower()
                for p in [
                    "no dispongo", "no tengo información", "consultarlo con el encargado",
                    "permíteme contactar", "no hay resultados", "no encontrado"
                ]
            )

            if _looks_like_internal_dump(respuesta_final) or no_info or respuesta_final == ESCALATE_SENTENCE:
                log.warning("⚠️ Escalación automática: no se encontró información útil.")
                msg = (
                    f"❓ *Consulta del huésped:*\n{user_input}\n\n"
                    "🧠 *Contexto:*\nEl sistema no encontró información relevante en la base de conocimiento."
                )

                # 🧠 Registrar escalación también en memoria
                if self.memory_manager and chat_id:
                    self.memory_manager.update_memory(
                        chat_id,
                        role="system",
                        content="[InfoAgent] Escalación automática al encargado por falta de información factual."
                    )

                await self.interno_agent.escalate(
                    guest_chat_id=chat_id,
                    guest_message=user_input,
                    escalation_type="info_no_encontrada",
                    reason="Falta de información relevante en la base de conocimiento.",
                    context="Escalación automática desde InfoAgent (modo factual)"
                )
                return language_manager.ensure_language(ESCALATE_SENTENCE, lang)

            log.info(f"✅ [InfoAgent] Respuesta final factual: {respuesta_final[:200]}")
            return respuesta_final or ESCALATE_SENTENCE

        except Exception as e:
            log.error(f"💥 Error en InfoAgent.invoke: {e}", exc_info=True)

            if self.memory_manager and chat_id:
                self.memory_manager.update_memory(
                    chat_id,
                    role="system",
                    content=f"[InfoAgent] Error interno en procesamiento: {e}"
                )

            await self.interno_agent.escalate(
                guest_chat_id=chat_id,
                guest_message=user_input,
                escalation_type="error_runtime",
                reason="Error en ejecución del InfoAgent",
                context="Error interno durante la invocación"
            )

            return language_manager.ensure_language(ESCALATE_SENTENCE, lang)
