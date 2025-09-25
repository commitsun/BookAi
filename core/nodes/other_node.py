import json
from core.state import GraphState
from core.language import enforce_language
from core.mcp_client import mcp_client
from core.utils_prompt import load_prompt

interno_prompt = load_prompt("interno_prompt.txt")


# =========
# Other / Interno Node
# =========
async def other_node(state: GraphState) -> GraphState:
    last_msg = state["messages"][-1]["content"].lower().strip()

    # 🔹 Detectar saludos comunes en varios idiomas
    GREETINGS = [
        "hola", "buenos días", "buenas",
        "hello", "hi", "hey",
        "salut", "bonjour",
        "ciao", "hallo"
    ]

    if any(last_msg.startswith(g) for g in GREETINGS):
        # 👉 Respondemos en el idioma detectado
        reply = enforce_language(
            state["messages"][-1]["content"],   # mensaje original
            "Hola, ¿en qué puedo ayudarte hoy?",  # base en español
            state.get("language")               # idioma detectado en router
        )
        return {
            **state,
            "messages": state["messages"] + [{"role": "assistant", "content": reply}],
        }

    # 🔹 Si no es saludo → intentamos con las tools del InternoAgent
    conversation = state.get("summary") or "\n".join(
        [m["content"] for m in state["messages"] if m["role"] == "user"]
    )

    tools = await mcp_client.get_tools(server_name="InternoAgent")
    print("🟢 TOOLS INTERNO DISPONIBLES:", [t.name for t in tools])  # Debug

    tool = next(
        (t for t in tools if t.name in ["consulta_encargado", "Base_de_conocimientos_del_hotel"]),
        None
    )

    if not tool:
        final_reply = "⚠️ No encontré ninguna tool válida para consultas internas en el MCP remoto."
    else:
        try:
            params = {}
            if "mensaje" in tool.args:
                params["mensaje"] = conversation
            elif "pregunta" in tool.args:
                params["pregunta"] = conversation
            elif "consulta" in tool.args:
                params["consulta"] = conversation
            else:
                params = {"input": conversation}

            raw_reply = await tool.ainvoke(params)

            final_reply = enforce_language(
                state["messages"][-1]["content"],
                raw_reply,
                state.get("language")
            )

        except Exception as e:
            final_reply = f"⚠️ Error invocando tool de InternoAgent: {e}"

    return {
        **state,
        "messages": state["messages"] + [{"role": "assistant", "content": final_reply}],
    }
