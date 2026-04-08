"""
🤖 Main Agent - Orquestador Principal (v6.0 - Arquitectura con Sub-Agentes + Flags anti-loop)
======================================================================================
- Evita bucles infinitos de Inciso.
- Sincroniza correctamente memoria entre herramientas.
- Integra sub-agentes: disponibilidad/precios, información general, e interno.
"""

import logging
import asyncio
import unicodedata
import re
import json
from typing import Optional, List, Callable

from langchain.agents import create_openai_tools_agent, AgentExecutor
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.tools import BaseTool

# Tools bases
from tools.think_tool import create_think_tool
from tools.inciso_tool import create_inciso_tool
from tools.sub_agent_tool_wrapper import create_sub_agent_tool
from tools.property_context_tool import create_property_context_tool

# Sub-agentes
from agents.dispo_precios_agent import DispoPreciosAgent
from agents.info_agent import InfoAgent
from agents.interno_agent import InternoAgent
from agents.onboarding_agent import OnboardingAgent

# Utilidades
from core.utils.utils_prompt import load_prompt
from core.utils.time_context import get_time_context
from core.utils.dynamic_context import build_dynamic_context_from_memory
from core.memory_manager import MemoryManager
from core.config import ModelConfig, ModelTier
from core.instance_context import (
    DEFAULT_PROPERTY_TABLE,
    fetch_property_by_id,
)
from core.db import get_active_chat_reservation
from core.language_manager import language_manager


log = logging.getLogger("MainAgent")

FLAG_ESCALATION_CONFIRMATION_PENDING = "escalation_confirmation_pending"
NO_GUEST_REPLY = "__NO_GUEST_REPLY__"


class MainAgent:
    """Agente principal que orquesta todas las operaciones del sistema."""

    def __init__(
        self,
        memory_manager: Optional[MemoryManager] = None,
        send_message_callback: Optional[Callable] = None,
        interno_agent: Optional[InternoAgent] = None,
    ):
        self.llm = ModelConfig.get_llm(ModelTier.MAIN)
        self.memory_manager = memory_manager
        self.send_callback = send_message_callback
        self.interno_agent = interno_agent
        self.locks = {}

        base_prompt = load_prompt("main_prompt.txt") or self._get_default_prompt()
        self.system_prompt = f"{get_time_context()}\n\n{base_prompt.strip()}"

        log.info("✅ MainAgent inicializado (GPT-4.1 + arquitectura modular + flags persistentes)")

    def _get_default_prompt(self) -> str:
        return (
            "Eres el agente principal de un sistema de IA para hoteles.\n\n"
            "Tu responsabilidad es ORQUESTAR: decidir qué herramienta usar según la consulta del usuario.\n\n"
            "HERRAMIENTAS DISPONIBLES:\n"
            "1. Think → consultas complejas.\n"
            "2. disponibilidad_precios → precios y disponibilidad.\n"
            "3. base_conocimientos → servicios, políticas, info general.\n"
            "4. Inciso → mensajes intermedios.\n"
            "5. identificar_property → fija el contexto de la propiedad.\n"
            "6. escalar_interno → escalar al encargado humano.\n\n"
            "NO generes respuestas por tu cuenta. SOLO invoca tools."
        )

    def build_tools(self, chat_id: str, hotel_name: str) -> List[BaseTool]:
        tools: List[BaseTool] = []

        tools.append(create_think_tool(model_name="gpt-4.1"))
        tools.append(create_inciso_tool(send_callback=self.send_callback))
        tools.append(create_property_context_tool(memory_manager=self.memory_manager, chat_id=chat_id))

        dispo_agent = DispoPreciosAgent(memory_manager=self.memory_manager)
        tools.append(
            create_sub_agent_tool(
                name="disponibilidad_precios",
                description=(
                    "Consulta disponibilidad, tipos de habitaciones y precios. "
                    "Úsala para fechas, tarifas y tipos de habitación."
                ),
                sub_agent=dispo_agent,
                memory_manager=self.memory_manager,
                chat_id=chat_id,
                hotel_name=hotel_name,
            )
        )

        info_agent = InfoAgent(memory_manager=self.memory_manager)
        tools.append(
            create_sub_agent_tool(
                name="base_conocimientos",
                description=(
                    "Busca información factual del hotel. Intenta primero la base de conocimientos y, "
                    "si no hay datos, recurre a Google antes de escalar."
                ),
                sub_agent=info_agent,
                memory_manager=self.memory_manager,
                chat_id=chat_id,
                hotel_name=hotel_name,
            )
        )

        onboarding_agent = OnboardingAgent(memory_manager=self.memory_manager)
        tools.append(
            create_sub_agent_tool(
                name="onboarding_reservas",
                description=(
                    "Gestiona reservas completas: obtiene token, identifica roomTypeId, crea la reserva "
                    "y consulta reservas propias del huésped. Úsala cuando el huésped quiera confirmar "
                    "una reserva con datos concretos o revisar su reserva."
                ),
                sub_agent=onboarding_agent,
                memory_manager=self.memory_manager,
                chat_id=chat_id,
                hotel_name=hotel_name,
            )
        )

        if self.interno_agent:
            tools.append(
                create_sub_agent_tool(
                    name="escalar_interno",
                    description=(
                        "Escala la conversación al encargado humano. Úsala cuando falte información, "
                        "cuando el huésped lo pida o cuando sea necesaria confirmación humana."
                    ),
                    sub_agent=self.interno_agent,
                    memory_manager=self.memory_manager,
                    chat_id=chat_id,
                    hotel_name=hotel_name,
                )
            )

        log.info("🔧 Tools cargadas para %s: %s", chat_id, [t.name for t in tools])
        return tools

    def create_prompt_template(self) -> ChatPromptTemplate:
        return ChatPromptTemplate.from_messages([
            ("system", self.system_prompt),
            MessagesPlaceholder(variable_name="chat_history", optional=True),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])

    async def _handle_pending_confirmation(self, chat_id: str, user_input: str) -> Optional[str]:
        if not self.memory_manager:
            return None

        pending = self.memory_manager.get_flag(chat_id, FLAG_ESCALATION_CONFIRMATION_PENDING)
        if not pending:
            return None

        decision = self._interpret_confirmation(user_input)

        if decision is True:
            motivo = pending.get("reason") or "Solicitud del huésped"
            escalation_type = pending.get("escalation_type", "info_not_found")
            original_message = pending.get("guest_message") or user_input
            self.memory_manager.clear_flag(chat_id, FLAG_ESCALATION_CONFIRMATION_PENDING)

            await self._delegate_escalation_to_interno(
                user_input=original_message,
                chat_id=chat_id,
                motivo=motivo,
                escalation_type=escalation_type,
                context="Escalación confirmada por el huésped",
            )
            return NO_GUEST_REPLY

        if decision is False:
            self.memory_manager.clear_flag(chat_id, FLAG_ESCALATION_CONFIRMATION_PENDING)
            self.memory_manager.clear_flag(chat_id, "consulta_base_realizada")
            reply = self._generate_reply(chat_id=chat_id, intent="escalation_declined")
            text = reply or (
                "Perfecto, seguimos buscando alternativas sin consultarlo por ahora. "
                + (
                    "Si quiere que lo consulte después, solo dígamelo."
                    if self._uses_formal_tone(chat_id)
                    else "Si quieres que lo consulte luego, solo dímelo."
                )
            )
            return self._localize(chat_id, text)

        reply = self._generate_reply(chat_id=chat_id, intent="escalation_confirm")
        text = reply or (
            "Solo para confirmar: ¿quiere que lo consulte? Responda con 'sí' o 'no'."
            if self._uses_formal_tone(chat_id)
            else "Solo para confirmar: ¿quieres que lo consulte? Responde con 'sí' o 'no'."
        )
        return self._localize(chat_id, text)

    def _interpret_confirmation(self, text: str) -> Optional[bool]:
        t = (text or "").strip().lower()
        if not t:
            return None

        try:
            llm = ModelConfig.get_llm(ModelTier.INTERNAL)
            system_prompt = (
                "Clasifica la respuesta del huésped sobre si autoriza que lo consultemos.\n"
                "Responde SOLO con una etiqueta exacta: yes, no, unclear.\n"
                "- yes: confirma explícitamente que lo consultemos.\n"
                "- no: rechaza explícitamente que lo consultemos.\n"
                "- unclear: cualquier otro caso ambiguo o tema distinto."
            )
            user_prompt = f"Respuesta del huésped:\n{t}\n\nEtiqueta:"
            raw = llm.invoke(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
            )
            label = (getattr(raw, "content", None) or str(raw or "")).strip().lower()
            label = re.sub(r"[^a-z_]", "", label)
            if label == "yes":
                return True
            if label == "no":
                return False
            return None
        except Exception:
            return None

    def _request_escalation_confirmation(self, chat_id: str, user_input: str, motivo: str) -> str:
        self.memory_manager.set_flag(
            chat_id,
            FLAG_ESCALATION_CONFIRMATION_PENDING,
            {
                "guest_message": user_input,
                "reason": motivo,
                "escalation_type": "info_not_found",
            },
        )
        reply = self._generate_reply(chat_id=chat_id, intent="escalation_confirm")
        text = reply or (
            "Ahora mismo no tengo ese dato confirmado. "
            + (
                "¿Quiere que lo consulte? Responda con 'sí' o 'no'."
                if self._uses_formal_tone(chat_id)
                else "¿Quieres que lo consulte? Responde con 'sí' o 'no'."
            )
        )
        return self._localize(chat_id, text)

    def _should_attach_to_pending_escalation(self, chat_id: str, user_input: str) -> bool:
        text = (user_input or "").strip()
        if not text:
            return False
        try:
            pending_context = ""
            if self.memory_manager and chat_id:
                try:
                    pending_context = (
                        self.memory_manager.get_flag(chat_id, FLAG_ESCALATION_CONFIRMATION_PENDING) or {}
                    )
                    if isinstance(pending_context, dict):
                        pending_context = (
                            str(pending_context.get("guest_message") or "")
                            or str(pending_context.get("reason") or "")
                        )
                    else:
                        pending_context = str(pending_context or "")
                except Exception:
                    pending_context = ""
            llm = ModelConfig.get_llm(ModelTier.INTERNAL)
            system_prompt = (
                "Clasifica el mensaje del huésped en UNA etiqueta exacta: attach o normal.\n"
                "Hay una escalación activa pendiente con el encargado.\n"
                "attach: el mensaje amplía/continúa esa consulta pendiente o añade otra pregunta para "
                "incluir en la misma gestión, incluso si no lo dice explícitamente.\n"
                "normal: sólo si el mensaje es claramente independiente de la gestión pendiente y no "
                "requiere incorporarse al hilo con el encargado.\n"
                "Responde SOLO con: attach o normal."
            )
            user_prompt = (
                "Consulta pendiente (si existe):\n"
                f"{pending_context or 'No disponible'}\n\n"
                "Mensaje nuevo del huésped:\n"
                f"{text}\n\nEtiqueta:"
            )
            raw = llm.invoke(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
            )
            label = (getattr(raw, "content", None) or str(raw or "")).strip().lower()
            return label == "attach"
        except Exception:
            return False

    def _has_real_property_context(self, chat_id: str) -> bool:
        if not self.memory_manager or not chat_id:
            return False
        if self.memory_manager.get_flag(chat_id, "property_id"):
            return True
        prop_name = self.memory_manager.get_flag(chat_id, "property_name")
        inst_name = self.memory_manager.get_flag(chat_id, "instance_hotel_code")
        if not prop_name:
            return False
        if inst_name and str(inst_name).strip().lower() == str(prop_name).strip().lower():
            return False
        return True

    def _normalize_text(self, value: str) -> str:
        text = (value or "").strip().lower()
        if not text:
            return ""
        text = unicodedata.normalize("NFKD", text)
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
        text = re.sub(r"[^a-z0-9]+", " ", text).strip()
        return re.sub(r"\s+", " ", text)

    def _uses_formal_tone(self, chat_id: str) -> bool:
        if not self.memory_manager or not chat_id:
            return False
        tone = str(self.memory_manager.get_flag(chat_id, "tone") or "").strip().lower()
        if not tone:
            return False
        return any(
            marker in tone
            for marker in ("usted", "3ª persona", "3a persona", "tercera persona")
        )

    def _generate_reply(self, chat_id: str, intent: str, **data) -> str:
        """
        Genera respuestas con LLM usando prompts configurables.
        """
        formal = self._uses_formal_tone(chat_id)
        static_replies = {
            "escalation_confirm": (
                "Ahora mismo no tengo ese dato confirmado. ¿Quiere que lo consulte? Responda con 'sí' o 'no'."
                if formal
                else "Ahora mismo no tengo ese dato confirmado. ¿Quieres que lo consulte? Responde con 'sí' o 'no'."
            ),
            "escalation_declined": (
                "Perfecto, seguimos buscando alternativas sin consultarlo por ahora. "
                "Si quiere que lo consulte después, solo dígamelo."
                if formal
                else "Perfecto, seguimos buscando alternativas sin consultarlo por ahora. "
                "Si quieres que lo consulte luego, solo dímelo."
            ),
            "inciso_wait": (
                "Un momento, estoy revisándolo para poder informarle mejor."
                if formal
                else "Un momento, lo estoy revisando para poder ayudarte mejor."
            ),
        }
        if intent in static_replies:
            return static_replies[intent]

        try:
            prompt = load_prompt("reply_generator.txt") or ""
        except Exception:
            prompt = ""
        if not prompt:
            return ""
        payload = {
            "intent": intent,
            "lang": self._get_guest_lang(chat_id),
            "tone": self.memory_manager.get_flag(chat_id, "tone") if self.memory_manager else None,
            **data,
        }
        try:
            out = self.llm.invoke(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ]
            ).content.strip()
        except Exception:
            return ""
        return out.strip()

    def _recent_guest_messages(self, chat_id: str, limit: int = 6) -> list[str]:
        if not self.memory_manager or not chat_id:
            return []
        try:
            raw_history = self.memory_manager.get_memory(chat_id, limit=max(limit * 4, 12)) or []
        except TypeError:
            try:
                raw_history = self.memory_manager.get_memory(chat_id) or []
            except Exception:
                raw_history = []
        except Exception:
            raw_history = []

        guest_msgs: list[str] = []
        fallback_user_msgs: list[str] = []
        for item in raw_history:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            if role == "guest":
                guest_msgs.append(content)
            elif role == "user":
                fallback_user_msgs.append(content)

        return (guest_msgs or fallback_user_msgs)[-limit:]

    def _get_guest_lang(self, chat_id: str, user_input: Optional[str] = None) -> str:
        if not self.memory_manager or not chat_id:
            return "es"
        prev = self.memory_manager.get_flag(chat_id, "guest_lang")
        if user_input is None:
            return (prev or "es").strip().lower() or "es"
        current_input = str(user_input or "").strip()
        if not current_input:
            return (prev or "es").strip().lower() or "es"

        last_resolved_message = self.memory_manager.get_flag(chat_id, "guest_lang_last_message")
        if (
            isinstance(last_resolved_message, str)
            and last_resolved_message.strip() == current_input
            and isinstance(prev, str)
            and prev.strip()
        ):
            return prev.strip().lower()

        prev_confidence = self.memory_manager.get_flag(chat_id, "guest_lang_confidence")
        resolved_lang, resolved_confidence = language_manager.resolve_response_language(
            latest_guest_message=current_input,
            recent_guest_messages=self._recent_guest_messages(chat_id),
            guest_language_hint=prev,
            guest_language_confidence=prev_confidence,
            last_resolved_language=prev,
        )
        if resolved_lang:
            self.memory_manager.set_flag(chat_id, "guest_lang", resolved_lang)
            self.memory_manager.set_flag(chat_id, "guest_lang_confidence", resolved_confidence)
            self.memory_manager.set_flag(chat_id, "guest_lang_last_message", current_input)
            return resolved_lang
        return (prev or "es").strip().lower() or "es"

    def _localize(self, chat_id: str, text: str) -> str:
        lang = self._get_guest_lang(chat_id)
        if not text or lang == "es":
            return text
        return language_manager.ensure_language(text, lang)

    def _get_intent_text_es(self, chat_id: Optional[str], text: str) -> str:
        raw = (text or "").strip()
        if not raw:
            return ""
        if not self.memory_manager or not chat_id:
            lang = language_manager.detect_language(raw, prev_lang=None)
            if lang and lang != "es":
                return language_manager.translate_if_needed(raw, lang, "es")
            return raw
        cache = self.memory_manager.get_flag(chat_id, "intent_text_es")
        if isinstance(cache, dict) and cache.get("src") == raw and cache.get("text"):
            return cache.get("text")
        prev_lang = self.memory_manager.get_flag(chat_id, "guest_lang")
        lang = language_manager.detect_language(raw, prev_lang=prev_lang)
        translated = language_manager.translate_if_needed(raw, lang, "es") if lang and lang != "es" else raw
        self.memory_manager.set_flag(chat_id, "intent_text_es", {"src": raw, "text": translated, "lang": lang})
        return translated

    def _hydrate_context_from_active_reservation(self, chat_id: str) -> bool:
        if not self.memory_manager or not chat_id:
            return False
        try:
            instance_id = (
                self.memory_manager.get_flag(chat_id, "instance_id")
                or self.memory_manager.get_flag(chat_id, "instance_hotel_code")
            )
            reservation = get_active_chat_reservation(
                chat_id=chat_id,
                instance_id=instance_id,
            )
        except Exception:
            return False
        if not reservation:
            return False

        changed = False
        prop_id = reservation.get("property_id")
        inst_id = reservation.get("instance_id")
        folio_id = reservation.get("folio_id")
        checkin = reservation.get("checkin")
        checkout = reservation.get("checkout")
        reservation_client_name = reservation.get("client_name")

        if prop_id and not self.memory_manager.get_flag(chat_id, "property_id"):
            self.memory_manager.set_flag(chat_id, "property_id", prop_id)
            changed = True
        if inst_id and not self.memory_manager.get_flag(chat_id, "instance_id"):
            self.memory_manager.set_flag(chat_id, "instance_id", inst_id)
            self.memory_manager.set_flag(chat_id, "instance_hotel_code", inst_id)
            self.memory_manager.set_flag(chat_id, "wa_context_instance_id", inst_id)
            changed = True
        if folio_id and not self.memory_manager.get_flag(chat_id, "folio_id"):
            self.memory_manager.set_flag(chat_id, "folio_id", folio_id)
            changed = True
        if checkin and not self.memory_manager.get_flag(chat_id, "checkin"):
            self.memory_manager.set_flag(chat_id, "checkin", checkin)
        if checkout and not self.memory_manager.get_flag(chat_id, "checkout"):
            self.memory_manager.set_flag(chat_id, "checkout", checkout)
        if reservation_client_name and not self.memory_manager.get_flag(chat_id, "client_name"):
            self.memory_manager.set_flag(chat_id, "client_name", reservation_client_name)

        if prop_id and (
            not self.memory_manager.get_flag(chat_id, "property_name")
            or not self.memory_manager.get_flag(chat_id, "tone")
        ):
            table = self.memory_manager.get_flag(chat_id, "property_table") or DEFAULT_PROPERTY_TABLE
            if table:
                try:
                    payload = fetch_property_by_id(table, prop_id)
                except Exception:
                    payload = {}
                display = (payload.get("name") or payload.get("property_name") or "").strip()
                tone = str(payload.get("tone") or "").strip()
                if display:
                    self.memory_manager.set_flag(chat_id, "property_name", display)
                    self.memory_manager.set_flag(chat_id, "property_display_name", display)
                if tone:
                    self.memory_manager.set_flag(chat_id, "tone", tone)
                else:
                    self.memory_manager.clear_flag(chat_id, "tone")
        return True

    async def _resolve_property_from_message(self, chat_id: str, user_input: str) -> bool:
        if not self.memory_manager or not chat_id:
            return False

        raw = (user_input or "").strip()
        if not raw:
            return False

        log.info(
            "[PROPERTY_RESOLVE] message start chat_id=%s input=%s prop_id=%s",
            chat_id,
            user_input,
            self.memory_manager.get_flag(chat_id, "property_id"),
        )

        property_id = None
        if raw.isdigit():
            try:
                property_id = int(raw)
            except Exception:
                property_id = None

        try:
            tool = create_property_context_tool(memory_manager=self.memory_manager, chat_id=chat_id)
            tool.invoke(
                {
                    "property_name": None if property_id is not None else raw,
                    "property_id": property_id,
                }
            )
        except Exception as exc:
            log.warning("No se pudo resolver property desde mensaje: %s", exc)
            return False

        resolved = bool(
            self.memory_manager.get_flag(chat_id, "property_id")
            or self.memory_manager.get_flag(chat_id, "property_name")
        )
        if resolved:
            self._sync_property_labels(chat_id)
        log.info(
            "[PROPERTY_RESOLVE] message end chat_id=%s resolved=%s prop_id=%s prop_name=%s",
            chat_id,
            resolved,
            self.memory_manager.get_flag(chat_id, "property_id"),
            self.memory_manager.get_flag(chat_id, "property_name"),
        )
        return resolved

    def _sync_property_labels(self, chat_id: str) -> None:
        if not self.memory_manager or not chat_id:
            return
        prop_id = self.memory_manager.get_flag(chat_id, "property_id")
        if not prop_id:
            return
        table = self.memory_manager.get_flag(chat_id, "property_table") or DEFAULT_PROPERTY_TABLE
        if not table:
            return
        try:
            payload = fetch_property_by_id(table, prop_id)
        except Exception:
            payload = {}
        display = (payload.get("name") or payload.get("property_name") or "").strip()
        instance_id = (payload.get("instance_id") or "").strip()
        tone = str(payload.get("tone") or "").strip()
        if display:
            self.memory_manager.set_flag(chat_id, "property_display_name", display)
            self.memory_manager.set_flag(chat_id, "property_name", display)
        if instance_id:
            self.memory_manager.set_flag(chat_id, "instance_id", instance_id)
            self.memory_manager.set_flag(chat_id, "instance_hotel_code", instance_id)
            self.memory_manager.set_flag(chat_id, "wa_context_instance_id", instance_id)
        if tone:
            self.memory_manager.set_flag(chat_id, "tone", tone)
        else:
            self.memory_manager.clear_flag(chat_id, "tone")

    def _is_new_reservation_intent(self, text: str, chat_id: Optional[str] = None) -> bool:
        t = self._normalize_text(self._get_intent_text_es(chat_id, text))
        if not t:
            return False
        triggers = [
            "otra reserva",
            "nueva reserva",
            "hacer una reserva",
            "quiero hacer una reserva",
            "quiero reservar",
            "reservar",
        ]
        if any(trig in t for trig in triggers):
            return True
        # "otra" + "reserva" en el mismo mensaje
        return "reserva" in t and ("otra" in t or "nuevo" in t or "nueva" in t)

    def _should_force_kb_when_no_tools(
        self,
        chat_id: str,
        user_input: str,
        response: str,
        intermediate_steps: list,
    ) -> bool:
        """
        Si no hubo uso de tools, decide semánticamente si esta consulta
        debía pasar por base_conocimientos y fuerza esa llamada.
        """
        if intermediate_steps:
            return False
        text = (user_input or "").strip()
        if not text:
            return False
        try:
            llm = ModelConfig.get_llm(ModelTier.INTERNAL)
            hist = ""
            if self.memory_manager and chat_id:
                try:
                    recent = self.memory_manager.get_memory(chat_id, limit=6) or []
                    lines = []
                    for msg in recent:
                        role = str((msg or {}).get("role") or "")
                        content = str((msg or {}).get("content") or "")
                        lines.append(f"{role}: {content}")
                    hist = "\n".join(lines[-6:])
                except Exception:
                    hist = ""
            system_prompt = (
                "Clasifica si DEBE forzarse una llamada a 'base_conocimientos'.\n"
                "Responde SOLO 'force' o 'skip'.\n"
                "force: consulta factual de hotel (servicios, horarios, políticas, ubicación, parking, accesibilidad, "
                "desayuno, normas) y no hubo tool call.\n"
                "skip: intención de reservar/crear reserva, listar hoteles/properties explícitamente, conversación no factual, "
                "o cuando en el historial reciente ya existe una respuesta directa y válida para la misma consulta."
            )
            user_prompt = (
                f"Mensaje: {text}\n"
                f"Respuesta propuesta: {response}\n"
                f"Historial reciente:\n{hist}\n\n"
                "Etiqueta:"
            )
            raw = llm.invoke(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
            )
            label = (getattr(raw, "content", None) or str(raw or "")).strip().lower()
            return label == "force"
        except Exception:
            return False

    def _should_require_availability_before_onboarding(
        self,
        chat_id: str,
        user_input: str,
        intermediate_steps: list,
    ) -> bool:
        """
        Regla de negocio:
        para crear reserva, primero debe pasar por disponibilidad/precios.
        Si en este turno se intentó onboarding sin haber usado disponibilidad,
        decide semánticamente si hay que redirigir a disponibilidad.
        """
        if not intermediate_steps:
            return False
        # Solo aplica a intención de NUEVA reserva, no a consultas de reservas existentes.
        if not self._is_new_reservation_intent(user_input, chat_id):
            return False

        used_onboarding = False
        used_availability = False
        for step in intermediate_steps:
            try:
                action = step[0] if isinstance(step, (list, tuple)) and step else None
                tool_name = str(getattr(action, "tool", "") or "").strip().lower()
            except Exception:
                tool_name = ""
            if tool_name == "onboarding_reservas":
                used_onboarding = True
            if tool_name in {"disponibilidad_precios", "availability_pricing"}:
                used_availability = True

        if not used_onboarding or used_availability:
            return False

        try:
            history_text = ""
            if self.memory_manager and chat_id:
                recent = self.memory_manager.get_memory(chat_id, limit=10) or []
                lines = []
                for msg in recent:
                    role = str((msg or {}).get("role") or "")
                    content = str((msg or {}).get("content") or "")
                    lines.append(f"{role}: {content}")
                history_text = "\n".join(lines[-10:])

            llm = ModelConfig.get_llm(ModelTier.INTERNAL)
            system_prompt = (
                "Decide si hay que exigir disponibilidad antes de onboarding.\n"
                "Responde SOLO: require o allow.\n"
                "require: el huésped aún no tiene opciones/tarifas claras de habitaciones "
                "o no ha elegido una habitación concreta para unas fechas.\n"
                "allow: ya hay disponibilidad/tarifas confirmadas recientemente y el huésped "
                "ya eligió opción concreta, por lo que onboarding puede continuar.\n"
                "Si el caso es consulta de reservas existentes (mis reservas, localizador, estado de reserva), "
                "siempre responde allow."
            )
            user_prompt = (
                f"Mensaje actual: {user_input}\n"
                f"Historial reciente:\n{history_text}\n\n"
                "Etiqueta:"
            )
            raw = llm.invoke(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
            )
            label = (getattr(raw, "content", None) or str(raw or "")).strip().lower()
            return label == "require"
        except Exception:
            return True

    async def _delegate_escalation_to_interno(
            self,
            *,
            user_input: str,
            chat_id: str,
            motivo: str,
            escalation_type: str,
            context: str,
        ):
            if not self.interno_agent:
                log.error("⚠️ Se intentó escalar pero no hay InternoAgent configurado")
                return

            try:
                query = (
                    f"[ESCALATION REQUEST]\n"
                    f"Motivo: {motivo}\n"
                    f"Mensaje del huésped: {user_input}\n"
                    f"Tipo: {escalation_type}\n"
                    f"Contexto: {context}\n"
                    f"Chat ID: {chat_id}"
                )

                await self.interno_agent.ainvoke(user_input=query, chat_id=chat_id)
                if self.memory_manager:
                    # Marca hilo activo para anidar nuevas consultas del huésped
                    # en la escalación pendiente del mismo chat.
                    targets = {str(chat_id or "").strip()}
                    raw_chat = str(chat_id or "").strip()
                    if ":" in raw_chat:
                        tail = raw_chat.split(":")[-1].strip()
                        if tail:
                            targets.add(tail)
                    try:
                        last_mem = self.memory_manager.get_flag(chat_id, "last_memory_id")
                        if isinstance(last_mem, str) and last_mem.strip():
                            targets.add(last_mem.strip())
                    except Exception:
                        pass
                    for target in [t for t in targets if t]:
                        self.memory_manager.set_flag(target, "escalation_in_progress", True)
                        self.memory_manager.clear_flag(target, "last_escalation_followup_message")

            except Exception as exc:
                log.error(f"❌ Error delegando escalación a InternoAgent: {exc}", exc_info=True)

    async def ainvoke(
        self,
        user_input: str,
        chat_id: str,
        hotel_name: str = "Hotel",
        chat_history: Optional[List] = None,
    ) -> str:

        if not self.memory_manager:
            raise RuntimeError("MemoryManager no configurado en MainAgent")

        if chat_id not in self.locks:
            self.locks[chat_id] = asyncio.Lock()

        async with self.locks[chat_id]:

            try:
                self._get_guest_lang(chat_id, user_input)
                if self.memory_manager.get_flag(chat_id, "escalation_in_progress"):
                    if self._should_attach_to_pending_escalation(chat_id, user_input):
                        candidate = (user_input or "").strip()
                        last_forwarded = (
                            self.memory_manager.get_flag(chat_id, "last_escalation_followup_message")
                            if self.memory_manager
                            else None
                        )
                        if candidate and candidate != str(last_forwarded or "").strip():
                            await self._delegate_escalation_to_interno(
                                user_input=candidate,
                                chat_id=chat_id,
                                motivo="Ampliación del huésped mientras la escalación está en curso",
                                escalation_type="info_not_found",
                                context="Escalación en progreso: incorporar esta nueva petición al hilo pendiente",
                            )
                            self.memory_manager.set_flag(
                                chat_id,
                                "last_escalation_followup_message",
                                candidate,
                            )
                        return self._localize(chat_id, "Un momento, sigo consultándolo.")

                pending = await self._handle_pending_confirmation(chat_id, user_input)
                if pending is not None:
                    self.memory_manager.save(chat_id, "user", user_input)
                    self.memory_manager.save(chat_id, "assistant", pending)
                    return pending

                if (
                    not self._is_new_reservation_intent(user_input, chat_id)
                    and not self._has_real_property_context(chat_id)
                ):
                    self._hydrate_context_from_active_reservation(chat_id)

                if not self._has_real_property_context(chat_id):
                    await self._resolve_property_from_message(chat_id, user_input)

                base_prompt = load_prompt("main_prompt.txt") or self._get_default_prompt()
                dynamic_context = build_dynamic_context_from_memory(self.memory_manager, chat_id)
                if dynamic_context:
                    self.system_prompt = (
                        f"{get_time_context()}\n\n{base_prompt.strip()}\n\n{dynamic_context}"
                    )
                else:
                    self.system_prompt = f"{get_time_context()}\n\n{base_prompt.strip()}"

                if chat_history is None:
                    chat_history = self.memory_manager.get_memory_as_messages(chat_id, limit=30)
                chat_history = chat_history or []

                tools = self.build_tools(chat_id, hotel_name)
                prompt_template = self.create_prompt_template()

                chain_agent = create_openai_tools_agent(
                    llm=self.llm,
                    tools=tools,
                    prompt=prompt_template,
                )

                executor = AgentExecutor(
                    agent=chain_agent,
                    tools=tools,
                    verbose=True,
                    max_iterations=25,
                    return_intermediate_steps=True,
                    max_execution_time=90,
                    handle_parsing_errors=True,
                )

                inciso_flag = self.memory_manager.get_flag(chat_id, "inciso_enviado")
                consulta_flag = self.memory_manager.get_flag(chat_id, "consulta_base_realizada")

                if consulta_flag and not self.memory_manager.get_flag(chat_id, FLAG_ESCALATION_CONFIRMATION_PENDING):
                    return self._request_escalation_confirmation(
                        chat_id,
                        user_input,
                        motivo="Consulta repetida sin información",
                    )

                result = await executor.ainvoke(
                    input={"input": user_input, "chat_history": chat_history},
                    config={"callbacks": []},
                )

                response = (result.get("output") or "").strip()
                intermediate_steps = result.get("intermediate_steps") or []

                if self._should_require_availability_before_onboarding(
                    chat_id=chat_id,
                    user_input=user_input,
                    intermediate_steps=intermediate_steps,
                ):
                    dispo_tool = next(
                        (t for t in tools if getattr(t, "name", "") == "disponibilidad_precios"),
                        None,
                    )
                    if dispo_tool is not None:
                        try:
                            forced_dispo = await dispo_tool.ainvoke(
                                {"query": user_input, "pregunta": user_input}
                            )
                            forced_text = (forced_dispo or "").strip()
                            if forced_text:
                                response = forced_text
                        except Exception as exc:
                            log.warning(
                                "No se pudo redirigir de onboarding a disponibilidad: %s",
                                exc,
                            )

                if self._should_force_kb_when_no_tools(
                    chat_id=chat_id,
                    user_input=user_input,
                    response=response,
                    intermediate_steps=intermediate_steps,
                ):
                    kb_tool = next((t for t in tools if getattr(t, "name", "") == "base_conocimientos"), None)
                    if kb_tool is not None:
                        try:
                            forced = await kb_tool.ainvoke({"query": user_input, "pregunta": user_input})
                            forced_text = (forced or "").strip()
                            if forced_text and forced_text.upper() != "ESCALATION_REQUIRED":
                                response = forced_text
                        except Exception as exc:
                            log.warning("No se pudo forzar llamada a base_conocimientos: %s", exc)

                if (
                    not response
                    or "no hay información disponible" in response.lower()
                    or response.upper() == "ESCALATION_REQUIRED"
                ):
                    self.memory_manager.set_flag(chat_id, "consulta_base_realizada", True)

                    if not inciso_flag and self.send_callback:
                        wait_msg = self._generate_reply(chat_id=chat_id, intent="inciso_wait") or (
                            "Un momento, estoy revisando internamente cómo ayudarle mejor."
                            if self._uses_formal_tone(chat_id)
                            else "Dame un momento, estoy revisando internamente cómo ayudarte mejor."
                        )
                        await self.send_callback(wait_msg)
                        self.memory_manager.set_flag(chat_id, "inciso_enviado", True)

                    return self._request_escalation_confirmation(
                        chat_id,
                        user_input,
                        motivo="Sin resultados en knowledge_base",
                    )

                self.memory_manager.save(chat_id, "user", user_input)
                final_response = self._localize(chat_id, response)
                self.memory_manager.save(chat_id, "assistant", final_response)

                self.memory_manager.clear_flag(chat_id, "inciso_enviado")
                self.memory_manager.clear_flag(chat_id, "consulta_base_realizada")

                return final_response

            except Exception as e:
                log.error(f"❌ Error en MainAgent ({chat_id}): {e}", exc_info=True)

                await self._delegate_escalation_to_interno(
                    user_input=user_input,
                    chat_id=chat_id,
                    motivo=str(e),
                    escalation_type="error",
                    context="Escalación por excepción en MainAgent",
                )
                fallback_msg = self._localize(
                    chat_id,
                    (
                        "Ha ocurrido un problema interno y ya lo estoy revisando. "
                        "Le aviso en breve."
                    )
                    if self._uses_formal_tone(chat_id)
                    else (
                        "Ha ocurrido un problema interno y ya lo estoy revisando. "
                        "Te aviso en breve."
                    )
                )

                # Guarda el intercambio aunque haya error para no perder contexto
                try:
                    if self.memory_manager:
                        self.memory_manager.save(chat_id, "user", user_input)
                        self.memory_manager.save(chat_id, "assistant", fallback_msg)
                except Exception:
                    log.debug("No se pudo guardar en memoria tras excepción", exc_info=True)

                # Mensaje determinista → evita duplicados por variaciones aleatorias
                return fallback_msg


def create_main_agent(
    memory_manager: Optional[MemoryManager] = None,
    send_callback: Optional[Callable] = None,
    interno_agent: Optional[InternoAgent] = None,
) -> MainAgent:
    return MainAgent(
        memory_manager=memory_manager,
        send_message_callback=send_callback,
        interno_agent=interno_agent,
    )
