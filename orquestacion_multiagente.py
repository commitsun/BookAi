# orquestacion_multiagente.py
from typing import Literal, TypedDict, List
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END
from pydantic import BaseModel, Field
from langchain_mcp_adapters.client import MultiServerMCPClient


# =========
# Estado compartido
# =========
class GraphState(TypedDict):
    messages: List[dict]
    route: Literal["general_info", "pricing", "other"] | None
    rationale: str | None


# =========
# LLM Router
# =========
llm_router = ChatOpenAI(model="gpt-4o-mini", temperature=0)

class RouteDecision(BaseModel):
    route: Literal["general_info", "pricing", "other"] = Field(...)
    rationale: str = Field(...)

router_system = (
    "Eres un router que decide el destino del mensaje:\n"
    "- 'general_info': horarios, servicios, ubicación, normas.\n"
    "- 'pricing': precios, disponibilidad, reservas.\n"
    "- 'other': todo lo demás (encargado interno).\n"
    "Responde en JSON {route, rationale}"
)

def router_node(state: GraphState) -> GraphState:
    last_msg = state["messages"][-1]["content"]
    structured = llm_router.with_structured_output(RouteDecision)
    decision = structured.invoke([
        {"role": "system", "content": router_system},
        {"role": "user", "content": last_msg},
    ])
    return {**state, "route": decision.route, "rationale": decision.rationale}


# =========
# Configuración de clientes MCP
# =========
mcp_connections = {
    "InfoAgent": {
        "command": "python",
        "args": ["-m", "agents.info_agent"],
        "transport": "stdio"
    },
    "DispoPreciosAgent": {
        "command": "python",
        "args": ["-m", "agents.dispo_precios_agent"],
        "transport": "stdio"
    },
    "InternoAgent": {
        "command": "python",
        "args": ["-m", "agents.interno_agent"],
        "transport": "stdio"
    }
}

mcp_client = MultiServerMCPClient(mcp_connections)



# =========
# Nodos asíncronos (usan ainvoke)
# =========
async def general_info_node(state: GraphState) -> GraphState:
    last_msg = state["messages"][-1]["content"]
    tools = await mcp_client.get_tools(server_name="InfoAgent")
    tool = next(t for t in tools if t.name == "consulta_info")
    reply = await tool.ainvoke({"pregunta": last_msg})
    return {
        "messages": state["messages"] + [{"role": "assistant", "content": reply}],
        "route": state["route"],
        "rationale": state.get("rationale"),
    }

async def pricing_node(state: GraphState) -> GraphState:
    last_msg = state["messages"][-1]["content"]
    tools = await mcp_client.get_tools(server_name="DispoPreciosAgent")
    tool = next(t for t in tools if t.name == "consulta_dispo")
    # TODO: parsear fechas/personas en serio (por ahora está hardcodeado)
    reply = await tool.ainvoke({"fechas": "2025-10-01/2025-10-05", "personas": 2})
    return {
        "messages": state["messages"] + [{"role": "assistant", "content": reply}],
        "route": state["route"],
        "rationale": state.get("rationale"),
    }

async def other_node(state: GraphState) -> GraphState:
    last_msg = state["messages"][-1]["content"]
    tools = await mcp_client.get_tools(server_name="InternoAgent")
    tool = next(t for t in tools if t.name == "consulta_encargado")
    reply = await tool.ainvoke({"mensaje": last_msg})
    return {
        "messages": state["messages"] + [{"role": "assistant", "content": reply}],
        "route": state["route"],
        "rationale": state.get("rationale"),
    }


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
