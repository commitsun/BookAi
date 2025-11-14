"""
🤖 InternoAgent v6 — Agente Reactivo con Memoria y Prompt Dinámico
Gestiona el flujo de escalaciones huésped ↔ encargado.
"""

import asyncio
import logging
from typing import Optional
from datetime import datetime
from langchain.agents import create_openai_tools_agent, AgentExecutor
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder

# Tools y utilidades
from tools.interno_tool import create_interno_tools, ESCALATIONS_STORE, Escalation
from core.utils.utils_prompt import load_prompt
from core.utils.time_context import get_time_context
from core.config import ModelConfig, ModelTier  # ✅ Configuración centralizada

log = logging.getLogger("InternoAgent")


# =============================================================
# 🧩 Creación del agente interno
# =============================================================
def create_interno_agent():
    """Crea el agente interno usando el prompt de utils_prompt y modelo centralizado."""
    llm = ModelConfig.get_llm(ModelTier.INTERNAL)

    base_prompt = load_prompt("interno_prompt.txt") or (
        "Eres el agente interno del hotel. Gestionas escalaciones entre huésped y encargado."
    )

    final_prompt = f"{get_time_context()}\n\n{base_prompt.strip()}"

    tools = create_interno_tools()

    prompt = ChatPromptTemplate.from_messages([
        ("system", final_prompt),
        MessagesPlaceholder("chat_history", optional=True),
        ("user", "{input}"),
        MessagesPlaceholder("agent_scratchpad"),
    ])

    agent = create_openai_tools_agent(llm, tools, prompt)
    executor = AgentExecutor(agent=agent, tools=tools, verbose=True)

    log.info("🧩 InternoAgent inicializado correctamente (modelo gpt-4.1 centralizado).")
    return executor


# =============================================================
# 🧠 Clase principal InternoAgent
# =============================================================
class InternoAgent:
    """Orquesta el flujo entre encargado y huésped, con memoria persistente."""

    def __init__(self, memory_manager=None):
        self.executor = create_interno_agent()
        self.escalations = ESCALATIONS_STORE
        self.memory_manager = memory_manager

    # ----------------------------------------------------------
    async def _clear_escalation_flag_later(self, chat_id: str, delay: int = 90):
        await asyncio.sleep(delay)
        if not self.memory_manager:
            return
        try:
            self.memory_manager.clear_flag(chat_id, "escalation_in_progress")
            log.info(f"🧹 Escalation flag limpiado para {chat_id}")
        except Exception as exc:
            log.warning(f"⚠️ No se pudo limpiar el flag de escalación para {chat_id}: {exc}")

    def _schedule_flag_cleanup(self, chat_id: str, delay: int = 90):
        if not self.memory_manager:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._clear_escalation_flag_later(chat_id, delay))
        except RuntimeError:
            log.debug("No hay loop en ejecución para programar la limpieza del flag de escalación.")

    # ----------------------------------------------------------
    async def escalate(self, guest_chat_id, guest_message, escalation_type, reason, context=""):
        """Crea una nueva escalación hacia el encargado."""
        escalation_id = f"esc_{guest_chat_id}_{int(datetime.utcnow().timestamp())}"

        user_input = f"""
Nueva escalación:
- ID: {escalation_id}
- Chat ID: {guest_chat_id}
- Tipo: {escalation_type}
- Mensaje: {guest_message}
- Razón: {reason}
- Contexto: {context}

Usa la tool 'notificar_encargado' con estos datos.
"""

        result = await self.executor.ainvoke({"input": user_input, "chat_history": []})
        output = (result.get("output") or str(result)).strip()

        if self.memory_manager:
            self.memory_manager.update_memory(
                guest_chat_id,
                role="system",
                content=(
                    f"[InternoAgent] Nueva escalación ({escalation_id}): {reason}\n"
                    f"Mensaje huésped: {guest_message}\nContexto: {context}\nSalida: {output}"
                )
            )

        log.info(f"📢 Escalación creada {escalation_id} → {guest_chat_id}")
        return output

    # ----------------------------------------------------------
    async def handle_guest_escalation(
        self,
        *,
        chat_id: str,
        guest_message: str,
        reason: str,
        escalation_type: str = "info_not_found",
        context: str = "",
        confirmation_flag: Optional[str] = None,
    ):
        """Gestiona la escalación completa en nombre del MainAgent."""

        context_value = context.strip() if context else "Auto-escalación gestionada por InternoAgent"

        if self.memory_manager:
            try:
                self.memory_manager.set_flag(chat_id, "escalation_in_progress", True)
                if confirmation_flag:
                    self.memory_manager.clear_flag(chat_id, confirmation_flag)
            except Exception as exc:
                log.warning(f"⚠️ No se pudieron actualizar los flags de escalación para {chat_id}: {exc}")

        log.warning(f"🚨 InternoAgent gestionando escalación ({chat_id}) → motivo: {reason}")

        try:
            output = await self.escalate(
                guest_chat_id=chat_id,
                guest_message=guest_message,
                escalation_type=escalation_type,
                reason=reason,
                context=context_value,
            )
        except Exception:
            if self.memory_manager:
                try:
                    self.memory_manager.clear_flag(chat_id, "escalation_in_progress")
                except Exception as exc:
                    log.warning(f"⚠️ Falló la limpieza del flag tras error ({chat_id}): {exc}")
            raise

        self._schedule_flag_cleanup(chat_id)
        return output

    # ----------------------------------------------------------
    async def process_manager_reply(self, escalation_id, manager_reply):
        """Procesa respuesta del encargado (Telegram) → generar o ajustar borrador."""
        manager_reply_clean = manager_reply.strip().lower()
        guest_chat_id = self._get_chat_from_escalation(escalation_id)

        if self.memory_manager and guest_chat_id:
            self.memory_manager.update_memory(
                guest_chat_id,
                role="system",
                content=f"[InternoAgent] Encargado respondió (escalación {escalation_id}): {manager_reply}"
            )

        # 🚨 Caso: encargado pide ajustes
        if escalation_id in self.escalations and self.escalations[escalation_id].draft_response:
            if "ok" not in manager_reply_clean and "confirm" not in manager_reply_clean:
                user_input = f"""
El encargado ha pedido ajustes al borrador de la escalación {escalation_id}.
Ajustes solicitados: "{manager_reply}"

Usa la tool 'confirmar_y_enviar_respuesta' con confirmed=False y adjustments="{manager_reply}".
"""
                result = await self.executor.ainvoke({"input": user_input, "chat_history": []})
                output = (result.get("output") or "").strip()
                return output

        # Nuevo borrador
        user_input = f"""
El encargado ha enviado una respuesta para {escalation_id}.
Respuesta: "{manager_reply}"

Usa la tool 'generar_borrador_respuesta'.
"""

        result = await self.executor.ainvoke({"input": user_input, "chat_history": []})
        output = (result.get("output") or "").strip()

        if self.memory_manager and guest_chat_id:
            self.memory_manager.update_memory(
                guest_chat_id,
                role="system",
                content=f"[InternoAgent] Nuevo borrador generado ({escalation_id}): {output}"
            )

        return output

    # ----------------------------------------------------------
    async def send_confirmed_response(self, escalation_id, confirmed=True, adjustments=""):
        """Envía o confirma una respuesta final al huésped."""
        user_input = f"""
Confirmación para la escalación {escalation_id}:
- Confirmado: {confirmed}
- Ajustes: {adjustments}

Usa la tool 'confirmar_y_enviar_respuesta'.
"""

        result = await self.executor.ainvoke({"input": user_input, "chat_history": []})
        output = (result.get("output") or "").strip()

        guest_chat_id = self._resolve_guest_chat_id(escalation_id)

        if self.memory_manager and guest_chat_id:
            self.memory_manager.update_memory(
                guest_chat_id,
                role="system",
                content=f"[InternoAgent] Confirmación final ({escalation_id}) → confirmed={confirmed}\n{output}"
            )

        return output

    # ----------------------------------------------------------
    def _resolve_guest_chat_id(self, escalation_id: str) -> Optional[str]:
        """Obtiene el chat_id asociado a una escalación, con fallback a BD."""
        esc = self.escalations.get(escalation_id)
        if esc:
            return getattr(esc, "chat_id", None) or getattr(esc, "guest_chat_id", None)

        try:
            from core.escalation_db import get_escalation as fetch_escalation

            record = fetch_escalation(escalation_id) or {}
            chat_id = record.get("guest_chat_id") or record.get("chat_id")

            if chat_id:
                self.escalations[escalation_id] = Escalation(
                    escalation_id=escalation_id,
                    guest_chat_id=chat_id,
                    guest_message=record.get("guest_message", ""),
                    escalation_type=record.get("escalation_type", ""),
                    escalation_reason=record.get("escalation_reason", ""),
                    context=record.get("context", ""),
                    timestamp=record.get("timestamp", ""),
                    draft_response=record.get("draft_response"),
                    manager_confirmed=record.get("manager_confirmed", False),
                    final_response=record.get("final_response"),
                    sent_to_guest=record.get("sent_to_guest", False),
                )
            return chat_id

        except Exception as exc:
            log.warning(f"⚠️ No se pudo recuperar el chat_id para escalación {escalation_id}: {exc}")

        return None

    # ----------------------------------------------------------
    # Alias retrocompatible
    def _get_chat_from_escalation(self, escalation_id: str):
        return self._resolve_guest_chat_id(escalation_id)
