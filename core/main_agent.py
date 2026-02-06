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
from core.utils.escalation_messages import EscalationMessages
from core.instance_context import DEFAULT_PROPERTY_TABLE, fetch_property_by_id, fetch_properties_by_code
from core.db import get_active_chat_reservation


log = logging.getLogger("MainAgent")

FLAG_ESCALATION_CONFIRMATION_PENDING = "escalation_confirmation_pending"
FLAG_PROPERTY_CONFIRMATION_PENDING = "property_confirmation_pending"
FLAG_PROPERTY_DISAMBIGUATION_PENDING = "property_disambiguation_pending"
FLAG_PROPERTY_SWITCH_PENDING = "property_switch_pending"
FLAG_PROPERTY_SWITCH_ASKED = "property_switch_asked"


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

            await self._delegate_escalation_to_interno(
                user_input=original_message,
                chat_id=chat_id,
                motivo=motivo,
                escalation_type=escalation_type,
                context="Escalación confirmada por el huésped",
            )
            return EscalationMessages.get_by_context("info")

        if decision is False:
            self.memory_manager.clear_flag(chat_id, FLAG_ESCALATION_CONFIRMATION_PENDING)
            self.memory_manager.clear_flag(chat_id, "consulta_base_realizada")
            return (
                "Perfecto, seguimos buscando alternativas sin molestar al encargado. "
                "Si quieres que lo contacte luego, solo dímelo."
            )

        return "Solo para confirmar: ¿quieres que contacte con el encargado? Responde con 'sí' o 'no'."

    def _interpret_confirmation(self, text: str) -> Optional[bool]:
        t = (text or "").strip().lower()
        if not t:
            return None

        negatives = ["prefiero que no", "mejor no", "no gracias", "no hace falta", "no por ahora", "no quiero"]
        positives = ["sí", "si", "hazlo", "adelante", "claro", "vale", "ok", "confirmo", "yes"]

        if any(n in t for n in negatives):
            return False
        if any(p in t for p in positives):
            return True
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
        return (
            "Ahora mismo no tengo ese dato confirmado. "
            "¿Quieres que consulte al encargado? Responde con 'sí' o 'no'."
        )

    def _needs_property_context(self, chat_id: str) -> bool:
        if not self.memory_manager or not chat_id:
            return False
        return not self.memory_manager.get_flag(chat_id, "property_id")

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

    def _get_property_candidates(self, chat_id: str) -> list[dict]:
        if not self.memory_manager or not chat_id:
            return []
        candidates = self.memory_manager.get_flag(chat_id, "property_disambiguation_candidates") or []
        if isinstance(candidates, list):
            return candidates
        return []

    def _ensure_property_candidates(self, chat_id: str) -> list[dict]:
        """
        Carga candidatos de property desde instancia si no existen en memoria.
        """
        if not self.memory_manager or not chat_id:
            return []
        existing = self._get_property_candidates(chat_id)
        if existing:
            log.info("[PROPERTY_CANDIDATES] reuse existing chat_id=%s count=%s", chat_id, len(existing))
            return existing
        instance_code = self.memory_manager.get_flag(chat_id, "instance_id") or self.memory_manager.get_flag(chat_id, "instance_hotel_code")
        if not instance_code:
            instance_code = self.memory_manager.get_flag(chat_id, "property_name")
        if not instance_code:
            log.info("[PROPERTY_CANDIDATES] no instance_code chat_id=%s", chat_id)
            return []
        table = self.memory_manager.get_flag(chat_id, "property_table") or DEFAULT_PROPERTY_TABLE
        if not table:
            log.info("[PROPERTY_CANDIDATES] no property_table chat_id=%s", chat_id)
            return []
        try:
            rows = fetch_properties_by_code(table, str(instance_code))
        except Exception:
            rows = []
        if not rows:
            log.info(
                "[PROPERTY_CANDIDATES] none from instance chat_id=%s instance_code=%s",
                chat_id,
                instance_code,
            )
            return []
        candidates = [
            {
                "property_id": row.get("property_id"),
                "name": row.get("name") or row.get("property_name"),
                "instance_id": row.get("instance_id"),
                "city": row.get("city"),
                "street": row.get("street"),
            }
            for row in rows
        ]
        self.memory_manager.set_flag(chat_id, "property_disambiguation_candidates", candidates)
        self.memory_manager.set_flag(chat_id, "property_disambiguation_instance_id", str(instance_code))
        log.info(
            "[PROPERTY_CANDIDATES] loaded chat_id=%s instance_code=%s count=%s",
            chat_id,
            instance_code,
            len(candidates),
        )
        log.info(
            "[PROPERTY_CANDIDATES] names chat_id=%s names=%s",
            chat_id,
            [c.get("name") for c in candidates],
        )
        return candidates

    def _normalize_text(self, value: str) -> str:
        text = (value or "").strip().lower()
        if not text:
            return ""
        text = unicodedata.normalize("NFKD", text)
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
        text = re.sub(r"[^a-z0-9]+", " ", text).strip()
        return re.sub(r"\s+", " ", text)

    def _is_valid_property_label(self, value: Optional[str]) -> bool:
        text = self._normalize_text(value or "")
        if not text:
            return False
        if len(text) < 3:
            return False
        generic = {"hotel", "hostal", "alojamiento", "propiedad"}
        if text in generic:
            return False
        banned_terms = [
            "reserva",
            "reservar",
            "quiero",
            "hacer",
            "otra",
            "nueva",
            "precio",
            "precios",
            "disponibilidad",
            "oferta",
        ]
        if any(term in text for term in banned_terms):
            return False
        return True

    def _tokenize(self, value: str) -> list[str]:
        text = self._normalize_text(value)
        if not text:
            return []
        stop = {"hotel", "hostal", "aldea", "alda", "el", "la", "los", "las", "de", "del"}
        return [t for t in text.split() if t and t not in stop]

    def _load_embedded_prompt(self, key: str) -> str:
        """
        Carga snippets embebidos dentro de main_prompt.txt usando marcadores:
        [[KEY]] ... [[/KEY]]
        """
        try:
            base_prompt = load_prompt("main_prompt.txt") or ""
        except Exception:
            base_prompt = ""
        if not base_prompt or not key:
            return ""
        pattern = rf"\\[\\[{re.escape(key)}\\]\\](.*?)\\[\\[/{re.escape(key)}\\]\\]"
        match = re.search(pattern, base_prompt, flags=re.DOTALL | re.IGNORECASE)
        if not match:
            return ""
        return match.group(1).strip()

    def _build_disambiguation_question(self, candidates: list[dict]) -> str:
        prompt = self._load_embedded_prompt("PROPERTY_DISAMBIGUATION")
        if prompt:
            return prompt
        return "¿En cuál de nuestros hoteles estarías interesado? Puedes darme un nombre aproximado."

    def _build_property_not_in_instance(self) -> str:
        prompt = self._load_embedded_prompt("PROPERTY_NOT_IN_INSTANCE")
        if prompt:
            return prompt
        return "No encuentro ese hotel en esta instancia. ¿Puedes indicarme otro nombre (aprox)?"

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

        if prop_id and not self.memory_manager.get_flag(chat_id, "property_name"):
            table = self.memory_manager.get_flag(chat_id, "property_table") or DEFAULT_PROPERTY_TABLE
            if table:
                try:
                    payload = fetch_property_by_id(table, prop_id)
                except Exception:
                    payload = {}
                display = (payload.get("name") or payload.get("property_name") or "").strip()
                if display:
                    self.memory_manager.set_flag(chat_id, "property_name", display)
                    self.memory_manager.set_flag(chat_id, "property_display_name", display)
        return True

    def _resolve_property_from_candidates(self, chat_id: str, user_input: str) -> bool:
        if not self.memory_manager or not chat_id:
            return False
        raw = (user_input or "").strip()
        if not raw:
            return False
        candidates = self._get_property_candidates(chat_id)
        if not candidates:
            log.info("[PROPERTY_MATCH] no candidates chat_id=%s input=%s", chat_id, user_input)
            return False

        selected = None
        lowered = raw.lower()
        raw_tokens = set(self._tokenize(raw))
        log.info(
            "[PROPERTY_MATCH] normalized chat_id=%s input=%s normalized=%s",
            chat_id,
            user_input,
            self._normalize_text(raw),
        )
        log.info(
            "[PROPERTY_MATCH] start chat_id=%s input=%s tokens=%s candidates=%s",
            chat_id,
            user_input,
            list(raw_tokens),
            len(candidates),
        )
        log.info(
            "[PROPERTY_MATCH] candidate names chat_id=%s names=%s",
            chat_id,
            [c.get("name") for c in candidates],
        )
        best_score = -1
        best_cand = None
        for cand in candidates:
            name = (cand or {}).get("name") or ""
            if name and (name.lower() in lowered or lowered in name.lower()):
                selected = cand
                break
            if name and raw_tokens:
                cand_tokens = set(self._tokenize(name))
                overlap = len(raw_tokens & cand_tokens)
                if overlap > best_score:
                    best_score = overlap
                    best_cand = cand
            # Si algún token significativo aparece en el nombre, tomarlo como señal mínima.
            if not selected and name and raw_tokens:
                for token in raw_tokens:
                    if len(token) >= 3 and token in name.lower():
                        selected = cand
                        break
                if selected:
                    break

        if not selected and best_cand and best_score > 0:
            selected = best_cand

        if not selected and raw.isdigit():
            for cand in candidates:
                if str(cand.get("property_id") or "").strip() == raw:
                    selected = cand
                    break

        if not selected:
            log.info("[PROPERTY_MATCH] no match chat_id=%s input=%s", chat_id, user_input)
            return False

        # Fija flags básicos de inmediato para evitar repetir la pregunta.
        try:
            prop_id = selected.get("property_id")
            prop_name = selected.get("name")
            if prop_id is not None:
                self.memory_manager.set_flag(chat_id, "property_id", prop_id)
                self.memory_manager.set_flag(chat_id, "wa_context_property_id", prop_id)
            if prop_name:
                self.memory_manager.set_flag(chat_id, "property_name", prop_name)
                self.memory_manager.set_flag(chat_id, "property_display_name", prop_name)
        except Exception:
            return False

        # Enriquecer contexto con la tool si es posible (no bloquea).
        try:
            tool = create_property_context_tool(memory_manager=self.memory_manager, chat_id=chat_id)
            tool.invoke(
                {
                    "property_name": selected.get("name"),
                    "property_id": selected.get("property_id"),
                }
            )
        except Exception as exc:
            log.warning("No se pudo enriquecer property desde candidatos: %s", exc)

        self._sync_property_labels(chat_id)

        log.info(
            "[PROPERTY_MATCH] matched chat_id=%s property_id=%s name=%s",
            chat_id,
            selected.get("property_id"),
            selected.get("name"),
        )
        return bool(
            self.memory_manager.get_flag(chat_id, "property_id")
            or self.memory_manager.get_flag(chat_id, "property_name")
        )

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
        if display:
            self.memory_manager.set_flag(chat_id, "property_display_name", display)
            self.memory_manager.set_flag(chat_id, "property_name", display)
        if instance_id:
            self.memory_manager.set_flag(chat_id, "instance_id", instance_id)
            self.memory_manager.set_flag(chat_id, "instance_hotel_code", instance_id)
            self.memory_manager.set_flag(chat_id, "wa_context_instance_id", instance_id)

    def _request_property_context(self, chat_id: str, original_message: str) -> str:
        self.memory_manager.set_flag(
            chat_id,
            FLAG_PROPERTY_CONFIRMATION_PENDING,
            {"original_message": original_message},
        )
        prompt = self._load_embedded_prompt("PROPERTY_REQUEST")
        if prompt:
            return prompt
        return "¿En qué hotel o propiedad te gustaría alojarte?"

    def _clear_property_context(self, chat_id: str) -> None:
        if not self.memory_manager:
            return
        for key in (
            "property_id",
            "property_name",
            "property_display_name",
            "wa_context_property_id",
            "wa_context_instance_id",
            "property_disambiguation_candidates",
            "property_disambiguation_instance_id",
            "property_disambiguation_attempts",
            FLAG_PROPERTY_DISAMBIGUATION_PENDING,
        ):
            self.memory_manager.clear_flag(chat_id, key)

    def _is_new_reservation_intent(self, text: str) -> bool:
        t = (text or "").strip().lower()
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

    def _request_property_switch_confirmation(self, chat_id: str, original_message: str) -> str:
        self.memory_manager.set_flag(
            chat_id,
            FLAG_PROPERTY_SWITCH_PENDING,
            {"original_message": original_message},
        )
        self.memory_manager.set_flag(chat_id, FLAG_PROPERTY_SWITCH_ASKED, True)
        current_display = self.memory_manager.get_flag(chat_id, "property_display_name")
        current_name = self.memory_manager.get_flag(chat_id, "property_name")
        instance_name = self.memory_manager.get_flag(chat_id, "instance_hotel_code")
        property_id = self.memory_manager.get_flag(chat_id, "property_id")
        if instance_name and current_name and str(instance_name).strip().lower() == str(current_name).strip().lower():
            current_name = None
        if instance_name and property_id:
            try:
                table = self.memory_manager.get_flag(chat_id, "property_table") or DEFAULT_PROPERTY_TABLE
                payload = fetch_property_by_id(table, property_id) if table else {}
                prop_code = (payload.get("name") or payload.get("property_name") or "").strip()
                if prop_code and str(prop_code).lower() != str(instance_name).strip().lower():
                    current_display = None
                    current_name = None
            except Exception:
                pass
        if not self._is_valid_property_label(current_display):
            current_display = None
        if not self._is_valid_property_label(current_name):
            current_name = None
        current = current_display or current_name
        if not current:
            return "¿Para qué hotel es la reserva? Dime el nombre (aprox) y continúo."
        return (
            f"¿Esta nueva reserva es para {current} o para otro hotel? "
            "Si es otro, dime el nombre (aprox) y continúo."
        )

    def _request_property_switch_confirmation_with_hint(
        self,
        chat_id: str,
        original_message: str,
        *,
        property_id_hint: Optional[int] = None,
        property_label_hint: Optional[str] = None,
    ) -> str:
        self.memory_manager.set_flag(
            chat_id,
            FLAG_PROPERTY_SWITCH_PENDING,
            {
                "original_message": original_message,
                "property_id": property_id_hint,
                "property_label": property_label_hint,
            },
        )
        self.memory_manager.set_flag(chat_id, FLAG_PROPERTY_SWITCH_ASKED, True)
        label = property_label_hint if self._is_valid_property_label(property_label_hint) else None
        instance_name = self.memory_manager.get_flag(chat_id, "instance_hotel_code")
        if instance_name and property_id_hint:
            try:
                table = self.memory_manager.get_flag(chat_id, "property_table") or DEFAULT_PROPERTY_TABLE
                payload = fetch_property_by_id(table, property_id_hint) if table else {}
                prop_code = (payload.get("name") or payload.get("property_name") or "").strip()
                if prop_code and str(prop_code).lower() != str(instance_name).strip().lower():
                    label = None
            except Exception:
                pass
        current = label or "el mismo hotel"
        if current == "el mismo hotel":
            return "¿Para qué hotel es la reserva? Dime el nombre (aprox) y continúo."
        return (
            f"¿Esta nueva reserva es para {current} o para otro hotel? "
            "Si es otro, dime el nombre (aprox) y continúo."
        )

    def _is_multi_property_instance(self, chat_id: str) -> bool:
        if not self.memory_manager or not chat_id:
            return False
        candidates = self.memory_manager.get_flag(chat_id, "property_disambiguation_candidates") or []
        if isinstance(candidates, list) and len(candidates) > 1:
            return True
        instance_code = self.memory_manager.get_flag(chat_id, "instance_id") or self.memory_manager.get_flag(chat_id, "instance_hotel_code")
        if not instance_code:
            return False
        table = self.memory_manager.get_flag(chat_id, "property_table") or DEFAULT_PROPERTY_TABLE
        if not table:
            return False
        try:
            rows = fetch_properties_by_code(table, str(instance_code))
        except Exception:
            rows = []
        if len(rows) > 1:
            return True
        if len(rows) == 1:
            row = rows[0] or {}
            prop_id = row.get("property_id")
            prop_code = row.get("name")
            if prop_id or prop_code:
                try:
                    tool = create_property_context_tool(
                        memory_manager=self.memory_manager,
                        chat_id=chat_id,
                    )
                    tool.invoke({"property_id": prop_id, "property_name": prop_code})
                except Exception as exc:
                    log.warning("No se pudo fijar property unica desde instancia: %s", exc)
        return False

    def _get_property_hint_from_history(self, chat_id: str) -> tuple[Optional[int], Optional[str]]:
        if not self.memory_manager:
            return None, None
        if not self.memory_manager.has_history(chat_id):
            return None, None
        prop_id = self.memory_manager.get_last_property_id_hint(chat_id)
        if not prop_id:
            return None, None
        prop_label = None
        try:
            table = self.memory_manager.get_flag(chat_id, "property_table") or DEFAULT_PROPERTY_TABLE
            payload = fetch_property_by_id(table, prop_id) if table else {}
            prop_label = payload.get("name") or payload.get("property_name")
        except Exception:
            prop_label = None
        return prop_id, prop_label

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
                skip_new_reservation_checks = False
                has_active_res_context = False
                if self.memory_manager.get_flag(chat_id, "escalation_in_progress"):
                    return "##INCISO## Un momento, sigo verificando tu solicitud con el encargado."

                pending = await self._handle_pending_confirmation(chat_id, user_input)
                if pending is not None:
                    self.memory_manager.save(chat_id, "user", user_input)
                    self.memory_manager.save(chat_id, "assistant", pending)
                    return pending

                pending_switch = self.memory_manager.get_flag(chat_id, FLAG_PROPERTY_SWITCH_PENDING)
                if pending_switch:
                    decision = self._interpret_confirmation(user_input)
                    original_message = (
                        pending_switch.get("original_message")
                        if isinstance(pending_switch, dict)
                        else None
                    )
                    hint_property_id = (
                        pending_switch.get("property_id")
                        if isinstance(pending_switch, dict)
                        else None
                    )
                    current_display = self.memory_manager.get_flag(chat_id, "property_display_name")
                    current_name = self.memory_manager.get_flag(chat_id, "property_name")
                    # Si el usuario menciona explícitamente el hotel actual, tomarlo como "sí"
                    if decision is None and (current_display or current_name):
                        current_label = (current_display or current_name or "").lower()
                        if current_label and current_label in (user_input or "").lower():
                            decision = True
                    if decision is True:
                        self.memory_manager.clear_flag(chat_id, FLAG_PROPERTY_SWITCH_PENDING)
                        self.memory_manager.clear_flag(chat_id, FLAG_PROPERTY_SWITCH_ASKED)
                        if hint_property_id and not self.memory_manager.get_flag(chat_id, "property_id"):
                            try:
                                tool = create_property_context_tool(
                                    memory_manager=self.memory_manager,
                                    chat_id=chat_id,
                                )
                                tool.invoke({"property_id": hint_property_id})
                            except Exception as exc:
                                log.warning("No se pudo fijar property desde historial: %s", exc)
                        self.memory_manager.save(chat_id, "user", user_input)
                        if original_message:
                            user_input = original_message
                        skip_new_reservation_checks = True
                    elif decision is False:
                        self.memory_manager.clear_flag(chat_id, FLAG_PROPERTY_SWITCH_PENDING)
                        self.memory_manager.clear_flag(chat_id, FLAG_PROPERTY_SWITCH_ASKED)
                        self._clear_property_context(chat_id)
                        question = self._request_property_context(chat_id, original_message or user_input)
                        self.memory_manager.save(chat_id, "user", user_input)
                        self.memory_manager.save(chat_id, "assistant", question)
                        return question
                    else:
                        # Si el usuario dio un nombre distinto al hotel actual, limpia contexto y resuelve de nuevo
                        current_label = (self.memory_manager.get_flag(chat_id, "property_display_name")
                                         or self.memory_manager.get_flag(chat_id, "property_name")
                                         or "")
                        if current_label and current_label.lower() not in (user_input or "").lower():
                            self._clear_property_context(chat_id)
                        candidate_name = (user_input or "").strip()
                        if candidate_name.lower().startswith(("es para ", "para ", "en ")):
                            for prefix in ("es para ", "para ", "en "):
                                if candidate_name.lower().startswith(prefix):
                                    candidate_name = candidate_name[len(prefix):].strip()
                                    break
                        for prefix in ("el ", "la ", "los ", "las "):
                            if candidate_name.lower().startswith(prefix):
                                candidate_name = candidate_name[len(prefix):].strip()
                                break
                        resolved = await self._resolve_property_from_message(chat_id, candidate_name)
                        if resolved:
                            self.memory_manager.clear_flag(chat_id, FLAG_PROPERTY_SWITCH_PENDING)
                            self.memory_manager.clear_flag(chat_id, FLAG_PROPERTY_SWITCH_ASKED)
                            self.memory_manager.save(chat_id, "user", user_input)
                            if original_message:
                                user_input = original_message
                            skip_new_reservation_checks = True
                        else:
                            # Evitar bucle: si no se pudo resolver, preguntar por hotel directamente
                            self.memory_manager.clear_flag(chat_id, FLAG_PROPERTY_SWITCH_PENDING)
                            self.memory_manager.clear_flag(chat_id, FLAG_PROPERTY_SWITCH_ASKED)
                            question = self._request_property_context(chat_id, original_message or user_input)
                            self.memory_manager.save(chat_id, "user", user_input)
                            self.memory_manager.save(chat_id, "assistant", question)
                            return question

                # Si ya preguntamos “mismo u otro hotel” y el huésped dio un nombre, no repetir la pregunta.
                if self.memory_manager.get_flag(chat_id, FLAG_PROPERTY_SWITCH_ASKED) and self._is_valid_property_label(user_input):
                    candidate_name = (user_input or "").strip()
                    if candidate_name.lower().startswith(("es para ", "para ", "en ")):
                        for prefix in ("es para ", "para ", "en "):
                            if candidate_name.lower().startswith(prefix):
                                candidate_name = candidate_name[len(prefix):].strip()
                                break
                    for prefix in ("el ", "la ", "los ", "las "):
                        if candidate_name.lower().startswith(prefix):
                            candidate_name = candidate_name[len(prefix):].strip()
                            break
                    resolved = await self._resolve_property_from_message(chat_id, candidate_name)
                    if not resolved:
                        candidates = self._ensure_property_candidates(chat_id)
                        if candidates and self._resolve_property_from_candidates(chat_id, candidate_name):
                            resolved = True
                    self.memory_manager.clear_flag(chat_id, FLAG_PROPERTY_SWITCH_ASKED)
                    if resolved:
                        skip_new_reservation_checks = True
                    else:
                        question = self._request_property_context(chat_id, user_input)
                        self.memory_manager.save(chat_id, "user", user_input)
                        self.memory_manager.save(chat_id, "assistant", question)
                        return question

                # Si hay una reserva activa en contexto y NO es una nueva reserva, evita pedir hotel otra vez.
                if (
                    not self._is_new_reservation_intent(user_input)
                    and not self._has_real_property_context(chat_id)
                ):
                    # Solo usa la reserva activa si NO hay mención explícita a otra property.
                    if not self._is_valid_property_label(user_input):
                        if self._hydrate_context_from_active_reservation(chat_id):
                            has_active_res_context = True

                # 🔎 Si el usuario menciona un hotel directamente, intenta resolver antes de volver a preguntar
                if (
                    not self.memory_manager.get_flag(chat_id, "property_id")
                    and self._is_valid_property_label(user_input)
                    and not self._is_new_reservation_intent(user_input)
                ):
                    resolved = await self._resolve_property_from_message(chat_id, user_input)
                    if resolved:
                        self.memory_manager.clear_flag(chat_id, FLAG_PROPERTY_DISAMBIGUATION_PENDING)
                        self.memory_manager.clear_flag(chat_id, "property_disambiguation_candidates")
                        self.memory_manager.clear_flag(chat_id, "property_disambiguation_instance_id")
                        self.memory_manager.clear_flag(chat_id, FLAG_PROPERTY_CONFIRMATION_PENDING)
                        skip_new_reservation_checks = True

                if (
                    not skip_new_reservation_checks
                    and self._has_real_property_context(chat_id)
                    and self._is_new_reservation_intent(user_input)
                ):
                    prop_id_hint = self.memory_manager.get_last_property_id_hint(chat_id) if self.memory_manager else None
                    if self._is_multi_property_instance(chat_id):
                        if prop_id_hint:
                            question = self._request_property_switch_confirmation(chat_id, user_input)
                            self.memory_manager.save(chat_id, "user", user_input)
                            self.memory_manager.save(chat_id, "assistant", question)
                            return question
                        # Sin hint de property previa: pedir hotel directamente para evitar arrastrar contexto viejo.
                        self._clear_property_context(chat_id)
                        question = self._request_property_context(chat_id, user_input)
                        self.memory_manager.save(chat_id, "user", user_input)
                        self.memory_manager.save(chat_id, "assistant", question)
                        return question

                if (
                    not skip_new_reservation_checks
                    and self._is_new_reservation_intent(user_input)
                    and not (
                        self.memory_manager.get_flag(chat_id, "property_id")
                        or self.memory_manager.get_flag(chat_id, "property_name")
                    )
                ):
                    if self._is_multi_property_instance(chat_id):
                        hint_id, hint_label = self._get_property_hint_from_history(chat_id)
                        if hint_id or hint_label:
                            question = self._request_property_switch_confirmation_with_hint(
                                chat_id,
                                user_input,
                                property_id_hint=hint_id,
                                property_label_hint=hint_label,
                            )
                            self.memory_manager.save(chat_id, "user", user_input)
                            self.memory_manager.save(chat_id, "assistant", question)
                            return question

                candidates = self._get_property_candidates(chat_id)
                pending_disambiguation = self.memory_manager.get_flag(chat_id, FLAG_PROPERTY_DISAMBIGUATION_PENDING)
                if pending_disambiguation:
                    # Si el usuario ya dio otro hotel, intenta resolver directamente antes de insistir con candidatos
                    resolved = await self._resolve_property_from_message(chat_id, user_input)
                    if not resolved:
                        resolved = self._resolve_property_from_candidates(chat_id, user_input)
                        if not resolved:
                            attempts = self.memory_manager.get_flag(chat_id, "property_disambiguation_attempts") or 0
                            attempts = int(attempts) + 1 if str(attempts).isdigit() else 1
                            self.memory_manager.set_flag(chat_id, "property_disambiguation_attempts", attempts)
                            instance_id = self.memory_manager.get_flag(chat_id, "instance_id") or self.memory_manager.get_flag(chat_id, "instance_hotel_code")
                            if attempts >= 2 and instance_id:
                                question = self._build_property_not_in_instance()
                                self.memory_manager.clear_flag(chat_id, FLAG_PROPERTY_DISAMBIGUATION_PENDING)
                                self.memory_manager.clear_flag(chat_id, "property_disambiguation_candidates")
                                self.memory_manager.clear_flag(chat_id, "property_disambiguation_instance_id")
                                self.memory_manager.clear_flag(chat_id, "property_disambiguation_attempts")
                                self.memory_manager.save(chat_id, "user", user_input)
                                self.memory_manager.save(chat_id, "assistant", question)
                                return question
                            question = self._build_disambiguation_question(candidates)
                            self.memory_manager.save(chat_id, "user", user_input)
                            self.memory_manager.save(chat_id, "assistant", question)
                            return question
                    self.memory_manager.clear_flag(chat_id, FLAG_PROPERTY_DISAMBIGUATION_PENDING)
                    self.memory_manager.clear_flag(chat_id, "property_disambiguation_candidates")
                    self.memory_manager.clear_flag(chat_id, "property_disambiguation_instance_id")
                    self.memory_manager.clear_flag(chat_id, "property_disambiguation_attempts")
                    self.memory_manager.save(chat_id, "user", user_input)
                    if isinstance(pending_disambiguation, dict):
                        original_message = pending_disambiguation.get("original_message")
                        if original_message:
                            user_input = original_message

                pending_property = self.memory_manager.get_flag(chat_id, FLAG_PROPERTY_CONFIRMATION_PENDING)
                if pending_property:
                    resolved = await self._resolve_property_from_message(chat_id, user_input)
                    if not resolved:
                        # Intentar resolver contra candidatos antes de volver a preguntar
                        candidates = self._ensure_property_candidates(chat_id)
                        if candidates and self._resolve_property_from_candidates(chat_id, user_input):
                            resolved = True
                    if not resolved:
                        log.info(
                            "[PROPERTY_RESOLVE] pending failed chat_id=%s input=%s candidates=%s",
                            chat_id,
                            user_input,
                            len(candidates or []),
                        )
                        prompt = self._load_embedded_prompt("PROPERTY_REQUEST")
                        question = prompt or "¿Podrías decirme el nombre del hotel en el que quieres alojarte?"
                        self.memory_manager.save(chat_id, "user", user_input)
                        self.memory_manager.save(chat_id, "assistant", question)
                        return question
                    self.memory_manager.clear_flag(chat_id, FLAG_PROPERTY_CONFIRMATION_PENDING)
                    original_message = pending_property.get("original_message") if isinstance(pending_property, dict) else None
                    self.memory_manager.save(chat_id, "user", user_input)
                    if original_message:
                        user_input = original_message

                # Si hay candidatos (o se pueden cargar), intenta resolver antes de volver a preguntar.
                candidates = self._ensure_property_candidates(chat_id)
                if candidates and not self.memory_manager.get_flag(chat_id, "property_id"):
                    resolved = self._resolve_property_from_candidates(chat_id, user_input)
                    if resolved:
                        self.memory_manager.clear_flag(chat_id, FLAG_PROPERTY_DISAMBIGUATION_PENDING)
                        self.memory_manager.clear_flag(chat_id, "property_disambiguation_candidates")
                        self.memory_manager.clear_flag(chat_id, "property_disambiguation_instance_id")
                        self.memory_manager.clear_flag(chat_id, "property_disambiguation_attempts")
                        self.memory_manager.save(chat_id, "user", user_input)
                    else:
                        log.info(
                            "[PROPERTY_RESOLVE] candidates unresolved chat_id=%s input=%s candidates=%s",
                            chat_id,
                            user_input,
                            len(candidates or []),
                        )
                        attempts = self.memory_manager.get_flag(chat_id, "property_disambiguation_attempts") or 0
                        attempts = int(attempts) + 1 if str(attempts).isdigit() else 1
                        self.memory_manager.set_flag(chat_id, "property_disambiguation_attempts", attempts)
                        instance_id = self.memory_manager.get_flag(chat_id, "instance_id") or self.memory_manager.get_flag(chat_id, "instance_hotel_code")
                        if attempts >= 2 and instance_id:
                            question = self._build_property_not_in_instance()
                            self.memory_manager.clear_flag(chat_id, FLAG_PROPERTY_DISAMBIGUATION_PENDING)
                            self.memory_manager.clear_flag(chat_id, "property_disambiguation_candidates")
                            self.memory_manager.clear_flag(chat_id, "property_disambiguation_instance_id")
                            self.memory_manager.clear_flag(chat_id, "property_disambiguation_attempts")
                        else:
                            question = self._build_disambiguation_question(candidates)
                        self.memory_manager.set_flag(
                            chat_id,
                            FLAG_PROPERTY_DISAMBIGUATION_PENDING,
                            {"original_message": user_input},
                        )
                        self.memory_manager.save(chat_id, "user", user_input)
                        self.memory_manager.save(chat_id, "assistant", question)
                        return question

                # Intentar resolver property directamente si el usuario ya dio un nombre
                if not self._has_real_property_context(chat_id):
                    log.info(
                        "[PROPERTY_RESOLVE] pre-check chat_id=%s prop_id=%s prop_name=%s instance_code=%s",
                        chat_id,
                        self.memory_manager.get_flag(chat_id, "property_id"),
                        self.memory_manager.get_flag(chat_id, "property_name"),
                        self.memory_manager.get_flag(chat_id, "instance_hotel_code"),
                    )
                    # Asegura candidatos antes de intentar resolver por mensaje
                    self._ensure_property_candidates(chat_id)
                    log.info("[PROPERTY_RESOLVE] attempt direct resolve chat_id=%s input=%s", chat_id, user_input)
                    resolved = await self._resolve_property_from_message(chat_id, user_input)
                    if resolved:
                        log.info("[PROPERTY_RESOLVE] direct resolved chat_id=%s", chat_id)
                        self.memory_manager.save(chat_id, "user", user_input)
                    else:
                        log.info("[PROPERTY_RESOLVE] direct NOT resolved chat_id=%s", chat_id)
                        # Si hay candidatos en memoria, intenta resolver contra ellos antes de preguntar
                        candidates = self._get_property_candidates(chat_id)
                        if candidates:
                            log.info(
                                "[PROPERTY_RESOLVE] trying candidates chat_id=%s candidates=%s",
                                chat_id,
                                len(candidates),
                            )
                            resolved = self._resolve_property_from_candidates(chat_id, user_input)
                            if resolved:
                                log.info("[PROPERTY_RESOLVE] candidates resolved chat_id=%s", chat_id)
                                self.memory_manager.clear_flag(chat_id, FLAG_PROPERTY_DISAMBIGUATION_PENDING)
                                self.memory_manager.clear_flag(chat_id, "property_disambiguation_candidates")
                                self.memory_manager.clear_flag(chat_id, "property_disambiguation_instance_id")
                                self.memory_manager.clear_flag(chat_id, "property_disambiguation_attempts")
                                self.memory_manager.save(chat_id, "user", user_input)
                            else:
                                log.info("[PROPERTY_RESOLVE] candidates NOT resolved chat_id=%s", chat_id)
                                attempts = self.memory_manager.get_flag(chat_id, "property_disambiguation_attempts") or 0
                                attempts = int(attempts) + 1 if str(attempts).isdigit() else 1
                                self.memory_manager.set_flag(chat_id, "property_disambiguation_attempts", attempts)
                                instance_id = self.memory_manager.get_flag(chat_id, "instance_id") or self.memory_manager.get_flag(chat_id, "instance_hotel_code")
                                if attempts >= 2 and instance_id:
                                    question = self._build_property_not_in_instance()
                                    self.memory_manager.clear_flag(chat_id, FLAG_PROPERTY_DISAMBIGUATION_PENDING)
                                    self.memory_manager.clear_flag(chat_id, "property_disambiguation_candidates")
                                    self.memory_manager.clear_flag(chat_id, "property_disambiguation_instance_id")
                                    self.memory_manager.clear_flag(chat_id, "property_disambiguation_attempts")
                                else:
                                    question = self._build_disambiguation_question(candidates)
                                self.memory_manager.set_flag(
                                    chat_id,
                                    FLAG_PROPERTY_DISAMBIGUATION_PENDING,
                                    {"original_message": user_input},
                                )
                                self.memory_manager.save(chat_id, "user", user_input)
                                self.memory_manager.save(chat_id, "assistant", question)
                                return question

                if self._needs_property_context(chat_id):
                    if not (has_active_res_context and not self._is_new_reservation_intent(user_input)):
                        question = self._request_property_context(chat_id, user_input)
                        self.memory_manager.save(chat_id, "user", user_input)
                        self.memory_manager.save(chat_id, "assistant", question)
                        return question

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
                    await self._delegate_escalation_to_interno(
                        user_input=user_input,
                        chat_id=chat_id,
                        motivo="Consulta repetida sin información",
                        escalation_type="info_not_found",
                        context="Escalación automática",
                    )
                    return EscalationMessages.get_by_context("info")

                result = await executor.ainvoke(
                    input={"input": user_input, "chat_history": chat_history},
                    config={"callbacks": []},
                )

                response = (result.get("output") or "").strip()

                if (
                    not response
                    or "no hay información disponible" in response.lower()
                    or response.upper() == "ESCALATION_REQUIRED"
                ):
                    self.memory_manager.set_flag(chat_id, "consulta_base_realizada", True)

                    if not inciso_flag and self.send_callback:
                        await self.send_callback(
                            "Dame un momento, estoy revisando internamente cómo ayudarte mejor."
                        )
                        self.memory_manager.set_flag(chat_id, "inciso_enviado", True)

                    return self._request_escalation_confirmation(
                        chat_id,
                        user_input,
                        motivo="Sin resultados en knowledge_base",
                    )

                self.memory_manager.save(chat_id, "user", user_input)
                self.memory_manager.save(chat_id, "assistant", response)

                self.memory_manager.clear_flag(chat_id, "inciso_enviado")
                self.memory_manager.clear_flag(chat_id, "consulta_base_realizada")

                return response

            except Exception as e:
                log.error(f"❌ Error en MainAgent ({chat_id}): {e}", exc_info=True)

                await self._delegate_escalation_to_interno(
                    user_input=user_input,
                    chat_id=chat_id,
                    motivo=str(e),
                    escalation_type="error",
                    context="Escalación por excepción en MainAgent",
                )
                fallback_msg = (
                    "Ha ocurrido un problema interno y ya lo estoy revisando con el encargado. "
                    "Te aviso en breve."
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
