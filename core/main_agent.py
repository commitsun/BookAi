# core/main_agent.py
import os
import uuid
import logging
from langchain_openai import ChatOpenAI
from langchain.agents import create_openai_functions_agent, AgentExecutor
from langchain.prompts import ChatPromptTemplate
from tools.hotel_tools import get_all_hotel_tools
from core.language import detect_language, enforce_language
from core.db import save_message
from core.utils.utils_prompt import load_prompt  


# =====================================================
# 🏨 Agente híbrido principal del sistema HotelAI
# =====================================================
class HotelAIHybrid:
    """
    Sistema híbrido de IA para HotelAI.
    Usa un agente basado en LangChain Functions + prompts externos + tools específicas.
    """

    def __init__(self):
        # ⚙️ Configuración inicial
        self.model_name = os.getenv("OPENAI_MODEL", "gpt-5-mini")
        self.temperature = float(os.getenv("OPENAI_TEMPERATURE", "0.2"))
        logging.info(f"🧠 Inicializando HotelAIHybrid con modelo: {self.model_name}")

        # 🧩 Carga del modelo LLM con fallback automático
        self.llm = self._build_llm()

        # 🧰 Cargar herramientas y crear agente
        self.tools = get_all_hotel_tools()
        self.agent = self._create_agent()

        # ⚙️ Crear executor (LangChain AgentExecutor)
        self.executor = AgentExecutor.from_agent_and_tools(
            agent=self.agent,
            tools=self.tools,
            verbose=True,
            handle_parsing_errors=True,
            return_intermediate_steps=False,
        )

        logging.info("✅ HotelAIHybrid inicializado correctamente.")

    # -------------------------------------------------
    # 🧠 Crea el modelo LLM principal con fallback
    # -------------------------------------------------
    def _build_llm(self):
        """Intenta usar GPT-5, si falla, usa GPT-4o-mini."""
        try:
            llm = ChatOpenAI(
                model=self.model_name,
                temperature=self.temperature,
                max_tokens=1500,
                streaming=False,  # Evita error de verificación
            )
            # Prueba mínima opcional
            _ = llm.invoke("ping")
            logging.info(f"✅ Modelo {self.model_name} cargado correctamente.")
            return llm

        except Exception as e:
            logging.warning(
                f"⚠️ No se pudo cargar {self.model_name}: {e}. "
                "Usando 'gpt-4o-mini' como respaldo."
            )
            return ChatOpenAI(
                model="gpt-4o-mini",
                temperature=self.temperature,
                max_tokens=1500,
                streaming=False,
            )

    # -------------------------------------------------
    # 🧩 Creación del agente con prompt externo
    # -------------------------------------------------
    def _create_agent(self):
        """Crea el agente LangChain con tools + prompt del archivo /prompts."""
        system_prompt = load_prompt("main_prompt.txt")

        prompt = ChatPromptTemplate.from_messages([
            ("system", system_prompt),
            ("user", "{input}"),
            ("assistant", "{agent_scratchpad}")
        ])

        return create_openai_functions_agent(self.llm, self.tools, prompt)

    # -------------------------------------------------
    # 💬 Proceso principal de mensajes
    # -------------------------------------------------
    async def process_message(self, user_message: str, conversation_id: str = None):
        """Procesa un mensaje del usuario usando el agente híbrido."""
        if not conversation_id:
            conversation_id = str(uuid.uuid4())

        language = detect_language(user_message)
        logging.info(f"📩 Mensaje recibido ({language}): {user_message}")

        try:
            result = await self.executor.ainvoke({
                "input": user_message,
                "language": language,
            })
            output = result.get("output", "")
            logging.info(f"🤖 Respuesta generada: {output[:200]}...")

        except Exception as e:
            logging.error(f"❌ Error en agente: {e}", exc_info=True)
            output = (
                "Ha ocurrido un error procesando tu solicitud. "
                "Estoy contactando con el encargado."
            )

        # 🗣️ Ajustar idioma de salida
        final_response = enforce_language(user_message, output, language)

        # 💾 Guardar conversación
        save_message(conversation_id, "user", user_message)
        save_message(conversation_id, "assistant", final_response)
        logging.info(f"💾 Conversación {conversation_id} guardada correctamente.")

        return final_response
