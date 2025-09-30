import json
import logging
from core.state import GraphState
from core.message_composition.language import enforce_language
from core.mcp_client import mcp_client
from core.message_composition.utils_prompt import load_prompt
from core.nodes.other_node import other_node 

logger = logging.getLogger(__name__)

dispo_precios_prompt = load_prompt("dispo_precios_prompt.txt")

async def pricing_node(state: GraphState) -> GraphState:
    user_msg = state["messages"][-1]["content"]

    tools = await mcp_client.get_tools(server_name="DispoPreciosAgent")
    logger.debug(f"TOOLS DISPONIBLES: {[t.name for t in tools]}")

    token = None
    try:
        token_tool = next(t for t in tools if t.name == "buscar_token")
        token_raw = await token_tool.ainvoke({})
        token_data = json.loads(token_raw) if isinstance(token_raw, str) else token_raw
        if isinstance(token_data, list) and token_data:
            token = token_data[0].get("key")
        elif isinstance(token_data, dict):
            token = token_data.get("key")
        logger.debug(f"TOKEN obtenido: {token}")
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

    try:
        dispo_tool = next(t for t in tools if t.name == "Disponibilidad_y_precios")
    except StopIteration:
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
        "key": token
    }
    logger.debug(f"PARAMS ENVIADOS: {params}")

    raw_reply = await dispo_tool.ainvoke(params)
    logger.debug(f"RAW REPLY (PricingAgent): {raw_reply}")

    final_reply = "No dispongo de ese dato en este momento."
    try:
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
    except Exception as e:
        logger.error(f"Error procesando disponibilidad: {e}")

    final_reply = enforce_language(user_msg, final_reply, state.get("language"))

    if "no dispongo" in final_reply.lower():
        logger.warning("PricingAgent no tiene datos → fallback a InternoAgent")
        return await other_node(state)

    return {
        **state,
        "messages": state["messages"] + [{"role": "assistant", "content": final_reply}],
    }
