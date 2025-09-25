import json
from core.state import GraphState
from core.language import enforce_language
from core.mcp_client import mcp_client
from core.utils_prompt import load_prompt
from core.nodes.other_node import other_node  # 👈 fallback si no hay datos

# =========
# Cargar prompt externo
# =========
dispo_precios_prompt = load_prompt("dispo_precios_prompt.txt")


# =========
# Pricing Node
# =========
async def pricing_node(state: GraphState) -> GraphState:
    conversation = state.get("summary") or "\n".join(
        [m["content"] for m in state["messages"] if m["role"] == "user"]
    )

    tools = await mcp_client.get_tools(server_name="DispoPreciosAgent")
    print("🟢 TOOLS DISPONIBLES:", [t.name for t in tools])  # Debug

    # 1️⃣ Sacamos el token con la tool buscar_token
    token = None
    try:
        token_tool = next(t for t in tools if t.name == "buscar_token")
        token_raw = await token_tool.ainvoke({})
        print("🟢 TOKEN RAW:", token_raw)

        token_data = json.loads(token_raw) if isinstance(token_raw, str) else token_raw
        if isinstance(token_data, list) and len(token_data) > 0:
            token = token_data[0].get("key")
        elif isinstance(token_data, dict):
            token = token_data.get("key")

        print("🟢 TOKEN EXTRAÍDO:", token)
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

    # 2️⃣ Tool de disponibilidad y precios
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

    # 3️⃣ Construimos parámetros de prueba (aquí puedes adaptar a fechas reales)
    params = {
        "checkin": "2025-10-25T00:00:00",
        "checkout": "2025-10-27T00:00:00",
        "occupancy": 2,
        "key": token
    }
    print("🟢 PARAMS ENVIADOS:", params)

    # 🚀 Llamamos a la tool de disponibilidad
    raw_reply = await dispo_tool.ainvoke(params)
    print("🟢 RAW REPLY DEL MCP:", raw_reply)

    # Procesamos la respuesta
    try:
        rooms = json.loads(raw_reply) if isinstance(raw_reply, str) else raw_reply
        if not rooms:
            final_reply = "No dispongo de ese dato en este momento."
        else:
            opciones = "\n".join(
                f"- {r['roomTypeName']}: {r['avail']} disponibles → {r['price']}€ por noche"
                for r in rooms
            )
            final_reply = (
                f"Estas son las opciones disponibles del {params['checkin']} "
                f"al {params['checkout']} para {params['occupancy']} personas:\n{opciones}"
            )
    except Exception:
        final_reply = "No dispongo de ese dato en este momento."

    final_reply = enforce_language(
        state["messages"][-1]["content"],
        final_reply,
        state.get("language")
    )

    # 🔹 Si la IA decide que no hay datos → fallback a Interno
    if "no dispongo de ese dato en este momento" in final_reply.lower():
        print("⚠️ PricingAgent no tiene datos → fallback a InternoAgent")
        return await other_node(state)

    return {
        **state,
        "messages": state["messages"] + [{"role": "assistant", "content": final_reply}],
    }
