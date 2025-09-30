import logging
from core.state import GraphState
from core.mcp_client import mcp_client
from core.message_composition.reply_utils import normalize_reply
from core.message_composition.utils_prompt import load_prompt
from .other_node import other_node

logger = logging.getLogger(__name__)

info_prompt = load_prompt("info_prompt.txt")

# Diccionario de sinónimos frecuentes
SYNONYMS = {
    "correo": ["correo", "email", "correo electrónico", "mail", "e-mail"],
    "teléfono": ["telefono", "teléfono", "phone", "móvil", "contacto telefónico"],
    "ubicación": ["ubicación", "dirección", "localización", "address", "dónde está"],
    "wifi": ["wifi", "wi-fi", "internet", "red"],
}

async def general_info_node(state: GraphState) -> GraphState:
    user_question = state["messages"][-1]["content"]

    # Añadir sinónimos relevantes al prompt
    for key, values in SYNONYMS.items():
        if any(v in user_question.lower() for v in values):
            user_question += f" (también conocido como {', '.join(values)})"

    tools = await mcp_client.get_tools(server_name="InfoAgent")
    tool = next((t for t in tools if t.name == "Base_de_conocimientos_del_hotel"), None)

    if not tool:
        final_reply = "No dispongo de ese dato en este momento."
    else:
        try:
            raw_reply = await tool.ainvoke({"input": user_question})
            final_reply = normalize_reply(raw_reply, user_question, state.get("language"), source="InfoAgent")
            if not final_reply.strip() or "no dispongo" in final_reply.lower():
                logger.warning("InfoAgent devolvió vacío → fallback a InternoAgent")
                return await other_node(state)
        except Exception as e:
            final_reply = f"⚠️ Error invocando Base_de_conocimientos_del_hotel: {e}"

    return {
        **state,
        "messages": state["messages"] + [{"role": "assistant", "content": final_reply}],
    }
