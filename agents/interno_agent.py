from fastmcp import FastMCP
from utils.logging_config import silence_logs

silence_logs()
mcp = FastMCP("InternoAgent")

@mcp.tool()
def consulta_encargado(mensaje: str) -> str:
    """
    Simula envío de la consulta al encargado humano.
    """
    return f"He avisado al encargado del hotel: {mensaje}. Esperando respuesta..."

if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
