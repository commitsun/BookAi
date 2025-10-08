import os
import json
import logging
from langchain_openai import ChatOpenAI
from langchain.agents import create_openai_functions_agent, AgentExecutor
from langchain.prompts import ChatPromptTemplate

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
    Sistema híbrido de IA para HotelAI.
    Usa LangChain Functions + prompts externos + herramientas específicas.
    """

    def __init__(self, memory_manager: MemoryManager | None = None):
        # 🧠 Usa memoria global o personalizada (para tests u otros canales)
        self.memory = memory_manager or _global_memory

        # ⚙️ Configuración del modelo desde entorno (.env)
        self.model_name = os.getenv("OPENAI_MODEL", "gpt-5-mini")
        self.temperature = float(os.getenv("OPENAI_TEMPERATURE", "0.2"))
        logging.info(f"🧠 Inicializando HotelAIHybrid con modelo: {self.model_name}")

        # 🧩 Inicializar modelo LLM con fallback
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
            return_intermediate_steps=True,  # 👈 Necesario para extraer tool outputs
        )

        logging.info("✅ HotelAIHybrid inicializado correctamente con memoria híbrida.")

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
        # ✅ Normalización de ID
        if not conversation_id:
            logging.warning("⚠️ conversation_id no recibido — usando ID temporal.")
            conversation_id = "unknown"

        clean_id = str(conversation_id).replace("+", "").strip()
        language = detect_language(user_message)
        logging.info(f"📩 Mensaje recibido de {clean_id} ({language}): {user_message}")

        try:
            # 🧠 Recuperar historial reciente (RAM + Supabase)
            history = self.memory.get_context(clean_id, limit=10)
            history_context = "\n".join(
                [f"{m['role']}: {m['content']}" for m in history]
            )

            # Combinar historial + mensaje actual
            full_input = (
                f"Historial reciente:\n{history_context}\n\n"
                f"Nuevo mensaje del usuario:\n{user_message}"
            )

            # 🤖 Ejecutar agente con contexto
            result = await self.executor.ainvoke({
                "input": full_input,
                "language": language,
            })

            # =============================================
            # 🧩 Captura inteligente del output real
            # =============================================
            try:
                output = (
                    result.get("output")
                    or result.get("final_output")
                    or result.get("response")
                    or ""
                )

                # Intentar recuperar desde intermediate_steps si no hay output directo
                if (not output or output.strip() == "") and "intermediate_steps" in result:
                    steps = result.get("intermediate_steps", [])
                    if isinstance(steps, list) and len(steps) > 0:
                        # Algunos agentes devuelven (AgentAction, str)
                        last_step = steps[-1]
                        if isinstance(last_step, (list, tuple)) and len(last_step) > 1:
                            last_output = last_step[1]
                            if isinstance(last_output, str):
                                output = last_output
                            elif isinstance(last_output, dict):
                                # Por si la tool devuelve JSON
                                output = json.dumps(last_output, ensure_ascii=False)

                # Si aún no hay salida válida, revisa si hay atributo result["output_text"]
                if (not output or output.strip() == "") and hasattr(result, "output_text"):
                    output = result.output_text

                # Fallback final solo si sigue vacío
                if not output or len(str(output).strip()) == 0:
                    output = (
                        "Ha ocurrido un error procesando tu solicitud. "
                        "Estoy contactando con el encargado."
                    )

                output = str(output).strip()
                logging.info(f"🤖 Respuesta generada correctamente: {output[:180]}...")

            except Exception as e:
                logging.error(f"⚠️ Error extrayendo salida del agente: {e}", exc_info=True)
                output = (
                    "Ha ocurrido un error procesando tu solicitud. "
                    "Estoy contactando con el encargado."
                )

        except Exception as e:
            logging.error(f"❌ Error en agente: {e}", exc_info=True)
            output = (
                "Ha ocurrido un error procesando tu solicitud. "
                "Estoy contactando con el encargado."
            )

        # 🗣️ Ajustar idioma de salida
        final_response = enforce_language(user_message, output, language)

        # 💾 Guardar mensaje en memoria híbrida
        self.memory.save(clean_id, "user", user_message)
        self.memory.save(clean_id, "assistant", final_response)

        logging.info(
            f"💾 Memoria actualizada para {clean_id} "
            f"({len(self.memory.runtime_memory.get(clean_id, []))} mensajes en RAM)"
        )

        return final_response
