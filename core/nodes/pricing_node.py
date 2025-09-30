# core/nodes/pricing_node.py

import json
import logging
from core.state import GraphState
from core.mcp_client import mcp_client
from core.language import enforce_language
from core.nodes.other_node import other_node

logger = logging.getLogger(__name__)

async def pricing_node(state: GraphState) -> GraphState:
    user_msg = state["messages"][-1]["content"]
    tools = await mcp_client.get_tools(server_name="DispoPreciosAgent")

    token = None
    try:
        token_tool = next(t for t in tools if t.name == "buscar_token")
        token_raw = await token_tool.ainvoke({})
        token_data = json.loads(token_raw) if isinstance(token_raw, str) else token_raw
        token = token_data[0].get("key") if isinstance(token_data, list) else token_data.get("key")
    except Exception as e:
        logger.error(f"Error obteniendo token: {e}")

    if not token:
        return await other_node(state)

    dispo_tool = next((t for t in tools if t.name == "Disponibilidad_y_precios"), None)
    if not dispo_tool:
        return await other_node(state)

    params = {
        "checkin": "2025-10-25T00:00:00",  # aquí deberías calcular dinámicamente
        "checkout": "2025-10-27T00:00:00",
        "occupancy": 2,
        "key": token,
    }

    try:
        raw_reply = await dispo_tool.ainvoke(params)
        rooms = json.loads(raw_reply) if isinstance(raw_reply, str) else raw_reply

        if not rooms:
            return await other_node(state)

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
        return await other_node(state)

    final_reply = enforce_language(user_msg, final_reply, state.get("language"))

    return {
        **state,
        "messages": state["messages"] + [{"role": "assistant", "content": final_reply}],
    }
