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

mcp = FastMCP("InternoAgent")

@mcp.tool()
def consulta_encargado(mensaje: str) -> str:
    """
    Simula envío de la consulta al encargado humano.
    """
    return f"He avisado al encargado del hotel: {mensaje}. Esperando respuesta..."

if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
