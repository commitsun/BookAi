# core/main_agent.py
import os
import json
import logging
import re
from typing import Optional, List

from langchain_openai import ChatOpenAI
from langchain.agents import create_openai_tools_agent, AgentExecutor
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage, AIMessage

from tools.hotel_tools import get_all_hotel_tools
from core.utils.utils_prompt import load_prompt
from core.memory_manager import MemoryManager
from core.language_manager import language_manager
from core.escalation_manager import mark_pending

# ===============================================
# (Opcional) LangSmith
# ===============================================
os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
os.environ.setdefault("LANGCHAIN_ENDPOINT", "https://api.smith.langchain.com")
os.environ.setdefault("LANGCHAIN_PROJECT", "BookAI")

# ===============================================
# Memoria híbrida
# ===============================================
_global_memory = MemoryManager(max_runtime_messages=8)

LANG_TAG_RE = re.compile(r"^\[lang:([a-z]{2})\]$", re.IGNORECASE)
ESCALATE_MARKERS = (
    "__ESCALATE__",  # marcador explícito desde tools
    "contactar con el encargado",
    "consultarlo con el encargado",
    "voy a consultarlo con el encargado",
    "un momento por favor",
    "permíteme contactar",
    "he contactado con el encargado",
    "no dispongo",
    "error",
)


class HotelAIHybrid:
    """
    Agente principal del hotel:
    - Router + Tools (LangChain)
    - Idioma dinámico (detecta, persiste como [lang:xx] en role='system', fuerza salida)
    - Escalación automática (mark_pending) si se detecta intención de escalado
    """

    def __init__(
        self,
        memory_manager: Optional[MemoryManager] = None,
        max_iterations: int = 10,
        return_intermediate_steps: bool = True,
    ):
        self.memory = memory_manager or _global_memory

        self.model_name = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
        self.temperature = float(os.getenv("OPENAI_TEMPERATURE", "0.2"))
        logging.info(f"🧠 Inicializando HotelAIHybrid con modelo: {self.model_name}")

        self.llm = ChatOpenAI(
            model=self.model_name,
            temperature=self.temperature,
            streaming=False,
            max_tokens=1500,
        )

        self.tools = get_all_hotel_tools()
        logging.info(f"🧩 {len(self.tools)} herramientas cargadas correctamente.")

        self.system_message = self._load_main_prompt()
        self.agent_executor = self._create_agent_executor(
            max_iterations=max_iterations,
            return_intermediate_steps=return_intermediate_steps,
        )

        logging.info("✅ HotelAIHybrid listo (idioma persistente + escalación automática).")

    # ---------------------- Prompt principal ----------------------
    def _load_main_prompt(self) -> str:
        try:
            prompt_text = load_prompt("main_prompt.txt")
            if not prompt_text or len(prompt_text.strip()) == 0:
                raise FileNotFoundError("El archivo main_prompt.txt está vacío o no se pudo leer.")
            logging.info("📜 main_prompt.txt cargado correctamente.")
            return prompt_text
        except Exception as e:
            logging.error(f"❌ Error al cargar main_prompt.txt: {e}")
            raise RuntimeError(
                "El agente no puede iniciarse sin main_prompt.txt. "
                "Verifica /prompts/main_prompt.txt."
            )

    # ---------------------- Agent executor ------------------------
    def _create_agent_executor(self, max_iterations: int, return_intermediate_steps: bool):
        prompt = ChatPromptTemplate.from_messages([
            ("system", self.system_message),
            MessagesPlaceholder(variable_name="chat_history"),
            ("user", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])

        agent = create_openai_tools_agent(
            llm=self.llm,
            tools=self.tools,
            prompt=prompt,
        )

        executor = AgentExecutor(
            agent=agent,
            tools=self.tools,
            verbose=True,
            handle_parsing_errors=True,
            max_iterations=max_iterations,
            return_intermediate_steps=return_intermediate_steps,
        )
        return executor

    # ---------------------- Idioma persistente --------------------
    def _extract_lang_from_history(self, history: List[dict]) -> Optional[str]:
        # Busca el tag [lang:xx] en el historial (cualquier role), del más reciente al más antiguo
        for msg in reversed(history):
            content = (msg or {}).get("content", "")
            if not isinstance(content, str):
                continue
            m = LANG_TAG_RE.match(content.strip())
            if m:
                return m.group(1).lower()
        return None

    def _persist_lang_tag(self, conversation_id: str, lang: str):
        # Importante: role='system' (válido para la constraint de Supabase)
        try:
            tag = f"[lang:{(lang or 'es').lower()}]"
            self.memory.save(conversation_id, "system", tag)
        except Exception as e:
            logging.warning(f"⚠️ No se pudo persistir tag de idioma: {e}")

    def _get_or_detect_language(self, user_message: str, conversation_id: str, combined_history: List[dict]) -> str:
        saved = self._extract_lang_from_history(combined_history)
        if saved:
            return saved
        detected = language_manager.detect_language(user_message)
        self._persist_lang_tag(conversation_id, detected)
        return detected

    # ---------------------- Instrucciones internas ----------------
    def _inject_smart_instructions(self, user_message: str, lang_code: str) -> str:
        return (
            "[INSTRUCCIONES INTERNAS — NO MOSTRAR]\n"
            f"- RESPONDE SIEMPRE en el idioma ISO 639-1: {lang_code}\n"
            "- Si hay varias preguntas, respóndelas en un único mensaje, claro, breve y ordenado.\n"
            "- Usa solo información del hotel o de la conversación. No inventes datos externos.\n"
            "- Si no consta en la base o no lo sabes, di naturalmente: "
            "\"No dispongo de ese dato ahora mismo. Si quieres, lo consulto y te confirmo.\"\n"
            "- Evita muletillas y cierres largos. Un emoji como máximo si aporta claridad.\n"
            "- SALUDOS/AGRADECIMIENTOS/CHARLA TRIVIAL: usa la herramienta 'other' y pásale una "
            "respuesta corta, profesional y en el idioma del cliente. La tool devolverá ese mismo texto.\n"
            "- CONSULTAS EXTERNAS (restaurantes, farmacias, taxis, etc.): no inventes. Si se requiere dato externo o la KB no lo tiene, escala.\n"
            "[FIN]\n\n"
            f"Mensaje del cliente:\n{user_message}"
        )

    # ---------------------- Post-proceso determinista -------------
    def _postprocess_response(self, raw_reply: str) -> str:
        if not raw_reply:
            return raw_reply
        reply = raw_reply.strip()
        lower = reply.lower()

        tails: List[str] = [
            "si necesitas más información, estaré encantado de ayudarte",
            "si necesita más información, estaré encantado de ayudarle",
            "si necesitas algo más, estaré encantado de ayudarte",
            "estoy aquí para ayudarte",
            "i'm here to help",
            "if you need anything else",
        ]
        for t in tails:
            if t in lower:
                idx = lower.find(t)
                reply = reply[:idx].rstrip(". ").strip()
                lower = reply.lower()

        harsh_map = {
            "no dispongo de ese dato en este momento": "No dispongo de ese dato ahora mismo. Si quieres, lo consulto y te confirmo.",
            "no dispongo de ese dato por el momento": "No dispongo de ese dato ahora mismo. Si quieres, lo consulto y te confirmo.",
            "actualmente no hay disponibilidad": "Ahora mismo no contamos con eso. Si te sirve, puedo proponerte alternativas.",
            "i don’t have that information at this moment": "I don’t have that detail right now. I can check and confirm if you’d like.",
            "not available at the moment": "It’s not available right now. I can suggest alternatives if helpful.",
        }
        l = reply.lower()
        for k, v in harsh_map.items():
            if k in l:
                i = l.find(k)
                reply = reply[:i] + v + reply[i + len(k):]
                break

        return reply.replace("..", ".").strip()

    # ---------------------- Escalación automática -----------------
    def _should_escalate(self, text: str) -> bool:
        if not text:
            return False
        t = text.lower()
        return any(marker in t for marker in ESCALATE_MARKERS)

    # ---------------------- Loop principal ------------------------
    async def process_message(self, user_message: str, conversation_id: str = None) -> str:
        if not conversation_id:
            logging.warning("⚠️ conversation_id no recibido — usando ID temporal.")
            conversation_id = "unknown"

        clean_id = str(conversation_id).replace("+", "").strip()
        logging.info(f"📩 Mensaje recibido de {clean_id}: {user_message}")

        history = self.memory.get_context(clean_id, limit=12)
        chat_history = [
            HumanMessage(content=m["content"]) if m["role"] == "user"
            else AIMessage(content=m["content"])
            for m in history
            if m.get("role") in ("user", "assistant")
        ]

        user_lang = self._get_or_detect_language(user_message, clean_id, history)
        smart_input = self._inject_smart_instructions(user_message, user_lang)

        try:
            result = await self.agent_executor.ainvoke({
                "input": smart_input,
                "chat_history": chat_history,
            })

            output = None
            for key in ["output", "final_output", "response"]:
                val = result.get(key)
                if isinstance(val, str) and val.strip():
                    output = val.strip()
                    break

            if (not output or not output.strip()) and "intermediate_steps" in result:
                steps = result.get("intermediate_steps", [])
                if isinstance(steps, list) and steps:
                    last_step = steps[-1]
                    if isinstance(last_step, (list, tuple)) and len(last_step) > 1:
                        candidate = last_step[1]
                        if isinstance(candidate, str) and candidate.strip():
                            output = candidate.strip()
                        elif isinstance(candidate, dict):
                            output = json.dumps(candidate, ensure_ascii=False)

            if not output or not output.strip():
                output = (
                    "Ha ocurrido un imprevisto al procesar tu solicitud. "
                    "Voy a consultarlo y te confirmo en breve."
                )

            logging.info(f"🤖 Respuesta generada (antes post-proceso): {output[:160]}...")

        except Exception as e:
            logging.error(f"❌ Error en agente: {e}", exc_info=True)
            output = (
                "Ha ocurrido un imprevisto al procesar tu solicitud. "
                "Voy a consultarlo y te confirmo en breve."
            )

        # Escalación automática (opción A)
        if self._should_escalate(output):
            try:
                await mark_pending(clean_id, user_message)
            except Exception as e:
                logging.error(f"❌ Error en mark_pending: {e}", exc_info=True)

            wait_base = "Un momento por favor, voy a consultarlo con el encargado."
            wait_phrase = "🕓 " + language_manager.short_phrase(wait_base, user_lang)

            # Persistir aviso breve
            try:
                if not self._extract_lang_from_history(history):
                    self._persist_lang_tag(clean_id, user_lang)
                self.memory.save(clean_id, "assistant", wait_phrase)
            except Exception as e:
                logging.warning(f"⚠️ No se pudo persistir aviso de escalación: {e}")

            return wait_phrase

        # Respuesta normal
        final_response = self._postprocess_response(output)
        final_response = language_manager.ensure_language(final_response, user_lang)

        try:
            self.memory.save(clean_id, "user", user_message)
            if not self._extract_lang_from_history(history):
                self._persist_lang_tag(clean_id, user_lang)
            self.memory.save(clean_id, "assistant", final_response)
        except Exception as e:
            logging.warning(f"⚠️ No se pudo persistir en memoria: {e}")

        logging.info(
            f"💾 Memoria actualizada para {clean_id} "
            f"({len(self.memory.runtime_memory.get(clean_id, []))} mensajes en RAM)"
        )
        return final_response
