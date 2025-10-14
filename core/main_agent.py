# =====================================================
# 🧠 HotelAIHybrid — Agente principal estilo n8n (usa main_prompt)
# =====================================================
import os
import json
import logging
from typing import Optional

from langchain_openai import ChatOpenAI
from langchain.agents import create_openai_tools_agent, AgentExecutor
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage, AIMessage

from tools.hotel_tools import get_all_hotel_tools
from core.language import detect_language, enforce_language
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
    - Multi-idioma y manejo automático de errores
    - Tono suave y humano; no inventa información externa.
    """

    def __init__(
        self,
        memory_manager: Optional[MemoryManager] = None,
        max_iterations: int = 10,
        return_intermediate_steps: bool = True,
    ):
        self.memory = memory_manager or _global_memory

        self.model_name = os.getenv("OPENAI_MODEL")
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
    def _inject_smart_instructions(self, user_message: str, language: str) -> str:
        is_es = (language or "").lower().startswith("es")

        if is_es:
            instructions = (
                "[INSTRUCCIONES INTERNAS — NO MOSTRAR]\n"
                "- Responde en español de España, con tono cercano y profesional.\n"
                "- Si hay varias preguntas, respóndelas una a una, de forma breve y clara en un único mensaje.\n"
                "- Usa solo información del hotel o de la conversación. No inventes datos externos.\n"
                "- Si no consta en la base o no lo sabes, di naturalmente: "
                "\"No dispongo de ese dato ahora mismo. Si quieres, lo consulto y te confirmo.\"\n"
                "- Para solicitudes absurdas (p. ej., dragones), responde con cortesía y sentido común.\n"
                "- Evita muletillas y cierres largos. Mantén la respuesta concisa.\n"
                "- Emojis: solo cuando aporten claridad (máx. 1).\n"
                "[FIN]\n"
            )
            prefix = "Mensaje del cliente:\n"
        else:
            instructions = (
                "[INTERNAL INSTRUCTIONS — DO NOT SHOW]\n"
                "- Answer in the user's language, warm and professional.\n"
                "- If multiple questions, answer them briefly in a single message.\n"
                "- Use hotel info or conversation context only. Do not invent external facts.\n"
                "- If unknown, say naturally: "
                "\"I don’t have that detail right now. I can check and confirm if you’d like.\"\n"
                "- Handle absurd requests politely.\n"
                "- Keep it concise; avoid long closings. One emoji max if helpful.\n"
                "[END]\n"
            )
            prefix = "Customer message:\n"

        return f"{instructions}\n{prefix}{user_message}"

    # -------------------------------------------------
    # ✨ Post-procesado suave (determinista)
    # -------------------------------------------------
    def _postprocess_response(self, user_message: str, raw_reply: str, language: str) -> str:
        if not raw_reply:
            return raw_reply

        reply = raw_reply.strip()
        lower = reply.lower()
        is_es = (language or "").lower().startswith("es")

        # Eliminar coletillas repetidas
        tails = [
            "si necesitas más información, estaré encantado de ayudarte",
            "si necesita más información, estaré encantado de ayudarle",
            "si necesitas algo más, estaré encantado de ayudarte",
            "estoy aquí para ayudarte",
        ]
        for t in tails:
            if t in lower:
                reply = reply[:lower.find(t)].rstrip(". ").strip()

        # Suavizar negativas muy secas
        if is_es:
            harsh_map = {
                "no dispongo de ese dato en este momento": "No dispongo de ese dato ahora mismo. Si quieres, lo consulto y te confirmo.",
                "no dispongo de ese dato por el momento": "No dispongo de ese dato ahora mismo. Si quieres, lo consulto y te confirmo.",
                "actualmente no hay disponibilidad": "Ahora mismo no contamos con eso. Si te sirve, puedo proponerte alternativas.",
                "no hay": "Ahora mismo no contamos con ello.",
            }
        else:
            harsh_map = {
                "i don’t have that information at this moment": "I don’t have that detail right now. I can check and confirm if you’d like.",
                "not available at the moment": "It’s not available right now. I can suggest alternatives if helpful.",
                "no": "Not at the moment.",
            }

        l = reply.lower()
        for k, v in harsh_map.items():
            if k in l:
                i = l.find(k)
                reply = reply[:i] + v + reply[i + len(k):]
                break

        return reply.replace("..", ".").strip()

    # -------------------------------------------------
    # 💬 Procesamiento principal de mensajes
    # -------------------------------------------------
    async def process_message(self, user_message: str, conversation_id: str = None) -> str:
        if not conversation_id:
            logging.warning("⚠️ conversation_id no recibido — usando ID temporal.")
            conversation_id = "unknown"

        clean_id = str(conversation_id).replace("+", "").strip()
        language = detect_language(user_message)
        logging.info(f"📩 Mensaje recibido de {clean_id} ({language}): {user_message}")

        # Contexto previo desde memoria híbrida
        history = self.memory.get_context(clean_id, limit=10)
        chat_history = [
            HumanMessage(content=m["content"]) if m["role"] == "user"
            else AIMessage(content=m["content"])
            for m in history
        ]

        # Instrucciones internas (sin segunda llamada LLM)
        smart_input = self._inject_smart_instructions(user_message, language)

        try:
            # Una sola llamada al agente (router + tools)
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
                raw_dump = json.dumps(result, ensure_ascii=False)
                output = raw_dump[:1500] if len(raw_dump) > 20 else None

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
        softened = self._postprocess_response(user_message, output, language)

        # Ajuste de idioma final
        final_response = enforce_language(user_message, softened, language)

        # Persistencia en memoria
        self.memory.save(clean_id, "user", user_message)
        self.memory.save(clean_id, "assistant", final_response)

        logging.info(
            f"💾 Memoria actualizada para {clean_id} "
            f"({len(self.memory.runtime_memory.get(clean_id, []))} mensajes en RAM)"
        )
        return final_response
