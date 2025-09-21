import asyncio
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("MainAgent")

# 🔹 Herramientas simuladas
@mcp.tool()
def elegir_agente(mensaje: str) -> str:
    """
    Decide qué agente usar según el mensaje del cliente.
    """
    if "precio" in mensaje or "disponibilidad" in mensaje or "reserv" in mensaje:
        return "dispo_precios"
    elif "mascota" in mensaje or "piscina" in mensaje or "información" in mensaje:
        return "info"
    else:
        return "interno"

if __name__ == "__main__":
    mcp.run(transport="stdio")
