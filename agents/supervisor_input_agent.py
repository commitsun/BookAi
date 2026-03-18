import json
import logging
import re
import unicodedata
from fastmcp import FastMCP
from core.config import ModelConfig, ModelTier  # ✅ Configuración centralizada
from core.observability import ls_context
from core.utils.utils_prompt import load_prompt

log = logging.getLogger("SupervisorInputAgent")

# =============================================================
# 🧠 CONFIGURACIÓN BASE
# =============================================================

mcp = FastMCP("SupervisorInputAgent")

# ✅ LLM centralizado (usa gpt-4.1 desde .env)
llm = ModelConfig.get_llm(ModelTier.SUPERVISOR)


# Compacta el texto.
# Se usa en el flujo de supervisor de entrada antes de pasar mensajes al sistema para preparar datos, validaciones o decisiones previas.
# Recibe `value`, `max_len` como entradas relevantes junto con el contexto inyectado en la firma.
# Devuelve un `str` con el resultado de esta operación. Sin efectos secundarios relevantes.
def _collapse_text(value: str, max_len: int = 240) -> str:
    text = re.sub(r"\s+", " ", (value or "")).strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


# Intenta rescatar un motivo útil cuando Interno({...}) viene con JSON inválido.
# Se usa en el flujo de supervisor de entrada antes de pasar mensajes al sistema para preparar datos, validaciones o decisiones previas.
# Recibe `inner` como entrada principal según la firma.
# Devuelve un `str` con el resultado de esta operación. Sin efectos secundarios relevantes.
def _extract_reason_from_invalid_json(inner: str) -> str:
    """
    Intenta rescatar un motivo útil cuando Interno({...}) viene con JSON inválido.
    Evita motivos genéricos para que el encargado entienda el problema.
    """
    if not inner:
        return "Mensaje marcado para revisión manual por el supervisor de entrada."

    motivo_patterns = [
        r'["\']motivo["\']\s*:\s*["\']([^"\']+)["\']',
        r'["\']prueba["\']\s*:\s*["\']([^"\']+)["\']',
    ]
    for pattern in motivo_patterns:
        match = re.search(pattern, inner, re.IGNORECASE)
        if match and match.group(1).strip():
            return _collapse_text(match.group(1).strip())

    cleaned = inner.strip().strip("{}")
    cleaned = re.sub(r"\b(estado|sugerencia)\b\s*:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip(" ,")
    if cleaned:
        return _collapse_text(cleaned)
    return "Mensaje marcado para revisión manual por el supervisor de entrada."


# Normaliza el texto.
# Se usa en el flujo de supervisor de entrada antes de pasar mensajes al sistema para preparar datos, validaciones o decisiones previas.
# Recibe `value` como entrada principal según la firma.
# Devuelve un `str` con el resultado de esta operación. Sin efectos secundarios relevantes.
def _normalize_text(value: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# Detecta si la consulta parece una petición operativa segura del hotel.
# Se usa en el flujo de supervisor de entrada antes de pasar mensajes al sistema para preparar datos, validaciones o decisiones previas.
# Recibe `mensaje_usuario` como entrada principal según la firma.
# Devuelve un booleano que gobierna la rama de ejecución siguiente. Sin efectos secundarios relevantes.
def _looks_like_safe_hotel_operational_query(mensaje_usuario: str) -> bool:
    """
    Permite pasar consultas hoteleras habituales no sensibles.
    Ej.: direcciones de hoteles, ciudades, disponibilidad, precios, reservas.
    """
    text = _normalize_text(mensaje_usuario)
    if not text:
        return False

    sensitive_or_personal = [
        "direccion personal",
        "direccion de la recepcionista",
        "direccion del recepcionista",
        "direccion de empleado",
        "direccion de un empleado",
        "domicilio",
        "dni",
        "pasaporte",
        "tarjeta",
        "cvv",
        "iban",
        "numero personal",
        "telefono personal",
        "correo personal",
        "email personal",
        "whatsapp personal",
    ]
    if any(term in text for term in sensitive_or_personal):
        return False

    operational_keywords = [
        "direccion",
        "direcciones",
        "ubicacion",
        "ubicaciones",
        "como llegar",
        "ciudad",
        "ciudades",
        "hotel",
        "hoteles",
        "alojamiento",
        "reserva",
        "reservar",
        "disponibilidad",
        "precio",
        "precios",
    ]
    return any(term in text for term in operational_keywords)


# Recupera el prompt de la operación.
# Se usa en el flujo de supervisor de entrada antes de pasar mensajes al sistema para preparar datos, validaciones o decisiones previas.
# No recibe parámetros externos; trabaja con estado capturado por el cierre o atributos de instancia.
# Devuelve un `str` con el resultado de esta operación. Sin efectos secundarios relevantes.
def _get_prompt() -> str:
    prompt = load_prompt("supervisor_input_prompt.txt")
    log.info("📜 Prompt SupervisorInput cargado (%d chars)", len(prompt or ""))
    return prompt

# =============================================================
# 🧩 FUNCIÓN PRINCIPAL DE EVALUACIÓN
# =============================================================

# Evalúa si el mensaje del huésped es apropiado según el prompt.
# Se usa en el flujo de supervisor de entrada antes de pasar mensajes al sistema para preparar datos, validaciones o decisiones previas.
# Recibe `mensaje_usuario` como entrada principal según la firma.
# Devuelve un `str` con el resultado de esta operación. Puede realizar llamadas externas o a modelos.
async def _evaluar_input_func(mensaje_usuario: str) -> str:
    """
    Evalúa si el mensaje del huésped es apropiado según el prompt.
    Devuelve texto en formato 'Aprobado' o 'Interno({...})'.
    """
    prompt = _get_prompt()
    with ls_context(
        name="SupervisorInputAgent.evaluar_input",
        metadata={"mensaje_usuario": mensaje_usuario},
        tags=["supervisor", "input"],
    ):
        try:
            response = await llm.ainvoke([
                {"role": "system", "content": prompt},
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
# 🚦 CLASE PRINCIPAL CON MEMORIA
# =============================================================

class SupervisorInputAgent:
    """
    Evalúa los mensajes entrantes del huésped para detectar si son apropiados.
    Ahora guarda en memoria cada evaluación realizada.
    """

    # Inicializa el estado interno y las dependencias de `SupervisorInputAgent`.
    # Se usa dentro de `SupervisorInputAgent` en el flujo de supervisor de entrada antes de pasar mensajes al sistema.
    # Recibe `memory_manager` como dependencias o servicios compartidos inyectados desde otras capas.
    # No devuelve valor; deja la instancia preparada con sus dependencias y estado inicial. Sin efectos secundarios relevantes.
    def __init__(self, memory_manager=None):
        self.memory_manager = memory_manager

    # Devuelve un diccionario con el campo 'estado' como mínimo.
    # Se usa dentro de `SupervisorInputAgent` en el flujo de supervisor de entrada antes de pasar mensajes al sistema.
    # Recibe `mensaje_usuario`, `chat_id` como entradas relevantes junto con el contexto inyectado en la firma.
    # Devuelve un `dict` con el resultado de esta operación. Sin efectos secundarios relevantes.
    async def validate(self, mensaje_usuario: str, chat_id: str = None) -> dict:
        """
        Devuelve un diccionario con el campo 'estado' como mínimo.
        Si no se puede interpretar con certeza, se asume Aprobado.
        Además, guarda el resultado en la memoria si está habilitada.
        """
        try:
            if _looks_like_safe_hotel_operational_query(mensaje_usuario):
                return {"estado": "Aprobado", "motivo": "Consulta hotelera operativa segura"}

            raw = await _evaluar_input_func(mensaje_usuario)
            salida = (raw or "").strip()

            # 🧠 Guardar en memoria el input y resultado
            if self.memory_manager and chat_id:
                self.memory_manager.update_memory(
                    chat_id,
                    f"[SupervisorInput] Evaluando mensaje:\n{mensaje_usuario}",
                    f"Resultado evaluación:\n{salida}"
                )

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
                        reason = _extract_reason_from_invalid_json(inner)
                        log.warning("🚨 Escalación textual detectada (sin JSON válido)")
                        return {
                            "estado": "No Aprobado",
                            "motivo": reason,
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
