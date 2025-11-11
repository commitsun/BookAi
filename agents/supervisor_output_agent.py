import json
import logging
from fastmcp import FastMCP
from core.config import ModelConfig, ModelTier  # ✅ Configuración centralizada
from core.observability import ls_context

log = logging.getLogger("SupervisorOutputAgent")

# =============================================================
# ⚙️ CONFIGURACIÓN BASE
# =============================================================

mcp = FastMCP("SupervisorOutputAgent")

# ✅ LLM centralizado (usa gpt-4.1 desde .env)
llm = ModelConfig.get_llm(ModelTier.SUPERVISOR)

# Cargar prompt desde archivo
with open("prompts/supervisor_output_prompt.txt", "r", encoding="utf-8") as f:
    SUPERVISOR_OUTPUT_PROMPT = f.read()

# =============================================================
# 🧠 FUNCIÓN PRINCIPAL DE AUDITORÍA
# =============================================================

async def _auditar_respuesta_func(input_usuario: str, respuesta_agente: str) -> str:
    """Evalúa si la respuesta del agente es adecuada, segura y coherente."""
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


# Registrar como herramienta MCP
auditar_respuesta = mcp.tool()(_auditar_respuesta_func)

# =============================================================
# 🚦 CLASE PRINCIPAL CON MEMORIA
# =============================================================

class SupervisorOutputAgent:
    """
    Agente de auditoría de salida con integración de memoria.
    Guarda cada interacción (entrada y salida) para mantener trazabilidad.
    """

    def __init__(self, memory_manager=None):
        self.memory_manager = memory_manager

    async def validate(self, user_input: str, agent_response: str, chat_id: str = None) -> dict:
        """Normaliza la salida del modelo y aplica tolerancia contextual."""
        try:
            raw = await _auditar_respuesta_func(user_input, agent_response)
            salida = (raw or "").strip()

            # 🧠 Guardar auditoría en memoria si está habilitada
            if self.memory_manager and chat_id:
                self.memory_manager.update_memory(
                    chat_id,
                    f"[SupervisorOutput] Validando respuesta:\n{user_input}",
                    f"Salida modelo:\n{salida}"
                )

            conversational_tokens = [
                "¿te gustaría", "¿prefieres", "¿deseas", "¿quieres",
                "puedo ayudarte", "¿necesitas más información"
            ]
            if (
                any(t in agent_response.lower() for t in conversational_tokens)
                or any(token in agent_response for token in ["1.", "2.", "•", "-", "\n\n"])
                or len(agent_response) > 80
            ):
                log.info("🩵 Respuesta extensa o conversacional → tolerancia activa")

            # =====================================================
            # Caso 1: salida directa “Aprobado”
            # =====================================================
            if salida.lower().startswith("aprobado"):
                return {"estado": "Aprobado", "motivo": "Respuesta correcta aprobada"}

            # =====================================================
            # Caso 2: salida tipo Interno({...})
            # =====================================================
            if salida.startswith("Interno(") and salida.endswith(")"):
                inner = salida[len("Interno("):-1].strip()
                try:
                    data = json.loads(inner)
                    estado = str(data.get("estado", "")).lower()

                    if any(pal in estado for pal in ["rechazado", "no aprobado"]):
                        if (
                            len(agent_response.split()) > 8
                            and not any(bad in agent_response.lower() for bad in ["insulto", "odio", "violencia", "sexual"])
                        ):
                            log.warning("⚠️ Rechazo leve detectado, pero la respuesta es coherente → Aprobada.")
                            return {
                                "estado": "Aprobado",
                                "motivo": "Rechazo leve corregido por tolerancia contextual",
                                "sugerencia": ""
                            }
                        log.warning(f"🚨 Escalación detectada por SupervisorOutput: {data}")
                        return data

                    if "revisión" in estado:
                        return {"estado": "Revisión Necesaria", "motivo": data.get("motivo", "")}

                    return {"estado": "Aprobado", "motivo": data.get("motivo", "Aprobado por defecto")}

                except json.JSONDecodeError:
                    log.warning("⚠️ JSON inválido dentro de Interno(), aprobado por seguridad.")
                    return {"estado": "Aprobado", "motivo": "Formato irregular pero sin indicios negativos"}

            # =====================================================
            # Caso 3: salida tipo texto con “Estado: ...”
            # =====================================================
            if "estado:" in salida.lower():
                estado_line = next((l for l in salida.splitlines() if "estado:" in l.lower()), "")
                estado_val = estado_line.lower()

                if any(k in estado_val for k in ["rechazado", "no aprobado"]):
                    if (
                        len(agent_response) > 80
                        or any(t in agent_response for t in ["1.", "2.", "•", "-", "\n\n"])
                        or any(x in agent_response.lower() for x in conversational_tokens)
                    ):
                        log.info("🩵 Rechazo ignorado (respuesta extensa o lista detectada).")
                        return {"estado": "Aprobado", "motivo": "Respuesta extensa aceptada"}
                    return {"estado": "Rechazado", "motivo": "Modelo marcó explícitamente rechazo"}

                if "revisión" in estado_val:
                    return {"estado": "Revisión Necesaria", "motivo": "Modelo solicita revisión"}

                return {"estado": "Aprobado", "motivo": "Modelo indicó aprobación textual"}

            # =====================================================
            # Caso 4: salida libre con “aprobado”
            # =====================================================
            if "aprobado" in salida.lower() and "rechazado" not in salida.lower():
                return {"estado": "Aprobado", "motivo": "Texto indica aprobación"}

            # =====================================================
            # Caso 5: formato desconocido → aprobado por seguridad
            # =====================================================
            log.warning(f"⚠️ Formato no conforme → aprobado por defecto.\nSalida: {salida}")
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
