from mcp.server.fastmcp import FastMCP

mcp = FastMCP("SupervisorAgent")

@mcp.tool()
def decision_supervisor(mensaje: str) -> str:
    """
    Escala la conversación a un supervisor.
    """
    return f"El supervisor revisará la consulta: {mensaje}"

if __name__ == "__main__":
    mcp.run(transport="stdio")
