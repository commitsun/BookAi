import json
import logging
import re
from datetime import datetime, timezone
from fastmcp import FastMCP
from core.config import ModelConfig, ModelTier
from core.escalation_db import get_latest_pending_escalation
from core.observability import ls_context

log = logging.getLogger("SupervisorOutputAgent")

# =============================================================
# ⚙️ CONFIGURACIÓN BASE
# =============================================================

mcp = FastMCP("SupervisorOutputAgent")
llm = ModelConfig.get_llm(ModelTier.SUPERVISOR)

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


# Registrar herramienta MCP
auditar_respuesta = mcp.tool()(_auditar_respuesta_func)

# =============================================================
# 🚦 CLASE PRINCIPAL CON MEMORIA Y CONTROL ANTI-LOOP
# =============================================================

class SupervisorOutputAgent:
    """
    Agente de auditoría de salida con integración de memoria y detección de loops.
    """

    def __init__(self, memory_manager=None):
        self.memory_manager = memory_manager
        self._pending_escalation_max_age_min = 20

    def _has_recent_pending_escalation(self, chat_id: str) -> bool:
        if not chat_id:
            return False
        try:
            property_id = None
            if self.memory_manager:
                property_id = self.memory_manager.get_flag(chat_id, "property_id")

            latest = get_latest_pending_escalation(chat_id, property_id=property_id)
            if not latest:
                return False

            ts_raw = str(latest.get("timestamp") or "").strip()
            if not ts_raw:
                return True

            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
            return age_min <= self._pending_escalation_max_age_min
        except Exception:
            return False

    @staticmethod
    def _claims_human_contact_already_done(text: str) -> bool:
        normalized = (text or "").strip().lower()
        if not normalized:
            return False
        done_patterns = [
            r"\bacabo de (enviar|avisar|preguntar|trasladar|consultar)\b",
            r"\bya he (enviado|avisado|preguntado|trasladado|consultado)\b",
            r"\bhe (enviado|avisado|preguntado|trasladado|consultado)\b",
            r"\bestoy esperando (su|la) respuesta\b",
            r"\bte avisar[ée] en cuanto reciba (su|la) respuesta\b",
        ]
        return any(re.search(p, normalized, re.IGNORECASE) for p in done_patterns)

    async def validate(self, user_input: str, agent_response: str, chat_id: str = None) -> dict:
        """Evalúa la respuesta y aplica reglas de tolerancia + detección de loops."""
        try:
            if self._claims_human_contact_already_done(agent_response):
                if not self._has_recent_pending_escalation(chat_id):
                    log.warning(
                        "🚨 Afirmación de contacto humano sin evidencia de escalación activa (chat_id=%s).",
                        chat_id,
                    )
                    return {
                        "estado": "Rechazado",
                        "motivo": (
                            "La respuesta afirma que ya se contactó al encargado, "
                            "pero no hay escalación activa verificable."
                        ),
                        "sugerencia": (
                            "No afirmar acciones ya realizadas sin evidencia. "
                            "Pedir confirmación para escalar o usar formulación condicional."
                        ),
                    }

            # =====================================================
            # 🚨 DETECCIÓN DE BUCLES DE INCISOS / REPETICIÓN
            # =====================================================
            inciso_pattern = re.compile(
                r"(estoy consultando|voy a consultar|un momento|permíteme|déjame).*encargado", re.IGNORECASE
            )
            repetitions = len(re.findall(inciso_pattern, agent_response))

            if repetitions >= 3:
                log.warning("♻️ Posible loop de incisos detectado → marcar como error controlado.")
                return {
                    "estado": "Rechazado",
                    "motivo": "Loop detectado (repetición excesiva de incisos)",
                    "sugerencia": "Detener ejecución y escalar al encargado",
                }

            # =====================================================
            # 🚀 DETECTOR DE MENSAJES DE ESCALACIÓN LEGÍTIMOS
            # =====================================================
            ESCALATION_PATTERNS = [
                r"(un momento|déjame|voy a|permíteme|contactando|consultando).*(encargado|equipo|gerente|hotel)",
                r"(estoy|comunicando|contactar).*(encargado|equipo|hotel)",
                r"(dame|dame un).*(momento|segundo|instante).*consult",
            ]

            for pattern in ESCALATION_PATTERNS:
                if re.search(pattern, agent_response.lower()):
                    log.info("✅ Mensaje de acompañamiento / escalación legítimo → aprobado automáticamente.")
                    return {
                        "estado": "Aprobado",
                        "motivo": "Mensaje de cortesía o escalación válido",
                        "response": agent_response,
                        "sugerencia": None,
                    }

            # =====================================================
            # 🧠 ANÁLISIS CON EL LLM (modo auditor)
            # =====================================================
            raw = await _auditar_respuesta_func(user_input, agent_response)
            salida = (raw or "").strip()

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

            # =====================================================
            # 🎯 REGLAS DE DECISIÓN
            # =====================================================

            # Caso 1: salida directa “Aprobado”
            if salida.lower().startswith("aprobado"):
                return {"estado": "Aprobado", "motivo": "Respuesta correcta aprobada"}

            # Caso 2: salida tipo Interno({...})
            if salida.startswith("Interno(") and salida.endswith(")"):
                inner = salida[len("Interno("):-1].strip()
                try:
                    data = json.loads(inner)
                    estado = str(data.get("estado", "")).lower()

                    if "rechazado" in estado or "no aprobado" in estado:
                        log.warning(f"🚨 Rechazo confirmado por SupervisorOutput: {data}")
                        return data

                    if "revisión" in estado:
                        return {"estado": "Revisión Necesaria", "motivo": data.get("motivo", "")}

                    return {"estado": "Aprobado", "motivo": data.get("motivo", "Aprobado por defecto")}
                except json.JSONDecodeError:
                    log.warning("⚠️ JSON irregular en Interno(), aprobado por seguridad.")
                    return {"estado": "Aprobado", "motivo": "Formato irregular pero sin errores"}

            # =====================================================
            # 🧩 Caso 3: salida con “rechazado” explícito → genera contexto enriquecido
            # =====================================================
            if "rechazado" in salida.lower():
                log.warning("🚨 Rechazo textual detectado por modelo auditor.")

                # 🧾 Recuperar historial reciente (si existe en memoria)
                historial = ""
                if self.memory_manager and chat_id:
                    try:
                        conv = self.memory_manager.get_memory(chat_id, limit=6)
                        if conv:
                            formatted = []
                            for m in conv:
                                role_val = m.get("role")
                                if role_val == "guest":
                                    role = "Huésped"
                                elif role_val == "user":
                                    role = "Hotel"
                                elif role_val in {"assistant", "bookai"}:
                                    role = "BookAI"
                                else:
                                    role = "BookAI"
                                content = m.get("content", "").strip()
                                formatted.append(f"{role}: {content}")
                            historial = "\n".join(formatted)
                    except Exception as e:
                        log.warning(f"⚠️ No se pudo recuperar historial para el contexto: {e}")

                # 🧠 Construir contexto extendido (como antes)
                contexto_extendido = (
                    f"Respuesta rechazada: {agent_response}\n\n"
                    f"Historial reciente:\n{historial if historial else '(sin historial disponible)'}"
                )

                return {
                    "estado": "Rechazado",
                    "motivo": "Modelo marcó rechazo textual",
                    "context": contexto_extendido,  # <— 🔥 clave: esto es lo que InternoAgent usa
                }

            # =====================================================
            # 🩵 Caso 4: Aprobado por defecto
            # =====================================================
            log.info("🩵 Aprobado por defecto (sin indicios de error o loop).")
            return {"estado": "Aprobado", "motivo": "Sin indicios negativos detectados"}

        except Exception as e:
            log.error(f"⚠️ Error en validate (output): {e}", exc_info=True)
            return {"estado": "Aprobado", "motivo": "Error interno, aprobado por seguridad"}


# =============================================================
# 🚀 ENTRYPOINT MCP
# =============================================================

if __name__ == "__main__":
    print("✅ SupervisorOutputAgent operativo (modo MCP)")
    mcp.run(transport="stdio", show_banner=False)
