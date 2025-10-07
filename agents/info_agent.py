import os
import logging
from fastmcp import FastMCP
from dotenv import load_dotenv
from core.language import enforce_language, detect_language
from utils.utils_prompt import load_prompt
from langchain_mcp_adapters.client import MCPClient

load_dotenv()
logger = logging.getLogger("InfoAgent")

mcp = FastMCP("InfoAgent")
INFO_PROMPT = load_prompt("info_prompt.txt")

MCP_URL = os.getenv("ENDPOINT_MCP")
kb_client = MCPClient(transport="streamable_http", url=MCP_URL)


@mcp.tool(name="Base de conocimientos")
async def consulta_info(pregunta: str) -> str:
    """
    Consulta información general en la Base de Conocimientos del hotel.
    Usa el MCP remoto para recuperar datos actualizados sobre servicios, ubicación o políticas.
    """
    try:
        lang = detect_language(pregunta)

        # 🔹 Permite compatibilidad con tu nuevo nombre
        tool = await kb_client.get_tool("Base_de_conocimientos_del_hotel")

        if not tool:
            reply = "No dispongo de esa información en este momento. Estoy consultando al encargado."
            return enforce_language(pregunta, reply, lang)

        raw_reply = await tool.ainvoke({"input": pregunta})
        return enforce_language(pregunta, raw_reply, lang)

    except Exception as e:
        logger.error(f"❌ Error en InfoAgent: {e}", exc_info=True)
        return enforce_language(pregunta, "Ha ocurrido un error interno al consultar la información.", "es")


if __name__ == "__main__":
    print("✅ InfoAgent iniciado correctamente y conectado a la KB")
    mcp.run(transport="stdio", show_banner=False)
