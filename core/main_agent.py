import os
import logging
from langchain_openai import ChatOpenAI
from langchain.agents import create_openai_functions_agent, AgentExecutor
from langchain.prompts import ChatPromptTemplate
from tools.hotel_tools import get_all_hotel_tools
from core.language import detect_language, enforce_language
from core.db import save_message, get_conversation_history
from core.utils.utils_prompt import load_prompt


# =====================================================
# 🏨 Agente híbrido principal del sistema HotelAI
# =====================================================
class HotelAIHybrid:
    """
    Sistema híbrido de IA para HotelAI.
    Usa LangChain Functions + prompts externos + herramientas específicas.
    """

    def __init__(self):
        # ⚙️ Configuración del modelo desde entorno (.env)
        self.model_name = os.getenv("OPENAI_MODEL", "gpt-5-mini")
        self.temperature = float(os.getenv("OPENAI_TEMPERATURE", "0.2"))
        logging.info(f"🧠 Inicializando HotelAIHybrid con modelo: {self.model_name}")

        # 🧩 Inicializar modelo LLM
        self.llm = self._build_llm()

        # 🧰 Cargar herramientas y crear el agente
        self.tools = get_all_hotel_tools()
        self.agent = self._create_agent()

        # ⚙️ Crear executor LangChain
        self.executor = AgentExecutor.from_agent_and_tools(
            agent=self.agent,
            tools=self.tools,
            verbose=True,
            handle_parsing_errors=True,
            return_intermediate_steps=False,
        )

        logging.info("✅ HotelAIHybrid inicializado correctamente.")

    # -------------------------------------------------
    # 🧠 Inicialización del modelo con fallback
    # -------------------------------------------------
    def _build_llm(self):
        """Intenta usar el modelo especificado; si falla, usa gpt-4o-mini."""
        try:
            llm = ChatOpenAI(
                model=self.model_name,
                temperature=self.temperature,
                max_tokens=1500,
                streaming=False,
            )
            # Prueba mínima de conectividad
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
    # 💬 Procesamiento principal del mensaje
    # -------------------------------------------------
    async def process_message(self, user_message: str, conversation_id: str = None):
        """
        Procesa un mensaje del usuario usando el agente híbrido.
        conversation_id debe ser el número del usuario (sin '+').
        """
        # ✅ Usar el número del usuario como ID de conversación
        if not conversation_id:
            logging.warning("⚠️ conversation_id no recibido — usando ID temporal.")
            conversation_id = "unknown"

        clean_id = str(conversation_id).replace("+", "").strip()
        language = detect_language(user_message)
        logging.info(f"📩 Mensaje recibido de {clean_id} ({language}): {user_message}")

        try:
            # 🔁 Recuperar historial previo para contexto (últimos 5 mensajes)
            history = get_conversation_history(clean_id, limit=5)
            history_context = "\n".join(
                [f"{m['role']}: {m['content']}" for m in history]
            )

            full_input = (
                f"Historial reciente:\n{history_context}\n\n"
                f"Nuevo mensaje del usuario:\n{user_message}"
            )

            # 🤖 Ejecutar agente
            result = await self.executor.ainvoke({
                "input": full_input,
                "language": language,
            })
            output = result.get("output", "").strip()
            logging.info(f"🤖 Respuesta generada: {output[:150]}...")

        except Exception as e:
            logging.error(f"❌ Error en agente: {e}", exc_info=True)
            output = (
                "Ha ocurrido un error procesando tu solicitud. "
                "Estoy contactando con el encargado."
            )

        # 🗣️ Ajustar idioma de salida
        final_response = enforce_language(user_message, output, language)

        # 💾 Guardar conversación en Supabase
        save_message(clean_id, "user", user_message)
        save_message(clean_id, "assistant", final_response)
        logging.info(f"💾 Conversación {clean_id} guardada correctamente.")

        return final_response
