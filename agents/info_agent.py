from fastmcp import FastMCP
from utils.logging_config import silence_logs

silence_logs()
mcp = FastMCP("InfoAgent")

@mcp.tool()
def consulta_info(pregunta: str) -> str:
    """
    Simulación de consulta a base de conocimientos del hotel.
    """
    if "mascota" in pregunta.lower():
        return "No se permiten mascotas en el hotel."
    if "piscina" in pregunta.lower():
        return "Sí, contamos con piscina climatizada."
    return "No dispongo de ese dato, consultaré con el encargado."

if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
