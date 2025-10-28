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
from tools.supervisor_input_tool import supervisor_input_tool
from tools.supervisor_output_tool import supervisor_output_tool

from core.utils.utils_prompt import load_prompt
from core.memory_manager import MemoryManager
from core.language_manager import language_manager
from core.escalation_manager import mark_pending
from agents.interno_agent import process_tool_call as interno_notify
from core.observability import ls_context

# ===============================================
# CONFIGURACIÓN
# ===============================================
os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
os.environ.setdefault("LANGCHAIN_PROJECT", "BookAI")

log = logging.getLogger("HotelAIHybrid")
logging.getLogger("langchain").setLevel(logging.WARNING)
logging.getLogger("langchain_core").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ===============================================
# MEMORIA
# ===============================================
_global_memory = MemoryManager(max_runtime_messages=8)
LANG_TAG_RE = re.compile(r"^\[lang:([a-z]{2})\]$", re.IGNORECASE)


# ===============================================
# CLASE PRINCIPAL
# ===============================================
class HotelAIHybrid:
    """Agente principal del hotel con supervisión, KB y escalado controlado."""

    def __init__(
        self,
        memory_manager: Optional[MemoryManager] = None,
        max_iterations: int = 10,
        return_intermediate_steps: bool = True,
    ):
        self.memory = memory_manager or _global_memory

        # 🧠 Hardcodeamos modelo por estabilidad (evita "must provide model parameter")
        self.model_name = "gpt-4.1-mini"
        self.temperature = 0.2

        log.info(f"🧠 Inicializando HotelAIHybrid con modelo fijo: {self.model_name}")

        self.llm = ChatOpenAI(
            model=self.model_name,
            temperature=self.temperature,
            streaming=False,
            max_tokens=1500,
        )

        # Herramientas
        self.tools = get_all_hotel_tools()
        log.info(f"🧩 {len(self.tools)} herramientas cargadas correctamente.")

        # Prompt base
        self.system_message = self._load_main_prompt()
        self.agent_executor = self._create_agent_executor(
            max_iterations=max_iterations,
            return_intermediate_steps=return_intermediate_steps,
        )

        log.info("✅ HotelAIHybrid listo (supervisado y estable).")

    # -----------------------------------------------
    def _load_main_prompt(self) -> str:
        text = load_prompt("main_prompt.txt")
        if not text or not text.strip():
            raise RuntimeError("El archivo main_prompt.txt no se pudo cargar.")
        return text

    def _create_agent_executor(self, max_iterations: int, return_intermediate_steps: bool):
        prompt = ChatPromptTemplate.from_messages([
            ("system", self.system_message),
            MessagesPlaceholder("chat_history"),
            ("user", "{input}"),
            MessagesPlaceholder("agent_scratchpad"),
        ])
        agent = create_openai_tools_agent(self.llm, self.tools, prompt)
        return AgentExecutor(
            agent=agent,
            tools=self.tools,
            verbose=True,
            handle_parsing_errors=True,
            max_iterations=max_iterations,
            return_intermediate_steps=return_intermediate_steps,
        )

    # -----------------------------------------------
    # Idioma persistente
    def _extract_lang_from_history(self, history: List[dict]) -> Optional[str]:
        for msg in reversed(history):
            if not isinstance(msg.get("content"), str):
                continue
            m = LANG_TAG_RE.match(msg["content"].strip())
            if m:
                return m.group(1).lower()
        return None

    def _persist_lang_tag(self, cid: str, lang: str):
        try:
            self.memory.save(cid, "system", f"[lang:{(lang or 'es').lower()}]")
        except Exception as e:
            log.warning(f"⚠️ No se pudo guardar tag idioma: {e}")

    def _get_or_detect_language(self, msg: str, cid: str, history: List[dict]) -> str:
        saved = self._extract_lang_from_history(history)
        detected = language_manager.detect_language(msg)
        if not detected and saved:
            return saved or "unknown"
        if saved and detected == saved:
            return saved
        if detected and detected != saved:
            self._persist_lang_tag(cid, detected)
            return detected
        return detected or "unknown"

    # -----------------------------------------------
    def _postprocess(self, text: str) -> str:
        """Limpia texto generado (sin muletillas ni cierres redundantes)."""
        if not text:
            return ""
        text = text.strip().replace("..", ".")
        tails = ["estoy aquí para ayudarte", "i'm here to help"]
        for t in tails:
            if t in text.lower():
                text = text[:text.lower().find(t)].strip()
        return text

    # -----------------------------------------------
    async def process_message(self, user_message: str, conversation_id: str = None) -> str | None:
        if not conversation_id:
            conversation_id = "unknown"

        cid = str(conversation_id).replace("+", "").strip()
        log.info(f"📩 Mensaje recibido de {cid}: {user_message}")

        with ls_context(
            name="HotelAIHybrid.process_message",
            metadata={"conversation_id": cid, "input": user_message},
            tags=["main_agent", "hotel_ai"],
        ):
            # ================= SUPERVISOR INPUT =================
            try:
                si_result = supervisor_input_tool.invoke({"mensaje_usuario": user_message})
                log.info(f"📑 [Supervisor INPUT] salida:\n{si_result}")
                if isinstance(si_result, str) and si_result != "Aprobado":
                    log.warning("🚫 [Supervisor INPUT] No aprobado. Escalando.")
                    await interno_notify(si_result)
                    await mark_pending(cid, user_message)
                    return ""
            except Exception as e:
                log.error(f"❌ Error en supervisor_input_tool: {e}", exc_info=True)
                await interno_notify(
                    f"Estado: Revisión Necesaria\nMotivo: Error interno supervisor_input_tool: {e}\nPrueba: {user_message}"
                )
                return ""

            # ================= AGENTE PRINCIPAL =================
            hist = self.memory.get_context(cid, limit=12)
            chat_hist = [
                HumanMessage(content=m["content"]) if m["role"] == "user" else AIMessage(content=m["content"])
                for m in hist if m.get("role") in ("user", "assistant")
            ]
            lang = self._get_or_detect_language(user_message, cid, hist)

            try:
                result = await self.agent_executor.ainvoke({
                    "input": user_message.strip(),
                    "chat_history": chat_hist,
                })
                output = next((result.get(k) for k in ["output", "final_output", "response"] if result.get(k)), "")
                if not output:
                    output = "No dispongo de ese dato en este momento."
                log.info(f"🤖 [Agente Principal] Generó: {output[:200]}")
            except Exception as e:
                log.error(f"❌ Error ejecutando agente: {e}", exc_info=True)
                output = "Ha ocurrido un imprevisto. Voy a consultarlo con el encargado."

            # ================= SUPERVISOR OUTPUT =================
            try:
                so_result = supervisor_output_tool.invoke({
                    "input_usuario": user_message,
                    "respuesta_agente": output,
                })
                log.info(f"📊 [Supervisor OUTPUT] salida:\n{so_result}")

                if isinstance(so_result, str):
                    estado = so_result.lower()
                    if "estado: rechazado" in estado:
                        log.warning("🚫 [Supervisor OUTPUT] Rechazado. Escalando al encargado.")
                        await interno_notify(so_result)
                        await mark_pending(cid, user_message)
                        return ""
                    elif "estado: revisión necesaria" in estado:
                        log.warning("⚠️ [Supervisor OUTPUT] Revisión necesaria (no crítica).")
                        await interno_notify(so_result)
            except Exception as e:
                log.error(f"❌ Error en supervisor_output_tool: {e}", exc_info=True)
                await interno_notify(
                    f"Estado: Revisión Necesaria\nMotivo: Error interno supervisor_output_tool: {e}\nPrueba: {output}"
                )
                return ""

            # ================= POSTPROCESO FINAL =================
            def _clean_output_text(text: str) -> str:
                if not text:
                    return text
                text = re.sub(r"(\b[A-ZÁÉÍÓÚÑ].{20,}?)\1+", r"\1", text)
                text = re.sub(r"\s{2,}", " ", text)
                return text.strip()

            final_resp = language_manager.ensure_language(self._postprocess(output), lang)
            final_resp = _clean_output_text(final_resp)

            try:
                self.memory.save(cid, "user", user_message)
                if not self._extract_lang_from_history(hist):
                    self._persist_lang_tag(cid, lang)
                self.memory.save(cid, "assistant", final_resp)
            except Exception as e:
                log.warning(f"⚠️ No se pudo guardar en memoria: {e}")

            log.info(f"💾 Memoria actualizada para {cid}")
            return final_resp
