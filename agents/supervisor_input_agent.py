import logging
from fastmcp import FastMCP
from langchain_openai import ChatOpenAI
from core.observability import ls_context

log = logging.getLogger("SupervisorInputAgent")

mcp = FastMCP("SupervisorInputAgent")
llm = ChatOpenAI(model="gpt-4.1-mini", temperature=0.2)

# Cargar prompt
with open("prompts/supervisor_input_prompt.txt", "r", encoding="utf-8") as f:
    SUPERVISOR_INPUT_PROMPT = f.read()


# 🔹 Define la función base (no decorada todavía)
async def _evaluar_input_func(mensaje_usuario: str) -> str:
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

# 🔹 Registra la función como tool MCP (wrapper)
evaluar_input = mcp.tool()(_evaluar_input_func)


class SupervisorInputAgent:
    async def validate(self, mensaje_usuario: str) -> str:
        try:
            # ✅ Llama a la función base directamente (no al wrapper)
            return await _evaluar_input_func(mensaje_usuario)
        except Exception as e:
            log.error(f"⚠️ Error en validate: {e}", exc_info=True)
            return "Aprobado"


if __name__ == "__main__":
    print("✅ SupervisorInputAgent operativo")
    mcp.run(transport="stdio", show_banner=False)
