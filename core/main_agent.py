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
    """

    def __init__(
        self,
        memory_manager: Optional[MemoryManager] = None,
        max_iterations: int = 10,
        return_intermediate_steps: bool = True,
    ):
        # 🧠 Usa memoria global o personalizada
        self.memory = memory_manager or _global_memory

        # ⚙️ Configuración de modelo
        self.model_name = os.getenv("OPENAI_MODEL")
        self.temperature = float(os.getenv("OPENAI_TEMPERATURE", "0.2"))
        logging.info(f"🧠 Inicializando HotelAIHybrid con modelo: {self.model_name}")

        # 🤖 Inicializar modelo LLM
        self.llm = ChatOpenAI(
            model=self.model_name,
            temperature=self.temperature,
            streaming=False,
            max_tokens=1500,
        )

        # 🧰 Cargar herramientas dinámicamente
        self.tools = get_all_hotel_tools()
        logging.info(f"🧩 {len(self.tools)} herramientas cargadas correctamente.")

        # 🧾 Cargar prompt del sistema (obligatorio)
        self.system_message = self._load_main_prompt()

        # 🧠 Crear agente (estilo n8n)
        self.agent_executor = self._create_agent_executor(
            max_iterations=max_iterations,
            return_intermediate_steps=return_intermediate_steps,
        )

        logging.info("✅ HotelAIHybrid inicializado con arquitectura n8n-style usando main_prompt.txt.")

    # -------------------------------------------------
    # 🧾 Carga de prompt principal desde /prompts
    # -------------------------------------------------
    def _load_main_prompt(self) -> str:
        """
        Carga el main_prompt.txt desde /prompts.
        Si no existe, lanza un error crítico (el agente no debería iniciar sin él).
        """
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
                "Verifica el archivo en /prompts/main_prompt.txt."
            )

    # -------------------------------------------------
    # 🧩 Construcción del agente con tools dinámicas
    # -------------------------------------------------
    def _create_agent_executor(self, max_iterations: int, return_intermediate_steps: bool):
        """
        Crea el agente LangChain Tools Agent con estructura tipo n8n.
        """
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

    # -------------------------------------------------
    # 💬 Procesamiento principal de mensajes
    # -------------------------------------------------
    async def process_message(self, user_message: str, conversation_id: str = None) -> str:
        """
        Procesa un mensaje del usuario (como n8n Tools Agent):
        - Recupera historial desde memoria híbrida
        - Ejecuta agente con tools dinámicas
        - Devuelve respuesta adaptada al idioma
        """
        if not conversation_id:
            logging.warning("⚠️ conversation_id no recibido — usando ID temporal.")
            conversation_id = "unknown"

        clean_id = str(conversation_id).replace("+", "").strip()
        language = detect_language(user_message)
        logging.info(f"📩 Mensaje recibido de {clean_id} ({language}): {user_message}")

        # 🧠 Recuperar contexto previo
        history = self.memory.get_context(clean_id, limit=10)
        chat_history = [
            HumanMessage(content=m["content"]) if m["role"] == "user"
            else AIMessage(content=m["content"])
            for m in history
        ]

        try:
            # 🤖 Ejecutar el agente estilo n8n
            result = await self.agent_executor.ainvoke({
                "input": user_message,
                "chat_history": chat_history,
            })

            # =====================================================
            # 🧩 Extracción más robusta del output final
            # =====================================================
            output = None

            # 1️⃣ Intenta los campos típicos de LangChain
            for key in ["output", "final_output", "response"]:
                val = result.get(key)
                if isinstance(val, str) and val.strip():
                    output = val.strip()
                    break

            # 2️⃣ Si sigue vacío, busca en intermediate_steps
            if (not output or not output.strip()) and "intermediate_steps" in result:
                steps = result.get("intermediate_steps", [])
                if isinstance(steps, list) and len(steps) > 0:
                    last_step = steps[-1]
                    if isinstance(last_step, (list, tuple)) and len(last_step) > 1:
                        candidate = last_step[1]
                        if isinstance(candidate, str) and candidate.strip():
                            output = candidate.strip()
                        elif isinstance(candidate, dict):
                            output = json.dumps(candidate, ensure_ascii=False)

            # 3️⃣ Si nada aún, intenta rescatar texto del resultado completo
            if not output or not output.strip():
                raw_dump = json.dumps(result, ensure_ascii=False)
                if len(raw_dump) > 20:
                    output = raw_dump[:1500]  # evita respuestas vacías o loops

            # 4️⃣ Último fallback — solo si sigue totalmente vacío
            if not output or not output.strip():
                output = (
                    "Ha ocurrido un error procesando tu solicitud. "
                    "Estoy contactando con el encargado del hotel."
                )

            logging.info(f"🤖 Respuesta generada (post-procesada): {output[:160]}...")

        except Exception as e:
            logging.error(f"❌ Error en agente: {e}", exc_info=True)
            output = (
                "Ha ocurrido un error procesando tu solicitud. "
                "Estoy contactando con el encargado del hotel."
            )

        # 🌐 Ajustar idioma de respuesta
        final_response = enforce_language(user_message, output, language)

        # 💾 Guardar en memoria híbrida (RAM + DB)
        self.memory.save(clean_id, "user", user_message)
        self.memory.save(clean_id, "assistant", final_response)

        logging.info(
            f"💾 Memoria actualizada para {clean_id} "
            f"({len(self.memory.runtime_memory.get(clean_id, []))} mensajes en RAM)"
        )

        return final_response
