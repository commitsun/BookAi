"""
🔧 Interno Tool - Escalación y Gestión de Respuestas al Encargado
=================================================================
Define las herramientas LangChain usadas por el agente interno del hotel.
Se encarga de:
- Notificar al encargado por Telegram cuando hay una escalación.
- Generar borradores de respuesta profesionales y empáticos.
- Confirmar y enviar respuestas finales al huésped por WhatsApp.
"""

import logging
import re
import importlib
import requests
import asyncio
from datetime import datetime
from typing import Dict, Optional
from dataclasses import dataclass
from pydantic import BaseModel
from langchain_core.tools import tool
import html

# 🧩 Core imports
from core.escalation_db import save_escalation, update_escalation, get_latest_pending_escalation
from core.config import Settings as C, ModelConfig, ModelTier  # ✅ Config centralizada
from core.escalation_manager import get_escalation
from core.socket_manager import emit_event

log = logging.getLogger("InternoTool")

# =============================================================
# 🧠 ESTRUCTURAS DE DATOS GLOBALES
# =============================================================

@dataclass
class Escalation:
    escalation_id: str
    guest_chat_id: str
    guest_message: str
    escalation_type: str
    escalation_reason: str
    context: str
    timestamp: str
    property_id: Optional[str | int] = None
    draft_response: Optional[str] = None
    manager_confirmed: bool = False
    final_response: Optional[str] = None
    sent_to_guest: bool = False


ESCALATIONS_STORE: Dict[str, Escalation] = {}

# Se usa para evitar enviar múltiples plantillas al encargado por la misma escalación.
NOTIFIED_ESCALATIONS: Dict[str, str] = {}

# Gestor de memoria compartido (inyectado desde InternoAgent)
_MEMORY_MANAGER = None


def _fire_event(event: str, payload: dict, rooms: list[str] | None = None) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            import anyio
            if rooms:
                payload["rooms"] = rooms
            anyio.from_thread.run(emit_event, event, payload, rooms=rooms)
        except Exception:
            try:
                if rooms:
                    payload["rooms"] = rooms
                asyncio.run(emit_event(event, payload, rooms=rooms))
            except Exception:
                log.debug("No se pudo emitir evento %s desde hilo sync", event)
            return
        return
    if rooms:
        payload["rooms"] = rooms
    loop.create_task(emit_event(event, payload, rooms=rooms))


def set_memory_manager(memory_manager):
    """Permite que las tools guarden mensajes en la memoria global."""
    global _MEMORY_MANAGER
    _MEMORY_MANAGER = memory_manager


def _clean_chat_id(chat_id: str) -> str:
    if not chat_id:
        return ""
    return re.sub(r"\D", "", str(chat_id or "")).strip()


def _normalize_guest_chat_id(guest_chat_id: str) -> str:
    """Normaliza ids compuestos (instancia:telefono) al chat_id que usa Chatter."""
    raw = str(guest_chat_id or "").strip()
    if not raw:
        return ""
    if ":" in raw:
        tail = raw.split(":")[-1].strip()
        return _clean_chat_id(tail) or tail
    return _clean_chat_id(raw) or raw


def _sanitize_guest_text(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    lines = []
    for line in raw.splitlines():
        current = line.strip()
        current = re.sub(r"^\s*\d+\.\s*\[esc_[^\]]+\]\s*", "", current, flags=re.IGNORECASE)
        current = re.sub(r"^\s*\[esc_[^\]]+\]\s*", "", current, flags=re.IGNORECASE)
        current = re.sub(r"\s*\[esc_[^\]]+\]\s*", " ", current, flags=re.IGNORECASE)
        current = re.sub(r"\s{2,}", " ", current).strip()
        if current:
            lines.append(current)
    return "\n".join(lines).strip()


def _merge_escalation_text(previous: str, addition: str) -> str:
    prev = (previous or "").strip()
    add = (addition or "").strip()
    if not prev:
        return add
    if not add:
        return prev
    if add.lower() in prev.lower():
        return prev
    return f"{prev} {add}".strip()


def _synthesize_escalation_query(previous: str, addition: str) -> str:
    """Sintetiza en una sola consulta lo ya pendiente + la nueva ampliación."""
    prev = (previous or "").strip()
    add = (addition or "").strip()
    if not prev:
        return add
    if not add:
        return prev
    if add.lower() in prev.lower():
        return prev
    try:
        llm = ModelConfig.get_llm(ModelTier.INTERNAL)
        system_prompt = (
            "Eres asistente interno hotelero. Fusiona las consultas del huésped en UNA sola frase breve y clara, "
            "sin perder información relevante.\n"
            "Reglas:\n"
            "- Devuelve SOLO la frase final.\n"
            "- No uses listas ni numeración.\n"
            "- Mantén el idioma original (español).\n"
            "- Máximo 35 palabras."
        )
        user_prompt = (
            "Consulta previa:\n"
            f"{prev}\n\n"
            "Nueva ampliación:\n"
            f"{add}\n\n"
            "Consulta sintetizada:"
        )
        raw = llm.invoke(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
        )
        text = (getattr(raw, "content", None) or str(raw or "")).strip()
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            return text
    except Exception:
        pass
    return _merge_escalation_text(prev, add)


def _escalation_priority(escalation_type: str) -> int:
    t = (escalation_type or "").strip().lower()
    if t == "inappropriate":
        return 4
    if t == "bad_response":
        return 3
    if t == "info_not_found":
        return 2
    if t == "manual":
        return 1
    return 0


def _pick_escalation_type(existing_type: str, incoming_type: str) -> str:
    """Conserva el tipo más crítico entre el pendiente y el nuevo."""
    a = (existing_type or "").strip()
    b = (incoming_type or "").strip()
    if not a:
        return b or "manual"
    if not b:
        return a or "manual"
    return a if _escalation_priority(a) >= _escalation_priority(b) else b


def _resolve_property_id(guest_chat_id: str) -> Optional[str | int]:
    if not _MEMORY_MANAGER or not guest_chat_id:
        return None
    try:
        candidates = []
        raw = str(guest_chat_id).strip()
        if raw:
            candidates.append(raw)
        clean = _clean_chat_id(raw)
        if clean and clean not in candidates:
            candidates.append(clean)
        if ":" in raw:
            tail = raw.split(":")[-1].strip()
            if tail and tail not in candidates:
                candidates.append(tail)
        # Si existe un memory_id compuesto, lo probamos también.
        try:
            last_mem = _MEMORY_MANAGER.get_flag(raw, "last_memory_id") if raw else None
            if isinstance(last_mem, str) and last_mem and last_mem not in candidates:
                candidates.append(last_mem)
        except Exception:
            pass
        for cid in candidates:
            val = _MEMORY_MANAGER.get_flag(cid, "property_id")
            if val is not None:
                return val
        # Fallback usando helper del memory_manager si existe.
        try:
            get_hint = getattr(_MEMORY_MANAGER, "get_last_property_id_hint", None)
            if callable(get_hint):
                for cid in candidates:
                    hint = get_hint(cid)
                    if hint is not None:
                        try:
                            _MEMORY_MANAGER.set_flag(cid, "property_id", hint)
                        except Exception:
                            pass
                        return hint
        except Exception:
            pass
        # Fallback: busca en DB el último property_id registrado para el chat.
        try:
            from core.db import supabase
            for cid in candidates:
                if not cid:
                    continue
                clean = _clean_chat_id(cid) or cid
                resp = (
                    supabase.table("chat_history")
                    .select("property_id")
                    .eq("conversation_id", clean)
                    .order("created_at", desc=True)
                    .limit(1)
                    .execute()
                )
                rows = resp.data or []
                if rows:
                    prop_id = rows[0].get("property_id")
                    if prop_id is not None:
                        # cache en memoria si es posible
                        try:
                            _MEMORY_MANAGER.set_flag(cid, "property_id", prop_id)
                        except Exception:
                            pass
                        return prop_id
        except Exception:
            pass
    except Exception:
        return None
    return None


def _rooms_for_escalation(guest_chat_id: str) -> list[str]:
    clean_id = _normalize_guest_chat_id(guest_chat_id) or guest_chat_id
    rooms = [f"chat:{clean_id}", "channel:whatsapp"]
    prop_id = _resolve_property_id(guest_chat_id)
    if prop_id is not None:
        rooms.append(f"property:{prop_id}")
    return rooms

# =============================================================
# 📥 INPUT SCHEMAS
# =============================================================

class SendToEncargadoInput(BaseModel):
    escalation_id: str
    guest_chat_id: str
    guest_message: str
    escalation_type: str
    reason: str
    context: str


class GenerarBorradorInput(BaseModel):
    escalation_id: str
    manager_response: str


class ConfirmarYEnviarInput(BaseModel):
    escalation_id: str
    confirmed: bool
    adjustments: str = ""


# =============================================================
# 📨 TOOL 1: NOTIFICAR ENCARGADO (Telegram)
# =============================================================

def send_to_encargado(escalation_id, guest_chat_id, guest_message, escalation_type, reason, context) -> str:
    """Envía una notificación al encargado del hotel por Telegram."""
    try:
        # Evita notificaciones duplicadas cuando la misma escalación se dispara más de una vez.
        if escalation_id in NOTIFIED_ESCALATIONS:
            log.info("🔁 Escalación %s ya notificada; se omite reenvío.", escalation_id)
            return f"ℹ️ Escalación {escalation_id} ya fue notificada al encargado."

        # Marcamos como pendiente para prevenir carreras; se limpia en caso de fallo.
        NOTIFIED_ESCALATIONS[escalation_id] = "pending"

        clean_reason = (reason or "").strip()
        clean_context = (context or "").strip()
        generic_contexts = {
            "Escalación manual desde MainAgent (Hotel)",
            "Escalación automática",
            "Rechazado por Supervisor Input",
        }
        context_to_store = clean_context if clean_context and clean_context not in generic_contexts else clean_reason

        property_id = _resolve_property_id(guest_chat_id)
        # Evita escalaciones duplicadas en cascada para el mismo chat/property cuando falta info.
        existing_pending = get_latest_pending_escalation(guest_chat_id, property_id=property_id)
        existing_id = str(existing_pending.get("escalation_id") or "").strip() if existing_pending else ""
        existing_type = (
            (existing_pending.get("escalation_type") or existing_pending.get("type") or "").strip()
            if existing_pending
            else ""
        )
        if existing_id and existing_id != escalation_id:
            merged_type = _pick_escalation_type(existing_type, escalation_type)
            merged_guest_message = _synthesize_escalation_query(
                str(existing_pending.get("guest_message") or ""),
                guest_message,
            )
            merged_reason = _merge_escalation_text(
                str(existing_pending.get("escalation_reason") or existing_pending.get("reason") or ""),
                clean_reason,
            )
            merged_context = _merge_escalation_text(
                str(existing_pending.get("context") or ""),
                context_to_store,
            )
            update_escalation(
                existing_id,
                {
                    "guest_message": merged_guest_message,
                    "escalation_type": merged_type,
                    "escalation_reason": merged_reason,
                    "context": merged_context,
                    "timestamp": datetime.utcnow().isoformat(),
                    "property_id": property_id,
                },
            )
            try:
                existing = ESCALATIONS_STORE.get(existing_id)
                if existing:
                    existing.guest_message = merged_guest_message
                    existing.escalation_type = merged_type
                    existing.escalation_reason = merged_reason
                    existing.context = merged_context
                    existing.timestamp = datetime.utcnow().isoformat()
                    existing.property_id = property_id
            except Exception:
                pass
            if C.TELEGRAM_CHAT_ID and C.TELEGRAM_BOT_TOKEN:
                try:
                    update_msg = (
                        "🔁 <b>ACTUALIZACIÓN DE CONSULTA ESCALADA</b>\n"
                        f"🆔 <b>ID:</b> <code>{html.escape(existing_id)}</code>\n"
                        f"📱 <b>Chat ID:</b> <code>{html.escape(guest_chat_id)}</code>\n\n"
                        "➕ <b>Nueva ampliación del huésped:</b>\n"
                        f"{html.escape(guest_message)}\n\n"
                        "📌 <b>Consulta acumulada:</b>\n"
                        f"{html.escape(merged_guest_message)}"
                    )
                    requests.post(
                        f"https://api.telegram.org/bot{C.TELEGRAM_BOT_TOKEN}/sendMessage",
                        json={"chat_id": str(C.TELEGRAM_CHAT_ID), "text": update_msg, "parse_mode": "HTML"},
                        timeout=10,
                    )
                except Exception:
                    log.debug("No se pudo enviar actualización de escalación %s por Telegram", existing_id)
            try:
                clean_chat_id = _normalize_guest_chat_id(guest_chat_id) or guest_chat_id
                rooms = _rooms_for_escalation(guest_chat_id)
                _fire_event(
                    "escalation.updated",
                    {
                        "chat_id": clean_chat_id,
                        "escalation_id": existing_id,
                        "guest_message": merged_guest_message,
                        "escalation_type": escalation_type,
                        "escalation_reason": merged_reason,
                        "context": merged_context,
                        "property_id": property_id,
                    },
                    rooms=rooms,
                )
                _fire_event(
                    "chat.updated",
                    {
                        "chat_id": clean_chat_id,
                        "needs_action": merged_guest_message,
                        "needs_action_type": merged_type,
                        "needs_action_reason": merged_reason,
                        "proposed_response": (existing_pending.get("draft_response") or "").strip() or None,
                        "is_final_response": bool((existing_pending.get("draft_response") or "").strip()),
                        "escalation_id": existing_id,
                        "property_id": property_id,
                    },
                    rooms=rooms,
                )
            except Exception:
                log.debug("No se pudo emitir actualización realtime de escalación %s", existing_id)
            log.info(
                "♻️ Reutilizada escalación pendiente %s para chat=%s property_id=%s",
                existing_id,
                guest_chat_id,
                property_id,
            )
            return f"Escalación {existing_id} ya pendiente; actualizada con el último contexto."

        esc = Escalation(
            escalation_id=escalation_id,
            guest_chat_id=guest_chat_id,
            guest_message=guest_message,
            escalation_type=escalation_type,
            escalation_reason=clean_reason,
            context=context_to_store,
            timestamp=datetime.utcnow().isoformat(),
            property_id=property_id,
        )
        ESCALATIONS_STORE[escalation_id] = esc
        save_escalation(vars(esc))
        tipo_map = {
            "info_not_found": "ℹ️ Información No Disponible",
            "inappropriate": "🚨 Contenido Inapropiado",
            "bad_response": "⚠️ Respuesta Incorrecta",
            "manual": "📎 Escalación Manual",
        }

        msg = (
            "🔔 <b>NUEVA CONSULTA ESCALADA</b>\n"
            f"🆔 <b>ID:</b> <code>{html.escape(escalation_id)}</code>\n"
            f"📱 <b>Chat ID:</b> <code>{html.escape(guest_chat_id)}</code>\n"
            f"🏷️ <b>Tipo:</b> {html.escape(tipo_map.get(escalation_type, escalation_type))}\n\n"
            "❓ <b>Mensaje del huésped:</b>\n"
            f"{html.escape(guest_message)}\n\n"
            "📝 <b>Razón:</b>\n"
            f"{html.escape(reason)}\n\n"
            "💭 <b>Contexto:</b>\n"
            f"{html.escape(context)}\n\n"
            f"⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n"
            "➡️ Responde a este mensaje (Reply). El sistema generará un borrador automáticamente."
        )

        if not C.TELEGRAM_CHAT_ID or not C.TELEGRAM_BOT_TOKEN:
            NOTIFIED_ESCALATIONS.pop(escalation_id, None)
            return "⚠️ No se pudo enviar la notificación: faltan credenciales de Telegram."

        r = requests.post(
            f"https://api.telegram.org/bot{C.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": str(C.TELEGRAM_CHAT_ID), "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )

        if r.status_code == 200:
            data = r.json()
            sent_message_id = str(data.get("result", {}).get("message_id", ""))

            if sent_message_id:
                try:
                    from core.escalation_manager import register_escalation
                    register_escalation(sent_message_id, escalation_id)
                    log.info(f"📎 Registrado message_id={sent_message_id} → escalación={escalation_id}")
                except Exception as e:
                    log.warning(f"⚠️ No se pudo registrar message_id → {e}")

            NOTIFIED_ESCALATIONS[escalation_id] = sent_message_id or "sent"
            log.info(f"✅ Escalación {escalation_id} enviada correctamente al encargado.")

            # Emisión en tiempo real se realiza en InternoAgent.escalate()

            return f"Escalación {escalation_id} notificada al encargado con éxito."

        NOTIFIED_ESCALATIONS.pop(escalation_id, None)
        return f"❌ Error al notificar al encargado: {r.text}"

    except Exception as e:
        NOTIFIED_ESCALATIONS.pop(escalation_id, None)
        log.exception("Error notificando al encargado")
        return f"Error notificando al encargado: {e}"


# =============================================================
# 🧠 TOOL 2: GENERAR BORRADOR DE RESPUESTA
# =============================================================

def generar_borrador(escalation_id: str, manager_response: str, adjustment: Optional[str] = None) -> str:
    """Genera o reformula un borrador empático y profesional para el huésped."""
    if escalation_id not in ESCALATIONS_STORE:
        return f"Error: Escalación {escalation_id} no encontrada."

    esc = ESCALATIONS_STORE[escalation_id]

    # ✅ Usa configuración centralizada para el modelo del agente interno
    llm = ModelConfig.get_llm(ModelTier.INTERNAL)

    system_prompt = (
        "Eres un asistente especializado en atención hotelera.\n"
        "Tu tarea es reformular el mensaje del encargado para el huésped con un tono cálido, empático y profesional.\n"
        "Usa SIEMPRE el idioma del huésped (el mismo idioma que su mensaje).\n"
        "No incluyas encabezados, comillas ni explicaciones, solo el texto final que se enviará al cliente.\n"
        "Si se proporcionan 'ajustes', incorpóralos en el tono o contenido."
    )

    user_prompt = (
        f"Mensaje original del huésped:\n{esc.guest_message}\n\n"
        f"Respuesta del encargado:\n{manager_response}\n"
    )

    if adjustment:
        user_prompt += f"\nInstrucciones de ajuste del encargado:\n{adjustment}\n"

    user_prompt += "\nReformula la respuesta final para el huésped siguiendo esas pautas."

    try:
        response = llm.invoke([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])
        draft = (response.content or "").strip()
        draft = re.sub(r'^[\"\'“”]+|[\"\'“”]+$', '', draft).strip()

        esc.draft_response = draft
        update_escalation(escalation_id, {"draft_response": draft})

        rooms = _rooms_for_escalation(esc.guest_chat_id)
        clean_chat_id = _normalize_guest_chat_id(esc.guest_chat_id) or esc.guest_chat_id
        _fire_event(
            "escalation.updated",
            {
                "chat_id": clean_chat_id,
                "escalation_id": escalation_id,
                "draft_response": draft,
                "property_id": _resolve_property_id(esc.guest_chat_id),
            },
            rooms=rooms,
        )
        _fire_event(
            "chat.proposed_response.updated",
            {
                "chat_id": clean_chat_id,
                "proposed_response": draft,
                "property_id": _resolve_property_id(esc.guest_chat_id),
            },
            rooms=rooms,
        )

        formatted = (
            f"📝 *BORRADOR DE RESPUESTA PROPUESTO:*\n\n"
            f"{draft}\n\n"
            "✏️ Si deseas modificar el texto, escribe tus ajustes directamente.\n"
            "✅ Si estás conforme, responde con 'OK' para enviarlo al huésped."
        )
        return formatted

    except Exception as e:
        log.exception("Error generando borrador")
        return f"Error generando borrador: {e}"


# =============================================================
# 📤 TOOL 3: CONFIRMAR Y ENVIAR RESPUESTA FINAL
# =============================================================

async def confirmar_y_enviar(escalation_id: str, confirmed: bool, adjustments: str = "") -> str:
    """Confirma o reformula según el input del encargado y envía si corresponde."""
    if escalation_id not in ESCALATIONS_STORE:
        return f"Error: Escalación {escalation_id} no encontrada."

    esc = ESCALATIONS_STORE[escalation_id]

    # 🔁 Caso 1: ajustes → reformular nuevo borrador
    if not confirmed and adjustments:
        new_draft = generar_borrador(escalation_id, esc.draft_response or "", adjustment=adjustments)

        clean_draft = new_draft
        for marker in [
            "📝 *BORRADOR DE RESPUESTA PROPUESTO:*",
            "✏️ Si deseas modificar",
            "✅ Si estás conforme",
            "📝 *Nuevo borrador generado",
        ]:
            clean_draft = clean_draft.replace(marker, "").strip()

        formatted = (
            "📝 *Nuevo borrador generado según tus ajustes:*\n\n"
            f"{clean_draft.strip()}\n\n"
            "✏️ Si deseas más cambios, vuelve a escribirlos.\n"
            "✅ Si estás conforme, responde con 'OK' para enviarlo al huésped."
        )
        return formatted

    # ✅ Caso 2: confirmado → envío final
    if confirmed:
        final_text = _sanitize_guest_text(esc.draft_response or adjustments or "")
        if not final_text:
            return "⚠️ No hay texto final disponible para enviar."

        try:
            ChannelManager = importlib.import_module("channels_wrapper.manager").ChannelManager
            cm = ChannelManager(memory_manager=_MEMORY_MANAGER)
            await cm.send_message(esc.guest_chat_id, final_text, channel="whatsapp")

            # Guarda el mensaje real que vio el huésped en la memoria compartida.
            try:
                if _MEMORY_MANAGER:
                    _MEMORY_MANAGER.save(
                        esc.guest_chat_id,
                        "assistant",
                        final_text,
                    )
            except Exception as mem_exc:
                log.warning("⚠️ No se pudo guardar en memoria el envío final: %s", mem_exc)

            esc.final_response = final_text
            esc.manager_confirmed = True
            esc.sent_to_guest = True
            update_escalation(escalation_id, {
                "final_response": final_text,
                "manager_confirmed": True,
                "sent_to_guest": True,
            })

            clean_chat_id = _normalize_guest_chat_id(esc.guest_chat_id) or esc.guest_chat_id
            rooms = _rooms_for_escalation(esc.guest_chat_id)
            _fire_event(
                "escalation.resolved",
                {
                    "chat_id": clean_chat_id,
                    "escalation_id": escalation_id,
                    "final_response": final_text,
                    "property_id": _resolve_property_id(esc.guest_chat_id),
                },
                rooms=rooms,
            )
            _fire_event(
                "chat.message.created",
                {
                    "chat_id": clean_chat_id,
                    "property_id": _resolve_property_id(esc.guest_chat_id),
                    "channel": "whatsapp",
                    "sender": "bookai",
                    "message": final_text,
                    "created_at": datetime.utcnow().isoformat(),
                },
                rooms=rooms,
            )
            _fire_event(
                "chat.updated",
                {
                    "chat_id": clean_chat_id,
                    "last_message": final_text,
                    "last_message_at": datetime.utcnow().isoformat(),
                    "needs_action": None,
                    "needs_action_type": None,
                    "needs_action_reason": None,
                    "proposed_response": None,
                    "property_id": _resolve_property_id(esc.guest_chat_id),
                },
                rooms=rooms,
            )

            return f"✅ *Respuesta enviada al huésped:*\n\n{final_text}"

        except Exception as e:
            log.exception("Error enviando respuesta final")
            return f"Error enviando respuesta: {e}"

    return "❌ Borrador rechazado. Esperando nueva versión."


# =============================================================
# 🧩 REGISTRO DE TOOLS
# =============================================================

@tool("notificar_encargado", args_schema=SendToEncargadoInput, return_direct=False)
def notificar_encargado_tool(**kwargs) -> str:
    """Tool que notifica al encargado del hotel sobre una nueva escalación por Telegram."""
    return send_to_encargado(**kwargs)


@tool("generar_borrador_respuesta", args_schema=GenerarBorradorInput, return_direct=True)
def generar_borrador_tool(**kwargs) -> str:
    """Tool que genera un borrador empático y profesional para el huésped a partir de la respuesta del encargado."""
    return generar_borrador(**kwargs)


@tool("confirmar_y_enviar_respuesta", args_schema=ConfirmarYEnviarInput, return_direct=True)
async def confirmar_y_enviar_tool(**kwargs) -> str:
    """Tool que confirma o ajusta la respuesta y la envía al huésped por WhatsApp."""
    return await confirmar_y_enviar(**kwargs)


def create_interno_tools(memory_manager=None):
    """Devuelve la lista de herramientas disponibles para el agente interno."""
    set_memory_manager(memory_manager)
    return [
        notificar_encargado_tool,
        generar_borrador_tool,
        confirmar_y_enviar_tool,
    ]
