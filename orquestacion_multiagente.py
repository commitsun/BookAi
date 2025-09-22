from typing import Literal, TypedDict, List
from pathlib import Path

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
# Utilidad para cargar prompts
# =========
def load_prompt(filename: str) -> str:
    return (Path("prompts") / filename).read_text(encoding="utf-8")


# =========
# Cargar prompts externos
# =========
main_prompt = load_prompt("main_prompt.txt")
info_prompt = load_prompt("info_prompt.txt")
dispo_precios_prompt = load_prompt("dispo_precios_prompt.txt")
interno_prompt = load_prompt("interno_prompt.txt")


# =========
# LLM Router
# =========
llm_router = ChatOpenAI(model="gpt-4o-mini", temperature=0)

class RouteDecision(BaseModel):
    route: Literal["general_info", "pricing", "other"] = Field(...)
    rationale: str = Field(...)

def router_node(state: GraphState) -> GraphState:
    last_msg = state["messages"][-1]["content"]
    structured = llm_router.with_structured_output(RouteDecision)
    decision = structured.invoke([
        {"role": "system", "content": main_prompt},
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
# Nodos asíncronos con prompts inyectados
# =========
async def general_info_node(state: GraphState) -> GraphState:
    last_msg = state["messages"][-1]["content"]
    tools = await mcp_client.get_tools(server_name="InfoAgent")
    tool = next(t for t in tools if t.name == "consulta_info")
    reply = await tool.ainvoke({"pregunta": f"{info_prompt}\n\nConsulta: {last_msg}"})
    return {
        "messages": state["messages"] + [{"role": "assistant", "content": reply}],
        "route": state["route"],
        "rationale": state.get("rationale"),
    }


async def pricing_node(state: GraphState) -> GraphState:
    last_msg = state["messages"][-1]["content"]
    tools = await mcp_client.get_tools(server_name="DispoPreciosAgent")
    tool = next(t for t in tools if t.name == "consulta_dispo")
    # ⚠️ Aquí deberías parsear fechas/personas de `last_msg` (por ahora fijo)
    reply = await tool.ainvoke({
        "fechas": "2025-10-01/2025-10-05",
        "personas": 2,
        "prompt": dispo_precios_prompt,
        "mensaje": last_msg
    })
    return {
        "messages": state["messages"] + [{"role": "assistant", "content": reply}],
        "route": state["route"],
        "rationale": state.get("rationale"),
    }


async def other_node(state: GraphState) -> GraphState:
    last_msg = state["messages"][-1]["content"]
    tools = await mcp_client.get_tools(server_name="InternoAgent")
    tool = next(t for t in tools if t.name == "consulta_encargado")
    reply = await tool.ainvoke({"mensaje": f"{interno_prompt}\n\nConsulta: {last_msg}"})
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
