import json
from core.state import GraphState
from core.mcp_client import mcp_client
from core.utils_prompt import load_prompt
from core.nodes.other_node import other_node  # fallback
from core.reply_utils import normalize_reply

# =========
# Prompt externo (si lo usas para estilo/resumen)
# =========
info_prompt = load_prompt("info_prompt.txt")


# =========
# General Info Node
# =========
async def general_info_node(state: GraphState) -> GraphState:
    user_question = state["messages"][-1]["content"]

    tools = await mcp_client.get_tools(server_name="InfoAgent")
    print("🟢 TOOLS INFO DISPONIBLES:", [t.name for t in tools])

    tool = next((t for t in tools if t.name == "Base_de_conocimientos_del_hotel"), None)

    if not tool:
        final_reply = "No dispongo de ese dato en este momento."
    else:
        try:
            raw_reply = await tool.ainvoke({"input": user_question})
            final_reply = normalize_reply(
                raw_reply,
                user_question,
                state.get("language"),
                source="InfoAgent"
            )

            if not final_reply.strip() or "no dispongo" in final_reply.lower():
                print("⚠️ InfoAgent devolvió vacío → fallback a InternoAgent")
                return await other_node(state)

        except Exception as e:
            final_reply = f"⚠️ Error invocando Base_de_conocimientos_del_hotel: {e}"

    return {
        **state,
        "messages": state["messages"] + [{"role": "assistant", "content": final_reply}],
    }
