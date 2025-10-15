# =====================================================
# 🧠 HotelAIHybrid — Agente principal estilo n8n (usa main_prompt)
# =====================================================
import os
import json
import logging
from typing import Optional, List

from langchain_openai import ChatOpenAI
from langchain.agents import create_openai_tools_agent, AgentExecutor
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage, AIMessage

from tools.hotel_tools import get_all_hotel_tools
from core.utils.utils_prompt import load_prompt
from core.memory_manager import MemoryManager  # 🧠 Memoria híbrida RAM + DB

# ===============================================
# 🔍 LangSmith Observability (BookAI Project)
# ===============================================
os.environ["LANGCHAIN_TRACING_V2"] = "true"
os.environ["LANGCHAIN_ENDPOINT"] = "https://api.smith.langchain.com"
os.environ["LANGCHAIN_PROJECT"] = "BookAI"
# LANGCHAIN_API_KEY debe estar en .env


# =====================================================
# 🧠 Instancia global de memoria (RAM + Supabase)
# =====================================================
_global_memory = MemoryManager(max_runtime_messages=8)


# =====================================================
# 🏨 Agente híbrido principal del sistema HotelAI
# =====================================================
class HotelAIHybrid:
    """
    Sistema IA principal del hotel, con arquitectura tipo n8n.
    - Usa main_prompt.txt como System Message obligatorio.
    - Tools dinámicas (LangChain Tools Agent)
    - Memoria híbrida (RAM + DB)
    - Multi-idioma nativo (una sola llamada al LLM)
    - Tono humano y conciso; no inventa información externa.
    """

    def __init__(
        self,
        memory_manager: Optional[MemoryManager] = None,
        max_iterations: int = 10,
        return_intermediate_steps: bool = True,
    ):
        self.memory = memory_manager or _global_memory

        # Modelo principal (por .env). Fallback por seguridad.
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

        logging.info("✅ HotelAIHybrid listo con arquitectura n8n usando main_prompt.txt.")

    # -------------------------------------------------
    # 🧾 Carga de prompt principal desde /prompts
    # -------------------------------------------------
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

    # -------------------------------------------------
    # 🧩 Construcción del agente con tools dinámicas
    # -------------------------------------------------
    def _create_agent_executor(self, max_iterations: int, return_intermediate_steps: bool):
        prompt = ChatPromptTemplate.from_messages([
            ("system", self.system_message),
            MessagesPlaceholder(variable_name="chat_history"),
            # 👇 Inyectamos instrucciones internas delante del input (una sola llamada al LLM)
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

    # -------------------------------------------------
    # 🧠 Instrucciones internas previas al input (tono + política)
    # -------------------------------------------------
    def _inject_smart_instructions(self, user_message: str) -> str:
        return (
            "[INSTRUCCIONES INTERNAS — NO MOSTRAR]\n"
            "- Responde SIEMPRE en el mismo idioma que el cliente (detéctalo a partir del mensaje).\n"
            "- Si hay varias preguntas, respóndelas en un único mensaje, claro, breve y ordenado.\n"
            "- Usa solo información del hotel o de la conversación. No inventes datos externos.\n"
            "- Si no consta en la base o no lo sabes, di naturalmente: "
            "\"No dispongo de ese dato ahora mismo. Si quieres, lo consulto y te confirmo.\"\n"
            "- Para solicitudes absurdas, responde con cortesía y sentido común.\n"
            "- Evita muletillas y cierres largos. Un emoji como máximo si aporta claridad.\n"
            "- SALUDOS/AGRADECIMIENTOS/CHARLA TRIVIAL: usa la herramienta 'other' y pásale una "
            "respuesta corta, profesional y en el idioma del cliente. La tool devolverá ese mismo texto.\n"
            "- CONSULTAS EXTERNAS (p.ej., restaurantes cercanos, farmacias, playas, taxis): "
            "no inventes. Si se requiere dato externo o la KB no lo tiene, escala.\n"
            "[FIN]\n\n"
            f"Mensaje del cliente:\n{user_message}"
        )

    # -------------------------------------------------
    # ✨ Post-procesado suave (determinista, sin LLM)
    # -------------------------------------------------
    def _postprocess_response(self, raw_reply: str) -> str:
        if not raw_reply:
            return raw_reply

        reply = raw_reply.strip()
        lower = reply.lower()

        # Eliminar coletillas repetidas comunes
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

        # Suavizado simple de negaciones duras
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

    # -------------------------------------------------
    # 💬 Procesamiento principal de mensajes (una sola llamada al LLM)
    # -------------------------------------------------
    async def process_message(self, user_message: str, conversation_id: str = None) -> str:
        if not conversation_id:
            logging.warning("⚠️ conversation_id no recibido — usando ID temporal.")
            conversation_id = "unknown"

        clean_id = str(conversation_id).replace("+", "").strip()
        logging.info(f"📩 Mensaje recibido de {clean_id}: {user_message}")

        # Contexto previo desde memoria híbrida
        history = self.memory.get_context(clean_id, limit=10)
        chat_history = [
            HumanMessage(content=m["content"]) if m["role"] == "user"
            else AIMessage(content=m["content"])
            for m in history
        ]

        smart_input = self._inject_smart_instructions(user_message)

        try:
            # Una sola llamada (router + tools + redacción final)
            result = await self.agent_executor.ainvoke({
                "input": smart_input,
                "chat_history": chat_history,
            })

            # Extracción robusta del output final
            output = None
            for key in ["output", "final_output", "response"]:
                val = result.get(key)
                if isinstance(val, str) and val.strip():
                    output = val.strip()
                    break

            # Fallback: intentar extraer del último intermediate_step
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

            # Fallback adicional: volcado recortado
            if not output or not output.strip():
                raw_dump = json.dumps(result, ensure_ascii=False)
                output = raw_dump[:1500] if len(raw_dump) > 20 else None

            # Último recurso
            if not output or not output.strip():
                output = (
                    "Ha ocurrido un imprevisto al procesar tu solicitud. "
                    "Voy a consultarlo y te confirmo en breve."
                )

            logging.info(f"🤖 Respuesta generada (antes de post-proceso): {output[:160]}...")

        except Exception as e:
            logging.error(f"❌ Error en agente: {e}", exc_info=True)
            output = (
                "Ha ocurrido un imprevisto al procesar tu solicitud. "
                "Voy a consultarlo y te confirmo en breve."
            )

        # Post-proceso suave (determinista)
        final_response = self._postprocess_response(output)

        # Persistencia en memoria
        try:
            self.memory.save(clean_id, "user", user_message)
            self.memory.save(clean_id, "assistant", final_response)
        except Exception as e:
            logging.warning(f"⚠️ No se pudo persistir en memoria: {e}")

        logging.info(
            f"💾 Memoria actualizada para {clean_id} "
            f"({len(self.memory.runtime_memory.get(clean_id, []))} mensajes en RAM)"
        )
        return final_response
