from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from .language import detect_language
from .state import GraphState
from .utils_prompt import load_prompt  # 👈 centralizado y seguro

# =========
# Cargar prompt principal
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
    "Disponibilidad/Precios": "pricing",
    "Interno": "other",
    "Inciso": "other",
    "Supervisor": "other",
    # Aceptamos también las claves internas por si acaso
    "general_info": "general_info",
    "pricing": "pricing",
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
        user_lang = "es"  # 👈 fallback seguro

    # Decisión de ruta
    structured = llm_router.with_structured_output(RouteDecision)
    decision = structured.invoke([
        {"role": "system", "content": main_prompt},
        {"role": "user", "content": last_msg},
    ])

    # Normalizar ruta
    normalized_route = ROUTE_MAP.get(decision.route, "other")

    return {
        **state,
        "route": normalized_route,
        "rationale": decision.rationale,
        "language": user_lang,   # 👈 siempre guardamos idioma detectado
    }
