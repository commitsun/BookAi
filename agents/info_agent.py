import os
from fastmcp import FastMCP
from langchain_openai import ChatOpenAI
from core.utils_prompt import load_prompt
from core.language import enforce_language, detect_language
from utils.logging_config import silence_logs
from dotenv import load_dotenv
from core.mcp_client import mcp_client  # 👈 cliente MCP para acceder a KB

# =========
# Setup
# =========
load_dotenv()
silence_logs()

info_prompt = load_prompt("info_prompt.txt")

api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise RuntimeError("❌ Falta la variable OPENAI_API_KEY en el entorno.")

llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.3, api_key=api_key)
mcp = FastMCP("InfoAgent")

# =========
# Tool principal
# =========
@mcp.tool()
async def consulta_info(pregunta: str) -> str:
    """
    Responde preguntas generales sobre el hotel.
    1. Intenta primero resolver con la KnowledgeBase (MCP tool).
    2. Si no hay dato en KB, usa LLM con el prompt original.
    """
    try:
        # Detectamos idioma del cliente
        lang = detect_language(pregunta)

        # 🔎 Paso 1 → intentar resolver con la KB vía MCP
        try:
            tools = await mcp_client.get_tools(server_name="KnowledgeBase")
            kb_tool = next(t for t in tools if t.name == "consulta_kb")
            kb_reply = await kb_tool.ainvoke({"clave": pregunta})

            if kb_reply and "no dispongo" not in kb_reply.lower():
                reply = enforce_language(pregunta, kb_reply, lang)
                # Parche UTF-8
                reply = reply.encode("utf-8", errors="replace").decode("utf-8")
                return reply
        except Exception as kb_err:
            # Si la KB falla, seguimos con el fallback
            print(f"⚠️ KB no disponible: {kb_err}")

        # 🔄 Paso 2 → fallback al LLM con prompt
        response = llm.invoke([
            {"role": "system", "content": info_prompt},
            {"role": "user", "content": pregunta},
        ])
        reply = enforce_language(pregunta, response.content, lang)

        # Parche UTF-8
        reply = reply.encode("utf-8", errors="replace").decode("utf-8")
        return reply

    except Exception as e:
        return f"⚠️ Error en InfoAgent: {e}"


# =========
# Run
# =========
if __name__ == "__main__":
    print("✅ InfoAgent arrancado con tool: consulta_info")
    mcp.run(transport="stdio", show_banner=False)
