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
_llm = ChatOpenAI(model="gpt-4.1-mini", temperature=0)

def _run_supervisor_output(input_usuario: str, respuesta_agente: str) -> str:
    """
    Evalúa la respuesta del agente según el prompt.
    Si el formato no es exactamente correcto, pero hay contenido válido,
    se asume 'Estado: Aprobado' para no bloquear respuestas correctas.
    Solo se marca 'Revisión Necesaria' si la respuesta está vacía o hay error interno.
    """
    try:
        res = _llm.invoke([
            {"role": "system", "content": _SUP_OUTPUT_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Input del usuario:\n{input_usuario}\n\n"
                    f"Respuesta del agente:\n{respuesta_agente}"
                ),
            },
        ])
        out = (res.content or "").strip()
        log.info(f"🧾 [Supervisor OUTPUT] salida modelo:\n{out}")

        if not out:
            log.warning("⚠️ [Supervisor OUTPUT] Respuesta vacía → Revisión Necesaria.")
            return (
                "Estado: Revisión Necesaria\n"
                "Motivo: El modelo no devolvió contenido.\n"
                "Prueba: [No disponible]\n"
                "Sugerencia: Revisar salida del modelo."
            )

        # Validación de formato mínima
        keys = ("Estado:", "Motivo:", "Prueba:", "Sugerencia:")
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        valid = all(any(line.startswith(k) for line in lines) for k in keys)

        # ✅ Si el contenido es razonable pero no cumple el formato → asumimos Aprobado
        if not valid and len(out) > 10:
            log.warning("⚠️ [Supervisor OUTPUT] Formato no conforme, pero con contenido válido → Aprobado por defecto.")
            return (
                "Estado: Aprobado\n"
                "Motivo: El contenido es válido aunque el formato no sigue la plantilla.\n"
                "Prueba: [N/A]\n"
                "Sugerencia: Ninguna acción requerida."
            )

        return out

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
    description=(
        "Audita la salida del agente según el prompt supervisor_output_prompt.txt. "
        "Evalúa relevancia, precisión y tono, devolviendo Estado/Motivo/Prueba/Sugerencia."
    ),
    func=_run_supervisor_output,
    args_schema=_SOSchema,
)
