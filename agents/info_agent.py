# agents/info_agent.py
import logging
from agents.base_agent import MCPBackedAgent
from core.language_manager import enforce_language, detect_language
from core.utils.utils_prompt import load_prompt

log = logging.getLogger("InfoAgent")

info_prompt = load_prompt("info_prompt.txt")
agent = MCPBackedAgent("InfoAgent")

@agent.mcp.tool()
async def consulta_info(pregunta: str) -> str:
    """
    Consulta información general del hotel (horarios, servicios, ubicación...).
    """
    try:
        lang = detect_language(pregunta)
        tool = await agent.kb_client.get_tool("Base_de_conocimientos_del_hotel")
        raw_reply = await tool.ainvoke({"input": pregunta})
        return enforce_language(pregunta, raw_reply, lang)
    except Exception as e:
        log.error(f"❌ Error en InfoAgent: {e}", exc_info=True)
        return f"⚠️ Error en InfoAgent: {e}"

if __name__ == "__main__":
    print("✅ InfoAgent conectado a la Base de Conocimientos del Hotel")
    agent.mcp.run(transport="stdio", show_banner=False)
