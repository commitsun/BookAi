from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from core.language import detect_language
from core.state import GraphState
from core.message_composition.utils_prompt import load_prompt

main_prompt = load_prompt("main_prompt.txt")
llm_router = ChatOpenAI(model="gpt-4o-mini", temperature=0)

class RouteDecision(BaseModel):
    route: str = Field(..., description="Ruta: general_info, pricing o other")
    rationale: str

def router_node(state: GraphState) -> GraphState:
    last_msg = state["messages"][-1]["content"]

    try:
        user_lang = detect_language(last_msg)
    except Exception:
        user_lang = "es"

    structured = llm_router.with_structured_output(RouteDecision)
    decision = structured.invoke([
        {"role": "system", "content": main_prompt},
        {"role": "user", "content": last_msg},
    ])

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
