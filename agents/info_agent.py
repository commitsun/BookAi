from fastmcp import FastMCP
import logging

# 🔇 Silenciar TODOS los logs (uvicorn, mcp, fastmcp)
logging.getLogger().handlers.clear()
logging.basicConfig(level=logging.CRITICAL, force=True)
logging.getLogger("uvicorn").setLevel(logging.CRITICAL)
logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)
logging.getLogger("uvicorn.access").setLevel(logging.CRITICAL)
logging.getLogger("mcp").setLevel(logging.CRITICAL)
logging.getLogger("fastmcp").setLevel(logging.CRITICAL)

mcp = FastMCP("InfoAgent")

@mcp.tool()
def consulta_info(pregunta: str) -> str:
    """
    Simulación de consulta a base de conocimientos del hotel.
    """
    if "mascota" in pregunta.lower():
        return "No se permiten mascotas en el hotel."
    elif "piscina" in pregunta.lower():
        return "Sí, contamos con piscina climatizada."
    return "No dispongo de ese dato, consultaré con el encargado."

if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
