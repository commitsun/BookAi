from fastmcp import FastMCP
import logging

# 🔇 Silenciar logs de fastmcp y mcp
logging.getLogger("fastmcp").setLevel(logging.ERROR)
logging.getLogger("mcp").setLevel(logging.ERROR)

mcp = FastMCP("InternoAgent")

@mcp.tool()
def consulta_encargado(mensaje: str) -> str:
    """
    Simula envío de la consulta al encargado humano.
    """
    return f"He avisado al encargado del hotel: {mensaje}. Esperando respuesta..."

if __name__ == "__main__":
    # 👇 sin banner
    mcp.run(transport="stdio", show_banner=False)
