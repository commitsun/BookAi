import json
import logging
from core.state import GraphState
from core.language import enforce_language
from core.mcp_client import mcp_client
from .other_node import other_node

logger = logging.getLogger(__name__)

async def pricing_node(state: GraphState) -> GraphState:
    user_msg = state["messages"][-1]["content"]
    tools = await mcp_client.get_tools(server_name="DispoPreciosAgent")

    # Obtener token
    token = None
    try:
        token_tool = next(t for t in tools if t.name == "buscar_token")
        token_raw = await token_tool.ainvoke({})
        token_data = json.loads(token_raw) if isinstance(token_raw, str) else token_raw
        if isinstance(token_data, list) and token_data:
            token = token_data[0].get("key")
        elif isinstance(token_data, dict):
            token = token_data.get("key")
    except Exception as e:
        logger.error(f"Error obteniendo token: {e}")

    if not token:
        return {
            **state,
            "messages": state["messages"] + [{
                "role": "assistant",
                "content": "No dispongo de ese dato en este momento."
            }],
        }

    # Buscar disponibilidad y precios
    dispo_tool = next((t for t in tools if t.name == "Disponibilidad_y_precios"), None)
    if not dispo_tool:
        return {
            **state,
            "messages": state["messages"] + [{
                "role": "assistant",
                "content": "No dispongo de ese dato en este momento."
            }],
        }

    params = {
        "checkin": "2025-10-25T00:00:00",
        "checkout": "2025-10-27T00:00:00",
        "occupancy": 2,
        "key": token,
    }

    try:
        raw_reply = await dispo_tool.ainvoke(params)
        rooms = json.loads(raw_reply) if isinstance(raw_reply, str) else raw_reply

        if isinstance(rooms, list) and rooms:
            opciones = "\n".join(
                f"- {r['roomTypeName']}: {r['avail']} disponibles · {r['price']}€/noche"
                for r in rooms
            )
            final_reply = (
                f"Estas son las opciones disponibles del {params['checkin'][:10]} "
                f"al {params['checkout'][:10]} para {params['occupancy']} personas:\n{opciones}"
            )
        else:
            final_reply = "No dispongo de ese dato en este momento."
    except Exception as e:
        logger.error(f"Error procesando disponibilidad: {e}")
        final_reply = "No dispongo de ese dato en este momento."

    final_reply = enforce_language(user_msg, final_reply, state.get("language"))

    if "no dispongo" in final_reply.lower():
        logger.warning("PricingAgent sin datos → fallback a InternoAgent")
        return await other_node(state)

    return {
        **state,
        "messages": state["messages"] + [{"role": "assistant", "content": final_reply}],
    }
