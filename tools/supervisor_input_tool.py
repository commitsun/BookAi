# tools/supervisor_input_tool.py
import json
import logging
from pydantic import BaseModel, Field
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from core.utils.utils_prompt import load_prompt

log = logging.getLogger("SupervisorInputTool")

class _SISchema(BaseModel):
    mensaje_usuario: str = Field(..., description="Mensaje original del usuario a validar")

_SUP_INPUT_PROMPT = load_prompt("supervisor_input_prompt.txt")
_llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

def _run_supervisor_input(mensaje_usuario: str) -> str:
    """
    Devuelve EXACTAMENTE:
      - 'Aprobado'
      - o bien 'Interno({ ...json... })'
    No hay listas ni reglas fijas: todo lo decide el prompt.
    Si el formato no es válido, se fuerza una escalada con payload estándar.
    """
    try:
        res = _llm.invoke([
            {"role": "system", "content": _SUP_INPUT_PROMPT},
            {"role": "user", "content": mensaje_usuario},
        ])
        out = (res.content or "").strip()
        log.info(f"🧠 [Supervisor INPUT] salida modelo: {out}")

        # Normalizamos: solo dos salidas válidas
        if out == "Aprobado":
            return out

        if out.startswith("Interno(") and out.endswith(")"):
            # Limpia y valida JSON interno si existe, pero sin imponer campos fijos
            inner = out[len("Interno("):-1].strip().strip("`")
            # Si el JSON es inválido, escalamos igualmente con un “wrapper” limpio
            try:
                json.loads(inner)
                return out
            except Exception:
                payload = {
                    "estado": "No Aprobado",
                    "motivo": "Formato inválido de payload devuelto por el supervisor",
                    "prueba": mensaje_usuario,
                    "sugerencia": "Revisión manual por el encargado."
                }
                return f"Interno({json.dumps(payload, ensure_ascii=False)})"

        # Cualquier otra cosa → formato no válido → escalar
        payload = {
            "estado": "No Aprobado",
            "motivo": "Salida no conforme al contrato (ni 'Aprobado' ni 'Interno({...})').",
            "prueba": mensaje_usuario,
            "sugerencia": "Revisión manual por el encargado."
        }
        result = f"Interno({json.dumps(payload, ensure_ascii=False)})"
        log.warning(f"⚠️ [Supervisor INPUT] salida no conforme. Escalando: {result}")
        return result

    except Exception as e:
        log.error(f"❌ [Supervisor INPUT] Error LLM: {e}", exc_info=True)
        payload = {
            "estado": "No Aprobado",
            "motivo": "Error interno del auditor de entrada.",
            "prueba": mensaje_usuario,
            "sugerencia": "Revisión manual por el encargado."
        }
        return f"Interno({json.dumps(payload, ensure_ascii=False)})"

supervisor_input_tool = StructuredTool.from_function(
    name="supervisor_input_tool",
    description="Valida si el input del usuario es apto según el prompt. Devuelve 'Aprobado' o 'Interno({...})'.",
    func=_run_supervisor_input,
    args_schema=_SISchema,
)
