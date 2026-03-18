"""
💰 DispoPreciosAgent v4 — Disponibilidad, precios y reservas
-------------------------------------------------------------
Responde preguntas sobre tipos de habitación, precios y disponibilidad.
Usa las tools 'buscar_token' y 'Disponibilidad_y_precios' del MCP.
Integrado con MemoryManager y configuración LLM centralizada.
"""

import logging
import json
import datetime
import asyncio
import re
from langchain_openai import ChatOpenAI
from langchain.agents import create_openai_tools_agent, AgentExecutor
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.tools import Tool

# Core imports
from core.mcp_client import get_tools
from core.utils.normalize_reply import normalize_reply
from core.utils.utils_prompt import load_prompt
from core.utils.time_context import get_time_context
from core.utils.dynamic_context import build_dynamic_context_from_memory
from core.config import ModelConfig, ModelTier  # ✅ nuevo import

log = logging.getLogger("DispoPreciosAgent")
log.setLevel(logging.INFO)


# Subagente encargado de responder preguntas sobre disponibilidad,.
# Se usa en el flujo de subagente de disponibilidad, precios y parsing de fechas como pieza de organización, contrato de datos o punto de extensión.
# Se instancia con configuración, managers, clients o callbacks externos y luego delega el trabajo en sus métodos.
# Los efectos reales ocurren cuando sus métodos se invocan; la definición de clase solo organiza estado y responsabilidades.
class DispoPreciosAgent:
    """
    Subagente encargado de responder preguntas sobre disponibilidad,
    tipos de habitación, precios y reservas.
    Usa MCP Tools y un LLM centralizado (gpt-4.1 por defecto).
    """

    # memory_manager: instancia opcional de MemoryManager.
    # Se usa dentro de `DispoPreciosAgent` en el flujo de subagente de disponibilidad, precios y parsing de fechas.
    # Recibe `memory_manager` como dependencias o servicios compartidos inyectados desde otras capas, y `model_name`, `temperature` como datos de contexto o entrada de la operación.
    # No devuelve valor; deja la instancia preparada con sus dependencias y estado inicial. Puede realizar llamadas externas o a modelos, activar tools o agentes.
    def __init__(self, memory_manager=None, model_name=None, temperature=None):
        """
        memory_manager: instancia opcional de MemoryManager
        model_name / temperature: opcionales. Si no se pasan, se leen de ModelConfig (SUBAGENT).
        """
        self._last_chat_history = []
        self._last_rooms = []
        self._last_dates = None
        self._last_occupancy = None
        self._current_chat_id = None
        # ✅ Modelo centralizado + posibilidad de override
        if model_name is not None or temperature is not None:
            default_name, default_temp = ModelConfig.get_model(ModelTier.SUBAGENT)
            if model_name is None:
                model_name = default_name
            if temperature is None:
                temperature = default_temp
            self.llm = ChatOpenAI(model=model_name, temperature=temperature)
            self.model_name = model_name
            self.temperature = temperature
        else:
            # Usa configuración centralizada tal cual
            self.llm = ModelConfig.get_llm(ModelTier.SUBAGENT)
            # Valores de referencia para logs
            self.model_name, self.temperature = ModelConfig.get_model(ModelTier.SUBAGENT)

        self.memory_manager = memory_manager

        # 🧩 Construcción inicial del prompt con contexto temporal
        base_prompt = load_prompt("dispo_precios_prompt.txt") or self._get_default_prompt()
        self.prompt_text = f"{get_time_context()}\n\n{base_prompt.strip()}"

        # Inicialización de tools y agente
        self.tools = [self._build_tool()]
        self.agent_executor = self._build_agent_executor()

        log.info(
            f"💰 DispoPreciosAgent inicializado correctamente "
            f"(modelo={self.model_name}, temp={self.temperature})"
        )

    # Prompt por defecto si no existe el archivo en disco.
    # Se usa dentro de `DispoPreciosAgent` en el flujo de subagente de disponibilidad, precios y parsing de fechas.
    # No recibe parámetros externos; trabaja con estado capturado por el cierre o atributos de instancia.
    # Devuelve un `str` con el resultado de esta operación. Sin efectos secundarios relevantes.
    def _get_default_prompt(self) -> str:
        """Prompt por defecto si no existe el archivo en disco."""
        return (
            "Eres un agente especializado en disponibilidad y precios de un hotel.\n"
            "Tu función es responder con precisión sobre fechas, precios y tipos de habitación disponibles.\n\n"
            "Usa la información del PMS y responde con tono amable y profesional.\n"
            "Si la información no es suficiente, solicita detalles adicionales al huésped "
            "(fechas, número de personas, tipo de habitación, etc.)."
        )

    # Crea la tool que consulta disponibilidad y precios en el PMS vía MCP.
    # Se usa dentro de `DispoPreciosAgent` en el flujo de subagente de disponibilidad, precios y parsing de fechas.
    # No recibe parámetros externos; trabaja con estado capturado por el cierre o atributos de instancia.
    # Devuelve un `Tool` con el resultado de esta operación. Puede realizar llamadas externas o a modelos, activar tools o agentes.
    def _build_tool(self) -> Tool:
        """Crea la tool que consulta disponibilidad y precios en el PMS vía MCP."""
        # Resuelve la tool de la operación.
        # Se invoca dentro de `_build_tool` para encapsular una parte local de subagente de disponibilidad, precios y parsing de fechas.
        # Recibe `query` como entrada principal según la firma.
        # Devuelve el resultado calculado para que el siguiente paso lo consuma. Puede realizar llamadas externas o a modelos, activar tools o agentes.
        async def _availability_tool(query: str):
            try:
                try:
                    tools = await get_tools(server_name="DispoPreciosAgent")
                except Exception as mcp_err:
                    log.error("❌ MCP no accesible para DispoPreciosAgent: %s", mcp_err, exc_info=True)
                    return (
                        "No puedo consultar disponibilidad ahora mismo porque el servidor MCP de precios "
                        "no es accesible (revisa ENDPOINT_MCP o la red)."
                    )

                token_tool = next((t for t in tools if t.name == "buscar_token"), None)
                dispo_tool = next((t for t in tools if t.name == "Disponibilidad_y_precios"), None)

                if not token_tool or not dispo_tool:
                    log.warning("⚠️ No se encontraron las tools necesarias en MCP.")
                    return "No dispongo de disponibilidad en este momento."

                instance_url = None
                if self.memory_manager and self._current_chat_id:
                    try:
                        instance_url = self.memory_manager.get_flag(self._current_chat_id, "instance_url")
                    except Exception:
                        instance_url = None

                # Obtener token de acceso
                token_payload = {"instance_url": instance_url} if instance_url else {}
                token_raw = await token_tool.ainvoke(token_payload)
                token_data = json.loads(token_raw) if isinstance(token_raw, str) else token_raw
                token = (
                    token_data[0].get("key") if isinstance(token_data, list)
                    else token_data.get("key")
                )

                if not token:
                    log.error("❌ No se pudo obtener el token de acceso.")
                    return "No se pudo obtener el token de acceso."

                today = datetime.date.today()
                parsed_dates = self._parse_dates(query)
                # Complementa con historial si no hay fechas claras en la pregunta
                if not parsed_dates:
                    parsed_dates = self._dates_from_history(self._last_chat_history)

                if parsed_dates:
                    checkin, checkout = parsed_dates
                else:
                    # Fechas por defecto: dentro de 7 días, estancia de 2 noches
                    checkin = today + datetime.timedelta(days=7)
                    checkout = checkin + datetime.timedelta(days=2)

                params = {
                    "checkin": f"{checkin}T00:00:00",
                    "checkout": f"{checkout}T00:00:00",
                    "occupancy": None,
                    "key": token,
                }

                occupancy = self._parse_occupancy(query)
                if not occupancy or occupancy == 1:
                    occupancy = self._occupancy_from_history(self._last_chat_history) or occupancy
                params["occupancy"] = occupancy or 2

                property_id = None
                if self.memory_manager and self._current_chat_id:
                    try:
                        instance_url = instance_url or self.memory_manager.get_flag(
                            self._current_chat_id, "instance_url"
                        )
                        property_id = self.memory_manager.get_flag(self._current_chat_id, "property_id")
                    except Exception:
                        instance_url = instance_url or None
                        property_id = None

                if instance_url:
                    params["instance_url"] = instance_url
                if property_id is not None:
                    try:
                        params["property_id"] = int(property_id)
                    except Exception:
                        params["property_id"] = property_id

                log.info(
                    "📦 Disponibilidad params extra (chat_id=%s): instance_url=%s property_id=%s",
                    self._current_chat_id,
                    instance_url,
                    property_id,
                )

                if "instance_url" not in params or "property_id" not in params:
                    return (
                        "Ahora mismo no tengo el contexto de la instancia para consultar disponibilidad. "
                        "En cuanto lo reciba, continúo."
                    )

                # Reutiliza la última respuesta si coincide fechas/occupancy.
                if self._last_rooms and self._last_dates and self._last_occupancy:
                    if self._is_same_request(query, self._last_rooms, self._last_dates, self._last_occupancy):
                        rooms = self._last_rooms
                        log.info("♻️ Reutilizando última respuesta de disponibilidad (sin nueva llamada).")
                    else:
                        raw_reply = await dispo_tool.ainvoke(params)
                        rooms = json.loads(raw_reply) if isinstance(raw_reply, str) else raw_reply
                else:
                    raw_reply = await dispo_tool.ainvoke(params)
                    rooms = json.loads(raw_reply) if isinstance(raw_reply, str) else raw_reply

                # Guarda contexto de la última consulta satisfactoria
                if rooms and isinstance(rooms, list):
                    self._last_rooms = rooms
                    self._last_dates = (checkin, checkout)
                    self._last_occupancy = params["occupancy"]

                if not rooms or not isinstance(rooms, list):
                    return "No hay disponibilidad en las fechas indicadas."

                prompt = (
                    f"{get_time_context()}\n\n"
                    f"Información de habitaciones y precios (los importes vienen YA calculados; no los multipliques ni los recalcules):\n\n"
                    f"{json.dumps(rooms, ensure_ascii=False, indent=2)}\n\n"
                    f"El huésped pregunta: \"{query}\""
                )

                response = await self.llm.ainvoke(prompt)
                return response.content.strip()

            except Exception as e:
                log.error(f"❌ Error en availability_pricing_tool: {e}", exc_info=True)
                return "Ha ocurrido un problema al consultar precios o disponibilidad."

        return Tool(
            name="availability_pricing",
            func=lambda q: self._sync_run(_availability_tool, q),
            description="Consulta disponibilidad, precios y tipos de habitación del hotel.",
            return_direct=True,
        )

    # Crea el AgentExecutor con control de iteraciones y sin pasos intermedios.
    # Se usa dentro de `DispoPreciosAgent` en el flujo de subagente de disponibilidad, precios y parsing de fechas.
    # No recibe parámetros externos; trabaja con estado capturado por el cierre o atributos de instancia.
    # Devuelve un `AgentExecutor` con el resultado de esta operación. Puede realizar llamadas externas o a modelos, activar tools o agentes.
    def _build_agent_executor(self) -> AgentExecutor:
        """Crea el AgentExecutor con control de iteraciones y sin pasos intermedios."""
        prompt = ChatPromptTemplate.from_messages([
            ("system", self.prompt_text),
            MessagesPlaceholder("chat_history"),
            ("user", "{input}"),
            MessagesPlaceholder("agent_scratchpad"),
        ])

        agent = create_openai_tools_agent(self.llm, self.tools, prompt)

        return AgentExecutor(
            agent=agent,
            tools=self.tools,
            verbose=True,
            return_intermediate_steps=False,
            handle_parsing_errors=True,
            max_iterations=6,
            max_execution_time=60
        )

    # Permite ejecutar async coroutines dentro de contextos sync (LangChain).
    # Se usa dentro de `DispoPreciosAgent` en el flujo de subagente de disponibilidad, precios y parsing de fechas.
    # Recibe `coro`, `*args`, `**kwargs` como entradas relevantes junto con el contexto inyectado en la firma.
    # Devuelve el resultado calculado para que el siguiente paso lo consuma. Sin efectos secundarios relevantes.
    def _sync_run(self, coro, *args, **kwargs):
        """Permite ejecutar async coroutines dentro de contextos sync (LangChain)."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        if loop.is_running():
            import nest_asyncio
            nest_asyncio.apply()

        return loop.run_until_complete(coro(*args, **kwargs))

    # Convierte números escritos (es/en) en dígitos para mejorar el parseo.
    # Se usa dentro de `DispoPreciosAgent` en el flujo de subagente de disponibilidad, precios y parsing de fechas.
    # Recibe `raw_text` como entrada principal según la firma.
    # Devuelve un `str` con el resultado de esta operación. Sin efectos secundarios relevantes.
    def _replace_word_numbers(self, raw_text: str) -> str:
        """Convierte números escritos (es/en) en dígitos para mejorar el parseo."""
        num_words = {
            "un": "1", "uno": "1", "una": "1",
            "dos": "2", "tres": "3", "cuatro": "4", "cinco": "5",
            "seis": "6", "siete": "7", "ocho": "8", "nueve": "9", "diez": "10",
            "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
            "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
        }
        pattern = r"\b(" + "|".join(num_words.keys()) + r")\b"
        return re.sub(
            pattern,
            lambda m: num_words[m.group(1).lower()],
            raw_text,
            flags=re.IGNORECASE,
        )

    # Comprueba si ya respondimos a la misma petición (mismas fechas y occupancy).
    # Se usa dentro de `DispoPreciosAgent` en el flujo de subagente de disponibilidad, precios y parsing de fechas.
    # Recibe `new_query`, `rooms`, `dates`, `occupancy` como entradas relevantes junto con el contexto inyectado en la firma.
    # Devuelve un booleano que gobierna la rama de ejecución siguiente. Sin efectos secundarios relevantes.
    def _is_same_request(self, new_query: str, rooms: list, dates, occupancy: int) -> bool:
        """
        Comprueba si ya respondimos a la misma petición (mismas fechas y occupancy)
        y aún hay datos en historial para reutilizar sin nueva llamada.
        """
        if not dates or not occupancy:
            return False
        if not rooms:
            return False
        # Si la query actual menciona explícitamente las mismas fechas y número de personas
        # no necesitamos volver a llamar si ya tenemos rooms calculado.
        parsed_dates = self._parse_dates(new_query)
        parsed_occupancy = self._parse_occupancy(new_query)
        if parsed_dates and parsed_occupancy and parsed_occupancy == occupancy and parsed_dates == dates:
            return True
        return False

    # Extrae fechas de check-in y check-out a partir de la consulta.
    # Se usa dentro de `DispoPreciosAgent` en el flujo de subagente de disponibilidad, precios y parsing de fechas.
    # Recibe `text` como entrada principal según la firma.
    # Devuelve el resultado calculado para que el siguiente paso lo consuma. Sin efectos secundarios relevantes.
    def _parse_dates(self, text: str):
        """
        Extrae fechas de check-in y check-out a partir de la consulta.
        - Soporta rangos con dos fechas (dd/mm[/yyyy], dd-mm, yyyy-mm-dd, etc.).
        - Reconoce expresiones tipo “este fin de semana” / “próximo fin de semana”
          y las mapea a viernes-domingo.
        - Si solo hay una fecha, asume estancia de 2 noches.
        """
        if not text:
            return None

        raw = text if isinstance(text, str) else str(text)
        raw = self._replace_word_numbers(raw)
        raw_lower = raw.lower()
        today = datetime.date.today()

        # Resuelve el int.
        # Se invoca dentro de `_parse_dates` para encapsular una parte local de subagente de disponibilidad, precios y parsing de fechas.
        # Recibe `val` como entrada principal según la firma.
        # Devuelve el resultado calculado para que el siguiente paso lo consuma. Sin efectos secundarios relevantes.
        def _safe_int(val):
            try:
                return int(val)
            except Exception:
                return None

        # Convierte año de 2 a 4 dígitos y evita fechas pasadas al saltar al siguiente año.
        # Se invoca dentro de `_parse_dates` para encapsular una parte local de subagente de disponibilidad, precios y parsing de fechas.
        # Recibe `y` como entrada principal según la firma.
        # Devuelve el resultado calculado para que el siguiente paso lo consuma. Sin efectos secundarios relevantes.
        def _normalize_year(y):
            """Convierte año de 2 a 4 dígitos y evita fechas pasadas al saltar al siguiente año."""
            if y is None:
                return today.year
            y = _safe_int(y)
            if y is None:
                return today.year
            if y < 100:
                y += 2000
            return y

        # Parsea el token.
        # Se invoca dentro de `_parse_dates` para encapsular una parte local de subagente de disponibilidad, precios y parsing de fechas.
        # Recibe `token` como entrada principal según la firma.
        # Devuelve el resultado calculado para que el siguiente paso lo consuma. Sin efectos secundarios relevantes.
        def _parse_token(token):
            token = token.strip()

            # Formato ISO o yyyy-mm-dd / yyyy/mm/dd
            m = re.match(r"(?P<y>\d{4})[/-](?P<m>\d{1,2})[/-](?P<d>\d{1,2})", token)
            if m:
                y = _safe_int(m.group("y"))
                mth = _safe_int(m.group("m"))
                d = _safe_int(m.group("d"))
                return datetime.date(y, mth, d)

            # Formato dd-mm[-yyyy] o dd/mm[/yyyy]
            m = re.match(r"(?P<d>\d{1,2})[/-](?P<m>\d{1,2})(?:[/-](?P<y>\d{2,4}))?", token)
            if m:
                d = _safe_int(m.group("d"))
                mth = _safe_int(m.group("m"))
                y = _normalize_year(m.group("y"))
                try:
                    candidate = datetime.date(y, mth, d)
                except ValueError:
                    return None

                # Si la fecha ya pasó este año y no había año explícito, salta al siguiente.
                if m.group("y") is None and candidate < today:
                    candidate = datetime.date(y + 1, mth, d)
                return candidate

            return None

        # Resuelve el weekend.
        # Se invoca dentro de `_parse_dates` para encapsular una parte local de subagente de disponibilidad, precios y parsing de fechas.
        # Recibe `weeks_out` como entrada principal según la firma.
        # Devuelve el resultado calculado para que el siguiente paso lo consuma. Sin efectos secundarios relevantes.
        def _next_weekend(weeks_out=0):
            base = today + datetime.timedelta(days=weeks_out * 7)
            days_to_friday = (4 - base.weekday()) % 7
            checkin_dt = base + datetime.timedelta(days=days_to_friday)
            checkout_dt = checkin_dt + datetime.timedelta(days=2)
            return checkin_dt, checkout_dt

        # 1) Expresiones de fin de semana
        if "fin de semana" in raw_lower or "finde" in raw_lower:
            # “próximo” / “que viene” desplazamos una semana
            weeks_out = 1 if any(k in raw_lower for k in ["próximo", "proximo", "que viene", "siguiente"]) else 0
            return _next_weekend(weeks_out)

        # 2) Buscar fechas explícitas
        date_tokens = re.findall(
            r"\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?",
            raw,
            flags=re.IGNORECASE,
        )

        parsed = []
        for token in date_tokens:
            dt = _parse_token(token)
            if dt:
                parsed.append(dt)

        if len(parsed) >= 2:
            parsed = sorted(parsed)
            checkin_dt, checkout_dt = parsed[0], parsed[1]
            # Evita checkout anterior a checkin
            if checkout_dt <= checkin_dt:
                checkout_dt = checkin_dt + datetime.timedelta(days=2)
            return checkin_dt, checkout_dt

        if len(parsed) == 1:
            checkin_dt = parsed[0]
            checkout_dt = checkin_dt + datetime.timedelta(days=2)
            return checkin_dt, checkout_dt

        return None

    # Intenta extraer el número total de huéspedes a partir de la consulta.
    # Se usa dentro de `DispoPreciosAgent` en el flujo de subagente de disponibilidad, precios y parsing de fechas.
    # Recibe `text` como entrada principal según la firma.
    # Devuelve el resultado calculado para que el siguiente paso lo consuma. Sin efectos secundarios relevantes.
    def _parse_occupancy(self, text: str):
        """
        Intenta extraer el número total de huéspedes a partir de la consulta.
        Soporta formatos como "A2C1", "2 adultos y 1 niño", "para 3 personas"
        o JSON con claves occupancy/adultos/niños.
        """
        if not text:
            return None

        # Convierte el int.
        # Se invoca dentro de `_parse_occupancy` para encapsular una parte local de subagente de disponibilidad, precios y parsing de fechas.
        # Recibe `val` como entrada principal según la firma.
        # Devuelve el resultado calculado para que el siguiente paso lo consuma. Sin efectos secundarios relevantes.
        def _to_int(val):
            try:
                return int(str(val).strip())
            except Exception:
                return None

        raw = text if isinstance(text, str) else str(text)
        raw = self._replace_word_numbers(raw)

        # 1) Si viene en JSON, intenta leer occupancy/adultos/niños
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                for key in ["occupancy", "huespedes", "huéspedes", "personas"]:
                    if key in data:
                        val = _to_int(data.get(key))
                        if val and val > 0:
                            return val

                adults = _to_int(data.get("adultos") or data.get("adults"))
                children = _to_int(data.get("ninos") or data.get("niños") or data.get("children"))
                if adults:
                    total = adults + (children or 0)
                    if total > 0:
                        return total
        except Exception:
            pass

        # 2) Código tipo A2C1
        code_match = re.search(r"a\s*(\d+)\s*c\s*(\d+)", raw, re.IGNORECASE)
        if code_match:
            adults, children = map(int, code_match.groups())
            total = adults + children
            if total > 0:
                return total

        # 3) Expresiones "X adultos" y "Y niños"
        adult_matches = re.findall(r"(\d+)\s*(?:adulto?s?|adults?)", raw, re.IGNORECASE)
        child_matches = re.findall(r"(\d+)\s*(?:niñ|child|menor|peque|hijo|infante|beb)", raw, re.IGNORECASE)

        adults = sum(int(x) for x in adult_matches) if adult_matches else 0
        children = sum(int(x) for x in child_matches) if child_matches else 0

        if adults:
            total = adults + children
            if total > 0:
                return total
        elif children:
            return children

        # 4) Total genérico "para X personas/huéspedes"
        total_match = re.search(r"(\d+)\s*(?:personas?|hu[eé]sped(?:es)?)", raw, re.IGNORECASE)
        if total_match:
            val = _to_int(total_match.group(1))
            if val and val > 0:
                return val

        # 5) Payload CSV tipo "2025-11-28,2025-11-30,4" → último número pequeño como occupancy
        csv_parts = [p.strip() for p in raw.split(",") if p.strip()]
        if csv_parts:
            occ_val = _to_int(csv_parts[-1])
            if occ_val and 1 <= occ_val <= 12:
                return occ_val

        # 6) Último número aislado al final (cuando no hay etiquetas).
        # Evita confundir edades ("5 años") con ocupación.
        trailing_num = re.search(r"(\d{1,2})\s*$", raw)
        if trailing_num:
            suffix = raw[trailing_num.start():].lower()
            if "año" not in suffix and "year" not in suffix:
                occ_val = _to_int(trailing_num.group(1))
                if occ_val and 1 <= occ_val <= 12:
                    return occ_val

        return None

    # Busca en el historial la última mención de ocupación >1.
    # Se usa dentro de `DispoPreciosAgent` en el flujo de subagente de disponibilidad, precios y parsing de fechas.
    # Recibe `chat_history` como entrada principal según la firma.
    # Devuelve el resultado calculado para que el siguiente paso lo consuma. Sin efectos secundarios relevantes.
    def _occupancy_from_history(self, chat_history):
        """Busca en el historial la última mención de ocupación >1."""
        if not chat_history:
            return None

        for msg in reversed(chat_history):
            content = getattr(msg, "content", "") or ""
            occ = self._parse_occupancy(content)
            if occ and occ > 1:
                return occ
        return None

    # Recupera el último par de fechas mencionado en el historial.
    # Se usa dentro de `DispoPreciosAgent` en el flujo de subagente de disponibilidad, precios y parsing de fechas.
    # Recibe `chat_history` como entrada principal según la firma.
    # Devuelve el resultado calculado para que el siguiente paso lo consuma. Sin efectos secundarios relevantes.
    def _dates_from_history(self, chat_history):
        """Recupera el último par de fechas mencionado en el historial."""
        if not chat_history:
            return None

        for msg in reversed(chat_history):
            content = getattr(msg, "content", "") or ""
            dates = self._parse_dates(content)
            if dates:
                return dates
        return None

    # Entrada principal del subagente (modo asíncrono) con soporte de memoria.
    # Se usa dentro de `DispoPreciosAgent` en el flujo de subagente de disponibilidad, precios y parsing de fechas.
    # Recibe `pregunta`, `chat_history`, `chat_id` como entradas relevantes junto con el contexto inyectado en la firma.
    # Devuelve un `str` con el resultado de esta operación. Puede realizar llamadas externas o a modelos, activar tools o agentes.
    async def handle(self, pregunta: str, chat_history=None, chat_id: str = None) -> str:
        """Entrada principal del subagente (modo asíncrono) con soporte de memoria."""
        log.info(f"📩 [DispoPreciosAgent] Recibida pregunta: {pregunta}")

        try:
            self._current_chat_id = chat_id
            if not chat_history and self.memory_manager and chat_id:
                try:
                    chat_history = self.memory_manager.get_memory_as_messages(
                        conversation_id=chat_id,
                        limit=20,
                    )
                except Exception as mm_err:
                    log.warning("No se pudo recuperar historial en DispoPreciosAgent: %s", mm_err)
                    chat_history = []

            # Guarda el historial para que la tool pueda reutilizarlo (fechas/ocupación).
            self._last_chat_history = chat_history or []

            # 🔁 Refrescar contexto temporal antes de cada ejecución
            base_prompt = load_prompt("dispo_precios_prompt.txt") or self._get_default_prompt()
            dynamic_context = build_dynamic_context_from_memory(self.memory_manager, chat_id)
            if dynamic_context:
                self.prompt_text = f"{get_time_context()}\n\n{base_prompt.strip()}\n\n{dynamic_context}"
            else:
                self.prompt_text = f"{get_time_context()}\n\n{base_prompt.strip()}"

            result = await self.agent_executor.ainvoke({
                "input": pregunta.strip(),
                "chat_history": chat_history or [],
            })

            output = next(
                (result.get(k) for k in ["output", "final_output", "response"] if result.get(k)),
                ""
            )

            respuesta_final = normalize_reply(output, pregunta, agent_name="DispoPreciosAgent")

            # 🧹 Limpieza de duplicados y redundancias
            seen, cleaned = set(), []
            for line in respuesta_final.splitlines():
                line = line.strip()
                if line and line not in seen:
                    cleaned.append(line)
                    seen.add(line)

            respuesta_final = " ".join(cleaned).strip()

            log.info(f"✅ [DispoPreciosAgent] Respuesta final: {respuesta_final[:200]}")
            return respuesta_final or "No dispongo de disponibilidad en este momento."

        except Exception as e:
            log.error(f"❌ Error en DispoPreciosAgent: {e}", exc_info=True)
            if self.memory_manager and chat_id:
                self.memory_manager.update_memory(
                    chat_id,
                    role="system",
                    content=f"[DispoPreciosAgent] Error interno: {e}"
                )
            return "Ha ocurrido un problema al obtener la disponibilidad."

    # Versión síncrona (wrapper) para integración con DispoPreciosTool.
    # Se usa dentro de `DispoPreciosAgent` en el flujo de subagente de disponibilidad, precios y parsing de fechas.
    # Recibe `user_input`, `chat_history`, `chat_id` como entradas relevantes junto con el contexto inyectado en la firma.
    # Devuelve un `str` con el resultado de esta operación. Sin efectos secundarios relevantes.
    def invoke(self, user_input: str, chat_history=None, chat_id: str = None) -> str:
        """Versión síncrona (wrapper) para integración con DispoPreciosTool."""
        try:
            return self._sync_run(self.handle, user_input, chat_history, chat_id)
        except Exception as e:
            log.error(f"❌ Error en DispoPreciosAgent.invoke: {e}", exc_info=True)
            if self.memory_manager and chat_id:
                self.memory_manager.update_memory(
                    chat_id,
                    role="system",
                    content=f"[DispoPreciosAgent] Error en invocación síncrona: {e}"
                )
            return "Ha ocurrido un error al procesar la disponibilidad o precios."
