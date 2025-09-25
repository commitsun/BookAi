from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from core.language import detect_language
from core.state import GraphState
from core.utils_prompt import load_prompt

# =========
# Cargar prompt principal del orquestador BookAI
# =========
main_prompt = load_prompt("main_prompt.txt")

# =========
# LLM router
# =========
llm_router = ChatOpenAI(model="gpt-4o-mini", temperature=0)

class RouteDecision(BaseModel):
    route: str = Field(..., description="Ruta elegida")
    rationale: str = Field(..., description="Razón de la elección")

# =========
# Mapa de normalización de rutas
# =========
ROUTE_MAP = {
    "Información": "general_info",
    "Info": "general_info",
    "General": "general_info",
    "general_info": "general_info",

    "Disponibilidad/Precios": "pricing",
    "Disponibilidad": "pricing",
    "Precios": "pricing",
    "Precio": "pricing",
    "Habitaciones": "pricing",
    "Reservas": "pricing",
    "Reserva": "pricing",
    "pricing": "pricing",

    "Interno": "other",
    "Encargado": "other",
    "Supervisor": "other",
    "Inciso": "other",
    "other": "other",
}

# =========
# Nodo router
# =========
def router_node(state: GraphState) -> GraphState:
    last_msg = state["messages"][-1]["content"]

    # Detectar idioma con fallback
    try:
        user_lang = detect_language(last_msg)
    except Exception:
        user_lang = "es"

    # Pedir al LLM que decida la ruta según el prompt de BookAI
    structured = llm_router.with_structured_output(RouteDecision)
    decision = structured.invoke([
        {"role": "system", "content": main_prompt},
        {"role": "user", "content": last_msg},
    ])

    # Normalizar la ruta
    normalized_route = ROUTE_MAP.get(decision.route, "other")

    print(f"🛣️ Router decidió: {decision.route} → {normalized_route}")

    return {
        **state,
        "route": normalized_route,
        "rationale": decision.rationale,
        "language": user_lang,
    }
