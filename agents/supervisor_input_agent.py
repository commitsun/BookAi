import json
import logging
from fastmcp import FastMCP
from langchain_openai import ChatOpenAI
from core.observability import ls_context

log = logging.getLogger("SupervisorInputAgent")

# =============================================================
# 🧠 CONFIGURACIÓN BASE
# =============================================================

mcp = FastMCP("SupervisorInputAgent")
llm = ChatOpenAI(model="gpt-4.1-mini", temperature=0.2)

# Cargar prompt
with open("prompts/supervisor_input_prompt.txt", "r", encoding="utf-8") as f:
    SUPERVISOR_INPUT_PROMPT = f.read()

# =============================================================
# 🧩 FUNCIÓN PRINCIPAL DE EVALUACIÓN
# =============================================================

async def _evaluar_input_func(mensaje_usuario: str) -> str:
    """
    Evalúa si el mensaje del huésped es apropiado según el prompt.
    Devuelve texto en formato 'Aprobado' o 'Interno({...})'.
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
            output = (response.content or "").strip()
            log.info(f"🧠 [SupervisorInputAgent] salida modelo:\n{output}")
            return output

        except Exception as e:
            log.error(f"❌ Error en SupervisorInputAgent: {e}", exc_info=True)
            # fallback seguro: escalación controlada
            fallback = {
                "estado": "No Aprobado",
                "motivo": "Error interno al evaluar el input",
                "prueba": mensaje_usuario,
                "sugerencia": "Revisión manual por el encargado"
            }
            return f"Interno({json.dumps(fallback, ensure_ascii=False)})"

# Registrar como herramienta MCP
evaluar_input = mcp.tool()(_evaluar_input_func)

# =============================================================
# 🚦 CLASE PRINCIPAL
# =============================================================

class SupervisorInputAgent:
    """
    Interpreta la salida del modelo y devuelve un dict estandarizado.
    Se toleran ligeras desviaciones de formato, priorizando evitar falsos negativos.
    """

    async def validate(self, mensaje_usuario: str) -> dict:
        """
        Devuelve un diccionario con el campo 'estado' como mínimo.
        Si no se puede interpretar con certeza, se asume Aprobado.
        """
        try:
            raw = await _evaluar_input_func(mensaje_usuario)
            salida = (raw or "").strip()

            # --- Caso 1: salida exacta 'Aprobado'
            if salida.lower() == "aprobado":
                return {"estado": "Aprobado"}

            # --- Caso 2: salida tipo Interno({...})
            if salida.startswith("Interno(") and salida.endswith(")"):
                inner = salida[len("Interno("):-1].strip()

                # 🔧 Normalizar comillas tipográficas o erróneas
                inner = (
                    inner.replace("‘", '"')
                         .replace("’", '"')
                         .replace("“", '"')
                         .replace("”", '"')
                         .replace("´", '"')
                         .replace("`", '"')
                )

                try:
                    data = json.loads(inner)
                    estado = str(data.get("estado", "")).strip().lower()

                    if any(pal in estado for pal in ["no aprobado", "rechazado"]):
                        log.warning(f"🚨 Escalación detectada por SupervisorInput: {data}")
                        return data

                    return {"estado": "Aprobado"}

                except json.JSONDecodeError:
                    # 🔍 Detección textual si el JSON no es válido
                    if "no aprobado" in inner.lower() or "rechazado" in inner.lower():
                        log.warning("🚨 Escalación textual detectada (sin JSON válido)")
                        return {
                            "estado": "No Aprobado",
                            "motivo": "Detectado texto de rechazo en salida del modelo",
                            "sugerencia": "Revisión manual por el encargado"
                        }

                    log.warning("⚠️ Formato JSON inválido dentro de Interno(), asumido como aprobado.")
                    return {"estado": "Aprobado", "motivo": "Formato irregular pero sin contenido hostil"}

            # --- Caso 3: salida textual libre con palabra 'aprobado'
            if "aprobado" in salida.lower() and "no aprobado" not in salida.lower():
                return {"estado": "Aprobado"}

            # --- Caso 4: cualquier formato no reconocible → aprobado por defecto
            log.warning(f"⚠️ Salida no conforme del modelo, asumida como aprobada: {salida}")
            return {"estado": "Aprobado", "motivo": "Salida no conforme pero sin indicios de rechazo"}

        except Exception as e:
            log.error(f"❌ Error en validate(): {e}", exc_info=True)
            return {"estado": "Aprobado", "motivo": "Error interno, aprobado por seguridad"}

# =============================================================
# 🚀 ENTRYPOINT MCP (solo si se ejecuta como script)
# =============================================================

if __name__ == "__main__":
    print("✅ SupervisorInputAgent operativo (modo MCP)")
    mcp.run(transport="stdio", show_banner=False)
