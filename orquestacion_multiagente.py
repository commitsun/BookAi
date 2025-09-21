# orquestacion_multiagente.py
from typing import Literal, TypedDict, List
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END
from pydantic import BaseModel, Field

# =========
# Estado compartido
# =========
class GraphState(TypedDict):
    messages: List[dict]
    route: Literal["general_info", "pricing", "other"] | None

# =========
# LLMs
# =========
llm_router = ChatOpenAI(model="gpt-4o-mini", temperature=0)
llm_general = ChatOpenAI(model="gpt-4o-mini", temperature=0)
llm_pricing = ChatOpenAI(model="gpt-4o-mini", temperature=0)

# =========
# Router
# =========
class RouteDecision(BaseModel):
    route: Literal["general_info", "pricing", "other"] = Field(...)
    rationale: str = Field(...)

router_system = (
    "Eres un router. Decide el destino:\n"
    "- 'general_info': horarios, servicios, ubicación.\n"
    "- 'pricing': precios, disponibilidad, reservas.\n"
    "- 'other': todo lo demás.\n"
    "Responde en JSON {route, rationale}"
)

def router_node(state: GraphState) -> GraphState:
    last_msg = state["messages"][-1]["content"]
    structured = llm_router.with_structured_output(RouteDecision)
    decision = structured.invoke([
        {"role": "system", "content": router_system},
        {"role": "user", "content": last_msg},
    ])
    return {**state, "route": decision.route}

# =========
# Agentes simulados (pueden llamar a tus MCP en el futuro)
# =========
def general_info_node(state: GraphState) -> GraphState:
    reply = "Check-in 14:00, check-out 12:00. Servicios: WiFi, piscina climatizada."
    return {"messages": state["messages"] + [{"role": "assistant", "content": reply}], "route": state["route"]}

def pricing_node(state: GraphState) -> GraphState:
    reply = "Disponible. Precio 200€/noche para 2 personas."
    return {"messages": state["messages"] + [{"role": "assistant", "content": reply}], "route": state["route"]}

def other_node(state: GraphState) -> GraphState:
    reply = "Puedo ayudarte con información del hotel o precios/disponibilidad."
    return {"messages": state["messages"] + [{"role": "assistant", "content": reply}], "route": state["route"]}

# =========
# Construcción del grafo
# =========
graph = StateGraph(GraphState)

graph.add_node("router", router_node)
graph.add_node("general_info", general_info_node)
graph.add_node("pricing", pricing_node)
graph.add_node("other", other_node)

graph.set_entry_point("router")

def route_edge(state: GraphState):
    return state["route"]

graph.add_conditional_edges("router", route_edge, {
    "general_info": "general_info",
    "pricing": "pricing",
    "other": "other",
})

graph.add_edge("general_info", END)
graph.add_edge("pricing", END)
graph.add_edge("other", END)

app = graph.compile()
