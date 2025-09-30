# core/router.py

from typing import Optional
from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from core.state import GraphState
from core.language import detect_language
from core.message_composition.utils_prompt import load_prompt

main_prompt = load_prompt("main_prompt.txt")

llm_router = ChatOpenAI(model="gpt-4o-mini", temperature=0)
llm_think = ChatOpenAI(model="gpt-4o-mini", temperature=0)

class RouteDecision(BaseModel):
    route: str = Field(..., description="Ruta: general_info, pricing, other")
    rationale: str = Field(..., description="Razón de la decisión")

def router_node(state: GraphState) -> GraphState:
    last_msg = state["messages"][-1]["content"]

    try:
        user_lang = detect_language(last_msg)
    except Exception:
        user_lang = "es"

    # Paso 1: THINK obligatorio
    think_result = llm_think.invoke([
        {"role": "system", "content": "Reflexiona sobre la intención del usuario."},
        {"role": "user", "content": last_msg},
    ])
    rationale = think_result.content.strip()

    # Paso 2: Routing estructurado
    structured = llm_router.with_structured_output(RouteDecision)
    decision: RouteDecision = structured.invoke([
        {"role": "system", "content": main_prompt},
        {"role": "user", "content": last_msg},
    ])

    normalized_route = decision.route.strip().lower()
    if normalized_route not in ["general_info", "pricing", "other"]:
        normalized_route = "other"

    # 🔹 Regla explícita: saludos → other
    if any(word in rationale.lower() for word in ["saludo", "greeting", "hello", "hola", "مرحبا"]):
        normalized_route = "other"

    print(f"🛣️ Router decidió: {decision.route} → {normalized_route}")
    print(f"💭 Think: {rationale}")

    return {
        **state,
        "route": normalized_route,
        "rationale": rationale,
        "language": user_lang,
    }
