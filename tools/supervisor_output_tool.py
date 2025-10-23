# tools/supervisor_output_tool.py
import logging
from pydantic import BaseModel, Field
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from core.utils.utils_prompt import load_prompt

log = logging.getLogger("SupervisorOutputTool")

class _SOSchema(BaseModel):
    input_usuario: str = Field(..., description="Mensaje original del usuario")
    respuesta_agente: str = Field(..., description="Respuesta generada por el agente principal")

_SUP_OUTPUT_PROMPT = load_prompt("supervisor_output_prompt.txt")
_llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

def _run_supervisor_output(input_usuario: str, respuesta_agente: str) -> str:
    """
    Debe devolver EXACTAMENTE el bloque con:
      Estado: ...
      Motivo: ...
      Prueba: ...
      Sugerencia: ...
    Si el formato no es válido, fuerza 'Revisión Necesaria' con plantilla.
    """
    try:
        res = _llm.invoke([
            {"role": "system", "content": _SUP_OUTPUT_PROMPT},
            {"role": "user", "content": f"Input del usuario:\n{input_usuario}\n\nRespuesta del agente:\n{respuesta_agente}"}
        ])
        out = (res.content or "").strip()
        log.info(f"🧾 [Supervisor OUTPUT] salida modelo:\n{out}")

        # Validación mínima de formato (sin reglas de negocio hardcodeadas)
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        keys = ("Estado:", "Motivo:", "Prueba:", "Sugerencia:")
        valid = all(any(line.startswith(k) for line in lines) for k in keys)
        if valid:
            return out

        fallback = (
            "Estado: Revisión Necesaria\n"
            "Motivo: Salida no conforme al formato esperado.\n"
            "Prueba: [No disponible]\n"
            "Sugerencia: Revisión manual por el encargado."
        )
        log.warning("⚠️ [Supervisor OUTPUT] Formato no conforme. Forzando Revisión Necesaria.")
        return fallback

    except Exception as e:
        log.error(f"❌ [Supervisor OUTPUT] Error LLM: {e}", exc_info=True)
        return (
            "Estado: Revisión Necesaria\n"
            "Motivo: Error interno del auditor.\n"
            "Prueba: [No disponible]\n"
            "Sugerencia: Revisión manual por el encargado."
        )

supervisor_output_tool = StructuredTool.from_function(
    name="supervisor_output_tool",
    description="Audita la salida del agente. Devuelve plantilla Estado/Motivo/Prueba/Sugerencia según el prompt.",
    func=_run_supervisor_output,
    args_schema=_SOSchema,
)
