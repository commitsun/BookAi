from .state import GraphState
from .language import enforce_language
from .mcp_client import mcp_client
from .utils_prompt import load_prompt  # 👈 centralizado y seguro
import json

# =========
# Cargar prompts externos
# =========
info_prompt = load_prompt("info_prompt.txt")
dispo_precios_prompt = load_prompt("dispo_precios_prompt.txt")
interno_prompt = load_prompt("interno_prompt.txt")


# =========
# General Info Node
# =========
async def general_info_node(state: GraphState) -> GraphState:
    # Usamos resumen si existe, si no, concatenamos historial de usuario
    conversation = state.get("summary") or "\n".join(
        [m["content"] for m in state["messages"] if m["role"] == "user"]
    )

    # 🔌 Pedir tools disponibles al MCP remoto
    tools = await mcp_client.get_tools(server_name="InfoAgent")
    print("🟢 TOOLS INFO DISPONIBLES:", [t.name for t in tools])  # Debug

    # Buscar tool válida (consulta_info o Base_de_conocimientos_del_hotel)
    tool = next(
        (t for t in tools if t.name in ["consulta_info", "Base_de_conocimientos_del_hotel"]),
        None
    )

    if not tool:
        final_reply = "⚠️ No encontré ninguna tool válida para responder información general en el MCP remoto."
    else:
        try:
            # 🔎 Debug: mostrar args que espera el endpoint
            print("🟢 TOOL INFO SCHEMA:", tool.args)

            # Construir parámetros dinámicamente según lo que soporte la tool
            params = {}
            if "pregunta" in tool.args:
                params["pregunta"] = conversation
            elif "consulta" in tool.args:
                params["consulta"] = conversation
            elif "mensaje" in tool.args:
                params["mensaje"] = conversation
            else:
                # fallback genérico
                params = {"input": conversation}

            raw_reply = await tool.ainvoke(params)

            final_reply = enforce_language(
                state["messages"][-1]["content"],
                raw_reply,
                state.get("language")  # 👈 siempre pasamos idioma detectado
            )
        except Exception as e:
            final_reply = f"⚠️ Error invocando tool de InfoAgent: {e}"

    return {
        **state,
        "messages": state["messages"] + [{"role": "assistant", "content": final_reply}],
    }

# =========
# Pricing Node (invocando solo Disponibilidad_y_precios)
# =========


# =========
# Pricing Node (usando dispo_tool.args en vez de schema)
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
                "content": "⚠️ No pude obtener el token de autorización. Por favor revisa la configuración."
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

    # 3️⃣ Construimos los parámetros correctos (probamos ISO)
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
            final_reply = "Lo siento, no encontré disponibilidad en esas fechas."
        else:
            opciones = "\n".join(
                f"- {r['roomTypeName']}: {r['avail']} disponibles → {r['price']}€ por noche"
                for r in rooms
            )
            final_reply = (
                f"Estas son las opciones disponibles del {params['checkin']} "
                f"al {params['checkout']} para {params['occupancy']} personas:\n{opciones}"
            )
    except Exception as e:
        final_reply = f"⚠️ Error procesando la respuesta de disponibilidad: {e}"

    final_reply = enforce_language(
        state["messages"][-1]["content"],
        final_reply,
        state.get("language")
    )

    return {
        **state,
        "messages": state["messages"] + [{"role": "assistant", "content": final_reply}],
    }

# =========
# Other / Interno Node
# =========
async def other_node(state: GraphState) -> GraphState:
    conversation = state.get("summary") or "\n".join(
        [m["content"] for m in state["messages"] if m["role"] == "user"]
    )

    tools = await mcp_client.get_tools(server_name="InternoAgent")
    tool = next(t for t in tools if t.name == "consulta_encargado")

    reply = await tool.ainvoke({
        "mensaje": (
            f"{interno_prompt}\n\n"
            "⚠️ CRÍTICO: Solo transmite el mensaje al encargado humano y devuelve su respuesta tal cual. "
            "No inventes ni cambies la información.\n\n"
            f"Historial de la conversación (cliente):\n{conversation}"
        )
    })

    final_reply = enforce_language(
        state["messages"][-1]["content"],
        reply,
        state.get("language")  # 👈 siempre pasamos idioma detectado
    )

    return {
        **state,
        "messages": state["messages"] + [{"role": "assistant", "content": final_reply}],
    }
