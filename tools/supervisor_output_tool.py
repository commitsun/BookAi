"""
🧠 Supervisor Input Tool
====================================================
Valida si el mensaje del usuario es apto para el flujo normal.
Responde solo con:
  - 'Aprobado'
  - o 'Interno({...})' para escalar al encargado.
Usa configuración centralizada de modelos LLM desde core/config.py.
"""

import json
import logging
from pydantic import BaseModel, Field
from langchain_core.tools import StructuredTool
from core.utils.utils_prompt import load_prompt
from core.config import ModelConfig, ModelTier  # ✅ Centralización

log = logging.getLogger("SupervisorInputTool")


# =============================================================
# 📄 SCHEMA DE ENTRADA
# =============================================================
class _SISchema(BaseModel):
    mensaje_usuario: str = Field(..., description="Mensaje original del usuario a validar")


# =============================================================
# 🧠 CONFIGURACIÓN CENTRALIZADA DEL LLM
# =============================================================
_SUP_INPUT_PROMPT = load_prompt("supervisor_input_prompt.txt") or (
    "Valida si el mensaje del usuario es apropiado o requiere revisión interna. "
    "Responde con 'Aprobado' o 'Interno({...})' según corresponda."
)

_llm = ModelConfig.get_llm(ModelTier.SUPERVISOR)  # ✅ Usa modelo y temperatura del .env


# =============================================================
# 🧩 FUNCIÓN PRINCIPAL
# =============================================================
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


# =============================================================
# 🧰 TOOL REGISTRADO
# =============================================================
supervisor_input_tool = StructuredTool.from_function(
    name="supervisor_input_tool",
    description="Valida si el input del usuario es apto según el prompt. Devuelve 'Aprobado' o 'Interno({...})'.",
    func=_run_supervisor_input,
    args_schema=_SISchema,
)
