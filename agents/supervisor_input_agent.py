import logging
from fastmcp import FastMCP
from langchain_openai import ChatOpenAI
from core.observability import ls_context  

log = logging.getLogger("SupervisorInputAgent")

mcp = FastMCP("SupervisorInputAgent")
llm = ChatOpenAI(model="gpt-4.1-mini", temperature=0.2)

# Cargar prompt desde /prompts/supervisor_input_prompt.txt
with open("prompts/supervisor_input_prompt.txt", "r", encoding="utf-8") as f:
    SUPERVISOR_INPUT_PROMPT = f.read()


@mcp.tool()
async def evaluar_input(mensaje_usuario: str) -> str:
    """
    Evalúa si el mensaje del usuario es apto según las normas hoteleras.
    Devuelve:
    - 'Aprobado' si el mensaje es válido
    - 'Interno({...})' si se requiere escalar al agente interno
    """
    with ls_context(
        name="SupervisorInputAgent.evaluar_input",
        metadata={"mensaje_usuario": mensaje_usuario},
        tags=["supervisor", "input"],
    ):
        try:
            response = await llm.ainvoke([
                {"role": "system", "content": SUPERVISOR_INPUT_PROMPT},
                {"role": "user", "content": mensaje_usuario},
            ])
            return response.content.strip()
        except Exception as e:
            log.error(f"❌ Error en SupervisorInputAgent: {e}", exc_info=True)
            return (
                "Interno({"
                "\"estado\": \"No Aprobado\", "
                "\"motivo\": \"Error interno al evaluar input\", "
                "\"prueba\": \"-\", "
                "\"sugerencia\": \"Revisar logs\""
                "})"
            )


if __name__ == "__main__":
    print("✅ SupervisorInputAgent operativo")
    mcp.run(transport="stdio", show_banner=False)
