import json
from core.state import GraphState
from core.language import enforce_language
from core.mcp_client import mcp_client
from core.utils_prompt import load_prompt
from core.nodes.other_node import other_node  # fallback

dispo_precios_prompt = load_prompt("dispo_precios_prompt.txt")

# =========
# Pricing Node
# =========
async def pricing_node(state: GraphState) -> GraphState:
    user_msg = state["messages"][-1]["content"]

    tools = await mcp_client.get_tools(server_name="DispoPreciosAgent")
    print("🟢 TOOLS DISPONIBLES:", [t.name for t in tools])  # Debug

    # 1️⃣ Buscar token
    token = None
    try:
        token_tool = next(t for t in tools if t.name == "buscar_token")
        token_raw = await token_tool.ainvoke({})
        token_data = json.loads(token_raw) if isinstance(token_raw, str) else token_raw
        if isinstance(token_data, list) and token_data:
            token = token_data[0].get("key")
        elif isinstance(token_data, dict):
            token = token_data.get("key")
        print("🟢 TOKEN:", token)
    except Exception as e:
        print("⚠️ Error obteniendo token:", e)

    if not token:
        return {
            **state,
            "messages": state["messages"] + [{
                "role": "assistant",
                "content": "No dispongo de ese dato en este momento."
            }],
        }

    # 2️⃣ Tool de disponibilidad
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

    # 3️⃣ Parámetros de ejemplo (esto luego lo puedes adaptar con fechas reales del cliente)
    params = {
        "checkin": "2025-10-25T00:00:00",
        "checkout": "2025-10-27T00:00:00",
        "occupancy": 2,
        "key": token
    }
    print("🟢 PARAMS ENVIADOS:", params)

    # 🚀 Llamada a la tool
    raw_reply = await dispo_tool.ainvoke(params)
    print("🟢 RAW REPLY:", raw_reply)

    # 4️⃣ Procesar la respuesta
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
        print("⚠️ Error procesando disponibilidad:", e)

    # 5️⃣ Normalizar idioma y estilo
    final_reply = enforce_language(user_msg, final_reply, state.get("language"))

    # 🔹 Fallback a Interno si no hay datos útiles
    if "no dispongo" in final_reply.lower():
        print("⚠️ PricingAgent no tiene datos → fallback a InternoAgent")
        return await other_node(state)

    return {
        **state,
        "messages": state["messages"] + [{"role": "assistant", "content": final_reply}],
    }
