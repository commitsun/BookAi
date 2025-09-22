from fastmcp import FastMCP
import logging

# 🔇 Silenciar logs de fastmcp y mcp
logging.getLogger("fastmcp").setLevel(logging.ERROR)
logging.getLogger("mcp").setLevel(logging.ERROR)

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
    # 👇 aquí está la clave: show_banner=False en run()
    mcp.run(transport="stdio", show_banner=False)
