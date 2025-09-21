from mcp.server.fastmcp import FastMCP

mcp = FastMCP("DispoPreciosAgent")

@mcp.tool()
def consulta_dispo(fechas: str, personas: int) -> str:
    """
    Simula consulta a motor de reservas.
    """
    if personas == 2:
        return f"Habitación estándar disponible del {fechas} por 200€."
    return "No he podido obtener disponibilidad, lo consulto con el encargado."

if __name__ == "__main__":
    mcp.run(transport="stdio")
