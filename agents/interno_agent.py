"""
🤖 InternoAgent v6 — Agente Reactivo con Memoria y Prompt Dinámico
Gestiona el flujo de escalaciones huésped ↔ encargado.
"""

import logging
from datetime import datetime
from langchain_openai import ChatOpenAI
from langchain.agents import create_openai_tools_agent, AgentExecutor
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from tools.interno_tool import create_interno_tools, ESCALATIONS_STORE
from core.utils.utils_prompt import load_prompt  # ✅ nuevo import
from core.utils.time_context import get_time_context  # para contexto temporal

log = logging.getLogger("InternoAgent")


def create_interno_agent():
    """Crea el agente interno usando el prompt de utils_prompt."""
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.2)

    # ✅ carga del prompt desde core.utils.utils_prompt
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
    return AgentExecutor(agent=agent, tools=tools, verbose=True)


class InternoAgent:
    """Orquesta el flujo entre encargado y huésped, con memoria persistente."""

    def __init__(self, memory_manager=None):
        self.executor = create_interno_agent()
        self.escalations = ESCALATIONS_STORE
        self.memory_manager = memory_manager

    async def escalate(self, guest_chat_id, guest_message, escalation_type, reason, context=""):
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
                f"[InternoAgent] Nueva escalación ({escalation_id}): {reason}",
                f"Mensaje huésped: {guest_message}\nContexto: {context}\nSalida: {output}"
            )

        log.info(f"📢 Escalación creada {escalation_id} → {guest_chat_id}")
        return output

    async def process_manager_reply(self, escalation_id, manager_reply):
        """Procesa respuesta del encargado (Telegram) → generar o ajustar borrador."""
        manager_reply_clean = manager_reply.strip().lower()
        guest_chat_id = self._get_chat_from_escalation(escalation_id)

        if self.memory_manager and guest_chat_id:
            self.memory_manager.update_memory(
                guest_chat_id,
                f"[InternoAgent] Encargado respondió (escalación {escalation_id})",
                manager_reply
            )

        if escalation_id in self.escalations and self.escalations[escalation_id].draft_response:
            if "ok" not in manager_reply_clean and "confirm" not in manager_reply_clean:
                user_input = f"""
El encargado ha pedido ajustes al borrador de la escalación {escalation_id}.
Ajustes solicitados: "{manager_reply}"

Usa la tool 'confirmar_y_enviar_respuesta' con confirmed=False y adjustments="{manager_reply}".
"""
                result = await self.executor.ainvoke({"input": user_input, "chat_history": []})
                output = (result.get("output") or "").strip()
                if self.memory_manager and guest_chat_id:
                    self.memory_manager.update_memory(
                        guest_chat_id,
                        f"[InternoAgent] Ajustes solicitados ({escalation_id})",
                        output
                    )
                return output

        user_input = f"""
El encargado respondió a la escalación {escalation_id}:
\"{manager_reply}\"
Usa la tool 'generar_borrador_respuesta'.
"""
        result = await self.executor.ainvoke({"input": user_input, "chat_history": []})
        output = (result.get("output") or "").strip()
        if self.memory_manager and guest_chat_id:
            self.memory_manager.update_memory(
                guest_chat_id,
                f"[InternoAgent] Nuevo borrador generado ({escalation_id})",
                output
            )
        return output

    async def send_confirmed_response(self, escalation_id, confirmed=True, adjustments=""):
        user_input = f"""
Confirmación para la escalación {escalation_id}:
- Confirmado: {confirmed}
- Ajustes: {adjustments}

Usa la tool 'confirmar_y_enviar_respuesta'.
"""
        result = await self.executor.ainvoke({"input": user_input, "chat_history": []})
        output = (result.get("output") or "").strip()

        guest_chat_id = self._get_chat_from_escalation(escalation_id)
        if self.memory_manager and guest_chat_id:
            self.memory_manager.update_memory(
                guest_chat_id,
                f"[InternoAgent] Confirmación final ({escalation_id}) → confirmed={confirmed}",
                output
            )
        return output

    def _get_chat_from_escalation(self, escalation_id: str):
        esc = self.escalations.get(escalation_id)
        if not esc:
            return None
        return getattr(esc, "chat_id", None) or getattr(esc, "guest_chat_id", None)
