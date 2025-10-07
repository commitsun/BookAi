import logging
from fastmcp import FastMCP
from dotenv import load_dotenv
from core.language import enforce_language, detect_language
from utils.utils_prompt import load_prompt

# ✅ Cargar entorno
load_dotenv()

# ✅ Configurar logs
logger = logging.getLogger("InternoAgent")

# ✅ Inicializar agente
mcp = FastMCP("InternoAgent")

# ✅ Cargar prompt contextual
INTERNAL_PROMPT = load_prompt("interno_prompt.txt")


@mcp.tool()
async def consulta_encargado(mensaje: str) -> str:
    """
    Responde en nombre del encargado cuando no hay información disponible.
    Nunca inventa datos. Usa un mensaje estándar en el idioma del usuario.
    """
    try:
        lang = detect_language(mensaje)
        reply = "No dispongo de ese dato en este momento. Estoy contactando con el encargado."
        return enforce_language(mensaje, reply, lang)
    except Exception as e:
        logger.error(f"❌ Error en InternoAgent: {e}", exc_info=True)
        return "Ha ocurrido un error interno consultando al encargado."


if __name__ == "__main__":
    print("✅ InternoAgent iniciado correctamente")
    mcp.run(transport="stdio", show_banner=False)
