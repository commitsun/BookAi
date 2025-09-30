from core.state import GraphState
from core.mcp_client import mcp_client
from core.message_composition.reply_utils import normalize_reply
from langchain_openai import ChatOpenAI
from core.language import detect_language
from core.nodes.other_node import other_node

llm_synonyms = ChatOpenAI(model="gpt-4o-mini", temperature=0)

async def general_info_node(state: GraphState) -> GraphState:
    user_question = state["messages"][-1]["content"]

    # 🔹 Expansión automática de la query con sinónimos
    try:
        lang = state.get("language") or detect_language(user_question)
    except Exception:
        lang = "es"

    expanded = llm_synonyms.invoke([
        {"role": "system", "content": f"Expande la siguiente consulta en {lang} con sinónimos útiles para búsqueda."},
        {"role": "user", "content": user_question},
    ]).content

    tools = await mcp_client.get_tools(server_name="InfoAgent")
    tool = next((t for t in tools if t.name == "Base_de_conocimientos_del_hotel"), None)

    if not tool:
        final_reply = "No dispongo de ese dato en este momento."
    else:
        try:
            raw_reply = await tool.ainvoke({"input": expanded})
            final_reply = normalize_reply(raw_reply, user_question, state.get("language"), source="InfoAgent")
            if not final_reply.strip() or "no dispongo" in final_reply.lower():
                return await other_node(state)
        except Exception as e:
            final_reply = f"⚠️ Error consultando InfoAgent: {e}"

    return {
        **state,
        "messages": state["messages"] + [{"role": "assistant", "content": final_reply}],
    }
