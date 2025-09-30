from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from core.message_composition.language import detect_language
from core.state import GraphState
from core.message_composition.utils_prompt import load_prompt

# =========
# Cargar prompt principal del orquestador BookAI
# =========
main_prompt = load_prompt("main_prompt.txt")

# =========
# LLM router
# =========
llm_router = ChatOpenAI(model="gpt-4o-mini", temperature=0)

# =========
# Modelo de salida estructurada
# =========
class RouteDecision(BaseModel):
    route: str = Field(
        ...,
        description=(
            "Ruta elegida. Debe ser exactamente uno de estos valores: "
            "general_info, pricing, other"
        )
    )
    rationale: str = Field(..., description="Razón de la elección")

# =========
# Nodo router
# =========
def router_node(state: GraphState) -> GraphState:
    last_msg = state["messages"][-1]["content"]

    # Detectar idioma con fallback seguro
    try:
        user_lang = detect_language(last_msg)
    except Exception:
        user_lang = "es"

    # Pedir al LLM que decida la ruta
    structured = llm_router.with_structured_output(RouteDecision)
    decision = structured.invoke([
        {"role": "system", "content": main_prompt},
        {"role": "user", "content": last_msg},
    ])

    # Normalizar y validar la ruta
    normalized_route = decision.route.strip().lower()
    if normalized_route not in ["general_info", "pricing", "other"]:
        normalized_route = "other"

    print(f"🛣️ Router decidió: {decision.route} → {normalized_route}")

    return {
        **state,
        "route": normalized_route,
        "rationale": decision.rationale,
        "language": user_lang,
    }
