from .state import GraphState
from .language import enforce_language
from .mcp_client import mcp_client
from .utils_prompt import load_prompt  # 👈 centralizado y seguro

# =========
# Cargar prompts externos
# =========
info_prompt = load_prompt("info_prompt.txt")
dispo_precios_prompt = load_prompt("dispo_precios_prompt.txt")
interno_prompt = load_prompt("interno_prompt.txt")


# =========
# General Info Node
# =========
async def general_info_node(state: GraphState) -> GraphState:
    # Usamos resumen si existe, si no, concatenamos historial
    conversation = state.get("summary") or "\n".join(
        [m["content"] for m in state["messages"] if m["role"] == "user"]
    )

    tools = await mcp_client.get_tools(server_name="InfoAgent")
    tool = next(t for t in tools if t.name == "consulta_info")

    reply = await tool.ainvoke({
        "pregunta": (
            f"{info_prompt}\n\n"
            "⚠️ CRÍTICO: No inventes ni añadas información externa. "
            "Si no tienes el dato, responde que consultarás con el encargado humano.\n\n"
            f"Historial de la conversación (cliente):\n{conversation}"
        )
    })

    final_reply = enforce_language(
        state["messages"][-1]["content"],
        reply,
        state.get("language")  # 👈 siempre pasamos idioma detectado
    )

    return {
        **state,
        "messages": state["messages"] + [{"role": "assistant", "content": final_reply}],
    }


# =========
# Pricing Node
# =========
async def pricing_node(state: GraphState) -> GraphState:
    conversation = state.get("summary") or "\n".join(
        [m["content"] for m in state["messages"] if m["role"] == "user"]
    )

    tools = await mcp_client.get_tools(server_name="DispoPreciosAgent")
    tool = next(t for t in tools if t.name == "consulta_dispo")

    reply = await tool.ainvoke({
        "fechas": "2025-10-01/2025-10-05",  # TODO: parsear fechas reales
        "personas": 2,
        "prompt": dispo_precios_prompt,
        "mensaje": (
            "⚠️ CRÍTICO: No inventes precios ni disponibilidad. "
            "Si no puedes obtener los datos, responde que consultarás con un encargado humano.\n\n"
            f"Historial de la conversación (cliente):\n{conversation}"
        )
    })

    final_reply = enforce_language(
        state["messages"][-1]["content"],
        reply,
        state.get("language")  # 👈 siempre pasamos idioma detectado
    )

    return {
        **state,
        "messages": state["messages"] + [{"role": "assistant", "content": final_reply}],
    }


# =========
# Other / Interno Node
# =========
async def other_node(state: GraphState) -> GraphState:
    conversation = state.get("summary") or "\n".join(
        [m["content"] for m in state["messages"] if m["role"] == "user"]
    )

    tools = await mcp_client.get_tools(server_name="InternoAgent")
    tool = next(t for t in tools if t.name == "consulta_encargado")

    reply = await tool.ainvoke({
        "mensaje": (
            f"{interno_prompt}\n\n"
            "⚠️ CRÍTICO: Solo transmite el mensaje al encargado humano y devuelve su respuesta tal cual. "
            "No inventes ni cambies la información.\n\n"
            f"Historial de la conversación (cliente):\n{conversation}"
        )
    })

    final_reply = enforce_language(
        state["messages"][-1]["content"],
        reply,
        state.get("language")  # 👈 siempre pasamos idioma detectado
    )

    return {
        **state,
        "messages": state["messages"] + [{"role": "assistant", "content": final_reply}],
    }
