import logging
from fastmcp import FastMCP
from langchain_openai import ChatOpenAI
from core.observability import ls_context

log = logging.getLogger("SupervisorOutputAgent")

mcp = FastMCP("SupervisorOutputAgent")
llm = ChatOpenAI(model="gpt-4.1-mini", temperature=0.3)

# Cargar prompt
with open("prompts/supervisor_output_prompt.txt", "r", encoding="utf-8") as f:
    SUPERVISOR_OUTPUT_PROMPT = f.read()


# 🔹 Define la función base (sin decorar)
async def _auditar_respuesta_func(input_usuario: str, respuesta_agente: str) -> str:
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

# 🔹 Registra la función como MCP tool
auditar_respuesta = mcp.tool()(_auditar_respuesta_func)


class SupervisorOutputAgent:
    async def validate(self, user_input: str, agent_response: str) -> str:
        try:
            # ✅ Llama a la función original directamente
            return await _auditar_respuesta_func(user_input, agent_response)
        except Exception as e:
            log.error(f"⚠️ Error en validate (output): {e}", exc_info=True)
            return (
                "Estado: Revisión Necesaria\n"
                "Motivo: Error al auditar respuesta\n"
                "Prueba: -\n"
                "Sugerencia: Revisar logs"
            )


if __name__ == "__main__":
    print("✅ SupervisorOutputAgent operativo")
    mcp.run(transport="stdio", show_banner=False)
