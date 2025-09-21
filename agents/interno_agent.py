from mcp.server.fastmcp import FastMCP

mcp = FastMCP("InternoAgent")

@mcp.tool()
def consulta_encargado(mensaje: str) -> str:
    """
    Simula envío de la consulta al encargado humano.
    """
    return f"He avisado al encargado del hotel: {mensaje}. Esperando respuesta..."

if __name__ == "__main__":
    mcp.run(transport="stdio")
