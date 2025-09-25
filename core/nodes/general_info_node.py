import json
from core.state import GraphState
from core.language import enforce_language
from core.mcp_client import mcp_client
from core.utils_prompt import load_prompt, sanitize_text
from .other_node import other_node

# =========
# Prompt externo
# =========
info_prompt = load_prompt("info_prompt.txt")


async def general_info_node(state: GraphState) -> GraphState:
    conversation = state.get("summary") or "\n".join(
        [m["content"] for m in state["messages"] if m["role"] == "user"]
    )

    tools = await mcp_client.get_tools(server_name="InfoAgent")
    print("🟢 TOOLS INFO DISPONIBLES:", [t.name for t in tools])

    tool = next(
        (t for t in tools if t.name in ["consulta_info", "Base_de_conocimientos_del_hotel"]),
        None
    )

    if not tool:
        final_reply = "⚠️ No encontré ninguna tool válida para responder información general en el MCP remoto."
    else:
        try:
            # Parametrización flexible
            params = {}
            if "pregunta" in tool.args:
                params["pregunta"] = conversation
            elif "consulta" in tool.args:
                params["consulta"] = conversation
            elif "mensaje" in tool.args:
                params["mensaje"] = conversation
            else:
                params = {"input": conversation}

            raw_reply = await tool.ainvoke(params)

            final_reply = enforce_language(
                state["messages"][-1]["content"],
                sanitize_text(raw_reply),
                state.get("language")
            )

            # 🔹 Fallback automático si InfoAgent no tiene datos
            if any(x in final_reply.lower() for x in ["no dispongo", "no tengo"]):
                print("⚠️ InfoAgent no tiene datos → fallback a InternoAgent")
                return await other_node(state)

        except Exception as e:
            final_reply = f"⚠️ Error invocando tool de InfoAgent: {e}"

    return {
        **state,
        "messages": state["messages"] + [{"role": "assistant", "content": final_reply}],
    }
