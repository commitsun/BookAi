import os
from fastmcp import FastMCP
from langchain_openai import ChatOpenAI
from core.utils_prompt import load_prompt
from core.language import enforce_language, detect_language
from utils.logging_config import silence_logs
from dotenv import load_dotenv
from core.mcp_client import mcp_client  # 👈 integración con la KB

# =========
# Setup
# =========
load_dotenv()
silence_logs()

interno_prompt = load_prompt("interno_prompt.txt")

api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise RuntimeError("❌ Falta la variable OPENAI_API_KEY en el entorno.")

llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.3, api_key=api_key)
mcp = FastMCP("InternoAgent")

# =========
# Tool principal
# =========
@mcp.tool()
async def consulta_encargado(mensaje: str) -> str:
    """
    Consulta con el encargado humano cuando no hay respuesta de otros agentes.
    1. Intenta primero buscar en la KnowledgeBase.
    2. Si no está, escala al encargado humano usando el prompt interno.
    """
    try:
        lang = detect_language(mensaje)

        # 🔹 Paso 1 → intentar resolver con la KB vía MCP
        try:
            tools = await mcp_client.get_tools(server_name="KnowledgeBase")
            kb_tool = next(t for t in tools if t.name == "consulta_kb")
            kb_reply = await kb_tool.ainvoke({"clave": mensaje})

            if kb_reply and "no dispongo" not in kb_reply.lower():
                reply = enforce_language(mensaje, kb_reply, lang)
                reply = reply.encode("utf-8", errors="replace").decode("utf-8")  # parche UTF-8
                return reply
        except Exception as kb_err:
            print(f"⚠️ KB no disponible en InternoAgent: {kb_err}")

        # 🔹 Paso 2 → escalar al encargado humano vía LLM
        response = llm.invoke([
            {"role": "system", "content": interno_prompt},
            {"role": "user", "content": mensaje},
        ])
        reply = enforce_language(mensaje, response.content, lang)
        reply = reply.encode("utf-8", errors="replace").decode("utf-8")  # parche UTF-8
        return reply

    except Exception as e:
        return f"⚠️ Error en InternoAgent: {e}"


# =========
# Run
# =========
if __name__ == "__main__":
    print("✅ InternoAgent arrancado con tool: consulta_encargado")
    mcp.run(transport="stdio", show_banner=False)
