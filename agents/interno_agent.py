"""
🤖 Interno Agent v4 - Agente Reactivo (versión limpia y optimizada)
==================================================================
Gestiona el flujo interno de escalaciones entre huésped y encargado.
Incluye soporte para ajustes iterativos (reformulación de respuesta).
"""

import logging
from datetime import datetime
from langchain_openai import ChatOpenAI
from langchain.agents import create_openai_tools_agent, AgentExecutor
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder

from tools.interno_tool import create_interno_tools, ESCALATIONS_STORE

log = logging.getLogger("InternoAgent")


# =============================================================
# 🧠 CREACIÓN DEL AGENTE REACTIVO
# =============================================================

def create_interno_agent():
    """Crea el agente interno con herramientas y modelo LLM."""
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.2)

    try:
        with open("prompts/interno_prompt.txt", "r", encoding="utf-8") as f:
            interno_prompt = f.read()
    except FileNotFoundError:
        interno_prompt = (
            "Eres el agente interno del hotel. Gestionas escalaciones entre huésped y encargado."
        )

    tools = create_interno_tools()

    prompt = ChatPromptTemplate.from_messages([
        ("system", interno_prompt),
        MessagesPlaceholder("chat_history", optional=True),
        ("user", "{input}"),
        MessagesPlaceholder("agent_scratchpad"),
    ])

    agent = create_openai_tools_agent(llm, tools, prompt)
    return AgentExecutor(agent=agent, tools=tools, verbose=True)


# =============================================================
# 🤖 CLASE PRINCIPAL DEL AGENTE INTERNO
# =============================================================

class InternoAgent:
    """Orquesta el flujo entre el encargado (Telegram) y el huésped (WhatsApp)."""

    def __init__(self):
        self.executor = create_interno_agent()
        self.escalations = ESCALATIONS_STORE

    # =========================================================
    # 🔺 1️⃣ Crear nueva escalación
    # =========================================================
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
        return (result.get("output") or str(result)).strip()

    # =========================================================
    # 🧾 2️⃣ Procesar respuesta del encargado → generar borrador
    # =========================================================
    async def process_manager_reply(self, escalation_id, manager_reply):
        """
        Procesa la respuesta del encargado (por Telegram):
        - Si es la primera vez, genera el borrador inicial.
        - Si es un ajuste posterior, reformula con el prompt empático.
        """
        manager_reply_clean = manager_reply.strip().lower()

        # 🧠 Si ya hay borrador → interpretar como ajustes (salvo que sea 'ok')
        if escalation_id in self.escalations and self.escalations[escalation_id].draft_response:
            if "ok" not in manager_reply_clean and "confirm" not in manager_reply_clean:
                user_input = f"""
El encargado ha pedido ajustes al borrador de la escalación {escalation_id}.
Ajustes solicitados: "{manager_reply}"

Usa la tool 'confirmar_y_enviar_respuesta' con confirmed=False y adjustments="{manager_reply}".
"""
                result = await self.executor.ainvoke({"input": user_input, "chat_history": []})
                output = (result.get("output") or "").strip()
                log.info(f"🧾 Nuevo borrador ajustado para {escalation_id}: {output[:100]}...")
                return output

        # 🆕 Si no había borrador previo → generar uno nuevo
        user_input = f"""
El encargado respondió a la escalación {escalation_id}:
\"{manager_reply}\"
Usa la tool 'generar_borrador_respuesta'.
"""
        result = await self.executor.ainvoke({"input": user_input, "chat_history": []})
        output = (result.get("output") or "").strip()
        log.info(f"🧾 Borrador inicial generado para {escalation_id}: {output[:100]}...")
        return output

    # =========================================================
    # ✅ 3️⃣ Confirmar y enviar respuesta final al huésped
    # =========================================================
    async def send_confirmed_response(self, escalation_id, confirmed=True, adjustments=""):
        """
        Maneja la confirmación o ajustes finales del encargado.
        - Si confirmed=True → se envía al huésped por WhatsApp.
        - Si confirmed=False con texto → se reformula el borrador.
        """
        user_input = f"""
Confirmación para la escalación {escalation_id}:
- Confirmado: {confirmed}
- Ajustes: {adjustments}

Usa la tool 'confirmar_y_enviar_respuesta'.
"""
        result = await self.executor.ainvoke({"input": user_input, "chat_history": []})
        output = (result.get("output") or "").strip()
        log.info(f"📤 Respuesta final procesada para {escalation_id}: {output[:100]}...")
        return output
