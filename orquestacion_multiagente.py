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
    language: str | None   # 👈 guardamos idioma detectado


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
# LLMs
# =========
llm_router = ChatOpenAI(model="gpt-4o-mini", temperature=0)
llm_language = ChatOpenAI(model="gpt-4o-mini", temperature=0.3)  # enforcer de idioma


class RouteDecision(BaseModel):
    route: Literal["general_info", "pricing", "other"] = Field(...)
    rationale: str = Field(...)


class LangDetect(BaseModel):
    language: str = Field(..., description="Idioma detectado en código ISO-639-1 (ej. es, en, fr, ar)")


# =========
# Detectar idioma con la IA
# =========
def detect_language(text: str) -> str:
    structured = llm_language.with_structured_output(LangDetect)
    result = structured.invoke([
        {"role": "system", "content": "Detecta el idioma del siguiente texto y respóndelo como código ISO-639-1."},
        {"role": "user", "content": text},
    ])
    return result.language


def enforce_language(user_msg: str, reply: str, lang: str | None = None) -> str:
    """Asegura que la respuesta sea en el idioma detectado del usuario"""
    system_prompt = (
        f"Responde SIEMPRE en {lang}." if lang
        else "Responde SIEMPRE en el mismo idioma que el usuario."
    )

    enforced = llm_language.invoke([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
        {"role": "assistant", "content": reply},
    ])
    return enforced.content


# =========
# Router principal
# =========
def router_node(state: GraphState) -> GraphState:
    last_msg = state["messages"][-1]["content"]
    user_lang = detect_language(last_msg)

    structured = llm_router.with_structured_output(RouteDecision)
    decision = structured.invoke([
        {"role": "system", "content": main_prompt},
        {"role": "user", "content": last_msg},
    ])

    return {
        **state,
        "route": decision.route,
        "rationale": decision.rationale,
        "language": user_lang,   # guardamos idioma
    }


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
# Nodos de ejecución (con historial completo)
# =========
async def general_info_node(state: GraphState) -> GraphState:
    # Usar TODO el historial del usuario como contexto
    conversation = "\n".join([m["content"] for m in state["messages"] if m["role"] == "user"])

    tools = await mcp_client.get_tools(server_name="InfoAgent")
    tool = next(t for t in tools if t.name == "consulta_info")
    reply = await tool.ainvoke({"pregunta": f"{info_prompt}\n\nConversación:\n{conversation}"})

    final_reply = enforce_language(state["messages"][-1]["content"], reply, state.get("language"))
    return {
        "messages": state["messages"] + [{"role": "assistant", "content": final_reply}],
        "route": state["route"],
        "rationale": state.get("rationale"),
        "language": state.get("language"),
    }


async def pricing_node(state: GraphState) -> GraphState:
    # Usar TODO el historial del usuario como contexto
    conversation = "\n".join([m["content"] for m in state["messages"] if m["role"] == "user"])

    tools = await mcp_client.get_tools(server_name="DispoPreciosAgent")
    tool = next(t for t in tools if t.name == "consulta_dispo")
    reply = await tool.ainvoke({
        "fechas": "2025-10-01/2025-10-05",  # TODO: parsear fechas reales
        "personas": 2,
        "prompt": dispo_precios_prompt,
        "mensaje": conversation
    })

    final_reply = enforce_language(state["messages"][-1]["content"], reply, state.get("language"))
    return {
        "messages": state["messages"] + [{"role": "assistant", "content": final_reply}],
        "route": state["route"],
        "rationale": state.get("rationale"),
        "language": state.get("language"),
    }


async def other_node(state: GraphState) -> GraphState:
    # Usar TODO el historial del usuario como contexto
    conversation = "\n".join([m["content"] for m in state["messages"] if m["role"] == "user"])

    tools = await mcp_client.get_tools(server_name="InternoAgent")
    tool = next(t for t in tools if t.name == "consulta_encargado")
    reply = await tool.ainvoke({"mensaje": f"{interno_prompt}\n\nConversación:\n{conversation}"})

    final_reply = enforce_language(state["messages"][-1]["content"], reply, state.get("language"))
    return {
        "messages": state["messages"] + [{"role": "assistant", "content": final_reply}],
        "route": state["route"],
        "rationale": state.get("rationale"),
        "language": state.get("language"),
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
