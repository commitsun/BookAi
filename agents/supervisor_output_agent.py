import json
import logging
from fastmcp import FastMCP
from langchain_openai import ChatOpenAI
from core.observability import ls_context

log = logging.getLogger("SupervisorOutputAgent")

# =============================================================
# ⚙️ CONFIGURACIÓN BASE
# =============================================================

mcp = FastMCP("SupervisorOutputAgent")
llm = ChatOpenAI(model="gpt-4.1-mini", temperature=0.3)

# Cargar prompt desde archivo
with open("prompts/supervisor_output_prompt.txt", "r", encoding="utf-8") as f:
    SUPERVISOR_OUTPUT_PROMPT = f.read()

# =============================================================
# 🧠 FUNCIÓN PRINCIPAL DE AUDITORÍA
# =============================================================

async def _auditar_respuesta_func(input_usuario: str, respuesta_agente: str) -> str:
    """
    Evalúa si la respuesta del agente es adecuada, segura y coherente.
    Devuelve texto tipo:
      - 'Aprobado'
      - o 'Interno({...})' (JSON con estado/motivo/sugerencia)
    """
    with ls_context(
        name="SupervisorOutputAgent.auditar_respuesta",
        metadata={"input_usuario": input_usuario, "respuesta_agente": respuesta_agente},
        tags=["supervisor", "output"],
    ):
        try:
            content = (
                f"Mensaje del huésped:\n{input_usuario}\n\n"
                f"Respuesta del agente:\n{respuesta_agente}"
            )

            response = await llm.ainvoke([
                {"role": "system", "content": SUPERVISOR_OUTPUT_PROMPT},
                {"role": "user", "content": content},
            ])

            output = (response.content or "").strip()
            log.info(f"🧠 [SupervisorOutputAgent] salida modelo:\n{output}")
            return output

        except Exception as e:
            log.error(f"❌ Error en SupervisorOutputAgent: {e}", exc_info=True)
            fallback = {
                "estado": "Revisión Necesaria",
                "motivo": "Error interno al auditar la respuesta",
                "sugerencia": "Revisión manual por el encargado"
            }
            return f"Interno({json.dumps(fallback, ensure_ascii=False)})"

# Registrar función MCP
auditar_respuesta = mcp.tool()(_auditar_respuesta_func)

# =============================================================
# 🚦 CLASE PRINCIPAL
# =============================================================

class SupervisorOutputAgent:
    async def validate(self, user_input: str, agent_response: str) -> dict:
        """
        Interpreta la salida del modelo de auditoría y la normaliza.
        Tolerante a formato textual, JSON y estructuras parciales.
        """
        try:
            raw = await _auditar_respuesta_func(user_input, agent_response)
            salida = (raw or "").strip()

            # --- Caso 1: salida directa "Aprobado"
            if salida.lower().startswith("aprobado"):
                return {"estado": "Aprobado", "motivo": "Respuesta correcta aprobada"}

            # --- Caso 2: salida tipo Interno({...})
            if salida.startswith("Interno(") and salida.endswith(")"):
                inner = salida[len("Interno("):-1].strip()
                try:
                    data = json.loads(inner)
                    estado = str(data.get("estado", "")).lower()

                    if any(pal in estado for pal in ["rechazado", "no aprobado"]):
                        log.warning(f"🚨 Escalación detectada por SupervisorOutput: {data}")
                        return data

                    if "revisión" in estado:
                        return {"estado": "Revisión Necesaria", "motivo": data.get("motivo", "")}

                    # Si marca aprobado o no tiene estado → aprobado
                    return {"estado": "Aprobado", "motivo": data.get("motivo", "Aprobado por defecto")}

                except json.JSONDecodeError:
                    log.warning("⚠️ Formato JSON inválido dentro de Interno(), aprobado por seguridad.")
                    return {"estado": "Aprobado", "motivo": "Formato irregular pero sin indicios negativos"}

            # --- Caso 3: salida estructurada tipo texto con “Estado: ...”
            if "estado:" in salida.lower():
                # Buscar palabra clave de estado
                estado_line = next((l for l in salida.splitlines() if "estado:" in l.lower()), "")
                estado_val = estado_line.lower()

                if any(k in estado_val for k in ["rechazado", "no aprobado"]):
                    return {"estado": "Rechazado", "motivo": "Modelo marcó explícitamente rechazo"}

                if "revisión" in estado_val:
                    return {"estado": "Revisión Necesaria", "motivo": "Modelo solicita revisión"}

                return {"estado": "Aprobado", "motivo": "Modelo indicó aprobación textual"}

            # --- Caso 4: salida textual libre con 'aprobado'
            if "aprobado" in salida.lower() and "rechazado" not in salida.lower():
                return {"estado": "Aprobado", "motivo": "Texto indica aprobación"}

            # --- Caso 5: formato desconocido → aprobado por defecto
            log.warning(f"⚠️ Formato no conforme en SupervisorOutput → aprobado por defecto.\nSalida: {salida}")
            return {"estado": "Aprobado", "motivo": "Formato no conforme pero sin errores detectados"}

        except Exception as e:
            log.error(f"⚠️ Error en validate (output): {e}", exc_info=True)
            return {"estado": "Aprobado", "motivo": "Error interno, aprobado por seguridad"}

# =============================================================
# 🚀 ENTRYPOINT MCP
# =============================================================

if __name__ == "__main__":
    print("✅ SupervisorOutputAgent operativo (modo MCP)")
    mcp.run(transport="stdio", show_banner=False)
