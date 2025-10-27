import logging
from fastmcp import FastMCP
from langchain_openai import ChatOpenAI
from core.observability import ls_context  # 🟢 NUEVO

log = logging.getLogger("SupervisorOutputAgent")

mcp = FastMCP("SupervisorOutputAgent")
llm = ChatOpenAI(model="gpt-4.1-mini", temperature=0.3)

# Cargar prompt desde /prompts/supervisor_output_prompt.txt
with open("prompts/supervisor_output_prompt.txt", "r", encoding="utf-8") as f:
    SUPERVISOR_OUTPUT_PROMPT = f.read()


@mcp.tool()
async def auditar_respuesta(input_usuario: str, respuesta_agente: str) -> str:
    """
    Audita las respuestas generadas por el sistema antes de enviarlas al huésped.
    Retorna:
    Estado: [Aprobado | Revisión Necesaria | Rechazado]
    Motivo, Prueba, Sugerencia
    """
    with ls_context(
        name="SupervisorOutputAgent.auditar_respuesta",
        metadata={"input_usuario": input_usuario, "respuesta_agente": respuesta_agente},
        tags=["supervisor", "output"],
    ):
        try:
            content = (
                f"Input del usuario:\n{input_usuario}\n\n"
                f"Respuesta generada:\n{respuesta_agente}"
            )
            response = await llm.ainvoke([
                {"role": "system", "content": SUPERVISOR_OUTPUT_PROMPT},
                {"role": "user", "content": content},
            ])
            return response.content.strip()
        except Exception as e:
            log.error(f"❌ Error en SupervisorOutputAgent: {e}", exc_info=True)
            return (
                "Estado: Rechazado\n"
                "Motivo: Error interno al auditar respuesta\n"
                "Prueba: -\n"
                "Sugerencia: Revisar agente de auditoría"
            )


if __name__ == "__main__":
    print("✅ SupervisorOutputAgent operativo")
    mcp.run(transport="stdio", show_banner=False)
