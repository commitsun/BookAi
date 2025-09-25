import json
from core.state import GraphState
from core.language import enforce_language
from core.mcp_client import mcp_client
from core.utils_prompt import load_prompt, sanitize_text

dispo_precios_prompt = load_prompt("dispo_precios_prompt.txt")


async def pricing_node(state: GraphState) -> GraphState:
    conversation = state.get("summary") or "\n".join(
        [m["content"] for m in state["messages"] if m["role"] == "user"]
    )

    tools = await mcp_client.get_tools(server_name="DispoPreciosAgent")
    print("🟢 TOOLS DISPONIBLES:", [t.name for t in tools])  # Debug

    # 1️⃣ Token
    token = None
    try:
        token_tool = next(t for t in tools if t.name == "buscar_token")
        token_raw = await token_tool.ainvoke({})
        print("🟢 TOKEN RAW:", token_raw)

        token_data = json.loads(token_raw) if isinstance(token_raw, str) else token_raw
        if isinstance(token_data, list) and token_data:
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
                "content": "⚠️ No pude obtener el token de autorización. Revisa la configuración."
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
                "content": "⚠️ No encontré la tool 'Disponibilidad_y_precios' en el MCP remoto."
            }],
        }

    # 3️⃣ Params dummy → luego se ajustará dinámicamente
    params = {
        "checkin": "2025-10-25T00:00:00",
        "checkout": "2025-10-27T00:00:00",
        "occupancy": 2,
        "key": token
    }
    print("🟢 PARAMS ENVIADOS:", params)

    # 🚀 Llamada
    raw_reply = await dispo_tool.ainvoke(params)
    print("🟢 RAW REPLY DEL MCP:", raw_reply)

    try:
        rooms = json.loads(sanitize_text(raw_reply)) if isinstance(raw_reply, str) else raw_reply
        if not rooms:
            final_reply = "No encontré disponibilidad en esas fechas."
        else:
            opciones = "\n".join(
                f"- {r['roomTypeName']}: {r['avail']} disponibles → {r['price']}€ por noche"
                for r in rooms
            )
            final_reply = (
                f"Opciones disponibles del {params['checkin']} "
                f"al {params['checkout']} para {params['occupancy']} personas:\n{opciones}"
            )
    except Exception as e:
        final_reply = f"⚠️ Error procesando la respuesta de disponibilidad: {e}"

    final_reply = enforce_language(
        state["messages"][-1]["content"],
        sanitize_text(final_reply),
        state.get("language")
    )

    return {
        **state,
        "messages": state["messages"] + [{"role": "assistant", "content": final_reply}],
    }
