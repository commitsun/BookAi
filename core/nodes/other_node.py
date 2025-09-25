import json
from core.state import GraphState
from core.mcp_client import mcp_client
from core.reply_utils import normalize_reply

# =========
# Other Node (fallback)
# =========
async def other_node(state: GraphState) -> GraphState:
    user_question = state["messages"][-1]["content"]

    tools = await mcp_client.get_tools(server_name="InternoAgent")
    print("🟢 TOOLS INTERNO DISPONIBLES:", [t.name for t in tools])

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
                source="InternoAgent"
            )

        except Exception as e:
            final_reply = f"⚠️ Error invocando Base_de_conocimientos_del_hotel (Interno): {e}"

    return {
        **state,
        "messages": state["messages"] + [{"role": "assistant", "content": final_reply}],
    }
