"""
InternoAgent v7 - Sub-agente Independiente con sincronización de memoria
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from datetime import datetime
from typing import Any, List, Optional

from langchain.agents import AgentExecutor, create_openai_tools_agent
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder

from core.config import ModelConfig, ModelTier
from core.escalation_db import get_latest_pending_escalation, save_escalation, update_escalation
from core.language_manager import language_manager
from core.socket_manager import emit_event
from core.utils.time_context import get_time_context
from core.utils.utils_prompt import load_prompt
from tools.interno_tool import (
    ESCALATIONS_STORE,
    Escalation,
    _synthesize_escalation_query,
    create_interno_tools,
    send_to_encargado,
)

log = logging.getLogger("InternoAgent")


def _resolve_bookai_enabled_flag(memory_manager: Any, *keys: str) -> Optional[bool]:
    if not memory_manager:
        return None
    for key in keys:
        if not key:
            continue
        try:
            val = memory_manager.get_flag(key, "bookai_enabled")
        except Exception:
            val = None
        if isinstance(val, bool):
            return val
    return None


class InternoAgent:
    """Agente interno independiente con creación de executor por invocación."""

    def __init__(
        self,
        memory_manager: Optional[Any] = None,
        escalation_db: Optional[Any] = None,
        channel_manager: Optional[Any] = None,
        model_tier: ModelTier = ModelTier.INTERNAL,
    ) -> None:
        self.memory_manager = memory_manager
        self.escalation_db = escalation_db
        self.channel_manager = channel_manager
        self.model_tier = model_tier
        self.escalations = ESCALATIONS_STORE

        self.llm = ModelConfig.get_llm(model_tier)
        log.info("InternoAgent inicializado (modelo: %s)", self.llm.model_name)

    async def ainvoke(
        self,
        user_input: str,
        chat_id: str,
        escalation_id: Optional[str] = None,
        escalation_context: str = "",
        context_window: int = 15,
        chat_history: Optional[List[Any]] = None,
        escalation_payload: Optional[dict[str, Any]] = None,
        auto_notify: bool = False,
    ) -> str:

        pre_notified = False
        notify_result: Optional[str] = None

        try:
            log.info("[InternoAgent] ainvoke inicio: %s - chat_id: %s", escalation_id, chat_id)

            # --- Cargar memoria ---
            if chat_history is None and self.memory_manager:
                chat_history = await self._safe_call(
                    getattr(self.memory_manager, "get_memory_as_messages", None),
                    conversation_id=chat_id,
                    limit=context_window,
                )
            chat_history = chat_history or []

            # --- Tools ---
            tools = create_interno_tools(memory_manager=self.memory_manager)

            # --- Prompt ---
            system_prompt = self._build_system_prompt(escalation_context)
            prompt_template = ChatPromptTemplate.from_messages(
                [
                    ("system", system_prompt),
                    MessagesPlaceholder(variable_name="chat_history", optional=True),
                    ("human", "{input}"),
                    MessagesPlaceholder(variable_name="agent_scratchpad", optional=True),
                ]
            )

            # --- Crear agente ---
            agent_chain = create_openai_tools_agent(
                llm=self.llm, tools=tools, prompt=prompt_template
            )

            executor = AgentExecutor(
                agent=agent_chain,
                tools=tools,
                verbose=True,
                max_iterations=15,
                return_intermediate_steps=True,
                handle_parsing_errors=True,
                max_execution_time=60,
            )

            # --- Ejecutar agente ---
            result = await executor.ainvoke(
                input={"input": user_input, "chat_history": chat_history},
                config={"callbacks": []},
            )

            output = (result.get("output") or "").strip()
            if not output:
                output = "No se pudo procesar la solicitud."

            # --- Actualizar DB ---
            if self.escalation_db and escalation_id:
                await self._safe_call(
                    getattr(self.escalation_db, "update_escalation", None),
                    escalation_id=escalation_id,
                    updates={
                        "latest_message": user_input,
                        "latest_response": output,
                        "updated_at": datetime.utcnow().isoformat(),
                    },
                )

            return output

        except Exception as exc:
            log.error("Error en InternoAgent.ainvoke: %s", exc, exc_info=True)
            raise

    async def _persist_interaction(
        self,
        *,
        chat_id: str,
        user_input: str,
        output: str,
        escalation_id: Optional[str],
        notify_result: Optional[str],
    ) -> None:

        await self._safe_call(
            getattr(self.memory_manager, "save", None),
            conversation_id=chat_id,
            role="user",
            content=user_input,
            escalation_id=escalation_id,
        )
        await self._safe_call(
            getattr(self.memory_manager, "save", None),
            conversation_id=chat_id,
            role="assistant",
            content=f"[InternoAgent] {output}",
            escalation_id=escalation_id,
        )

        if notify_result:
            await self._safe_call(
                getattr(self.memory_manager, "save", None),
                conversation_id=chat_id,
                role="system",
                content=f"[InternoAgent] Notificación enviada: {notify_result}",
                escalation_id=escalation_id,
            )

    async def handle_guest_escalation(
        self,
        chat_id: str,
        guest_message: str,
        reason: str,
        escalation_type: str = "info_not_found",
        context: str = "Auto-escalación",
        confirmation_flag: Optional[str] = None,
    ) -> str:

        if self.memory_manager:
            try:
                self.memory_manager.set_flag(chat_id, "escalation_in_progress", True)
                if confirmation_flag:
                    self.memory_manager.clear_flag(chat_id, confirmation_flag)
            except Exception:
                pass

        try:
            response = await self.escalate(
                guest_chat_id=chat_id,
                guest_message=guest_message,
                escalation_type=escalation_type,
                reason=reason,
                context=context,
            )
            return response

        except Exception:
            if self.memory_manager:
                try:
                    self.memory_manager.clear_flag(chat_id, "escalation_in_progress")
                except Exception:
                    pass
            raise

    async def escalate(
        self,
        guest_chat_id: str,
        guest_message: str,
        escalation_type: str,
        reason: str,
        context: str,
        property_id: Optional[str | int] = None,
    ) -> str:
        def _guest_lang() -> str:
            msg = (guest_message or "").strip()
            fresh_lang = None
            # Detecta idioma del mensaje actual sin arrastrar histórico.
            if msg:
                try:
                    fresh_lang = (language_manager.detect_language(msg, prev_lang=None) or "").strip().lower()
                except Exception:
                    fresh_lang = None
            try:
                if self.memory_manager:
                    for key in [str(guest_chat_id or "").strip(), re.sub(r"\D", "", str(guest_chat_id or ""))]:
                        if not key:
                            continue
                        value = self.memory_manager.get_flag(key, "guest_lang")
                        if value:
                            mem_lang = str(value).strip().lower() or "es"
                            # Si el mensaje actual trae señal clara y difiere del histórico, priorízalo.
                            if fresh_lang and fresh_lang != mem_lang and len(msg) >= 12:
                                return fresh_lang
                            return mem_lang
            except Exception:
                pass
            if fresh_lang:
                return fresh_lang
            return "es"

        def _needs_action_es(lang: str) -> str:
            raw = (reason or "").strip() or (guest_message or "").strip()
            if not raw:
                return ""
            text_es = raw
            if lang != "es":
                try:
                    text_es = language_manager.ensure_language(raw, "es").strip() or raw
                except Exception:
                    try:
                        text_es = language_manager.translate_if_needed(raw, lang, "es").strip() or raw
                    except Exception:
                        text_es = raw
            return f"El huésped solicita: {text_es} (Idioma huésped: {lang})"

        def _clean_chat_id(value: str) -> str:
            raw = str(value or "").strip()
            if ":" in raw:
                # Para ids compuestos "instancia:telefono" conservar el chat real.
                raw = raw.split(":")[-1].strip()
            return re.sub(r"\D", "", raw).strip() or raw

        def _chat_aliases(*values: str) -> list[str]:
            aliases: list[str] = []
            seen: set[str] = set()
            for raw_value in values:
                raw = str(raw_value or "").strip()
                if not raw:
                    continue
                candidates = [raw]
                if ":" in raw:
                    tail = raw.split(":")[-1].strip()
                    if tail:
                        candidates.append(tail)
                        tail_clean = re.sub(r"\D", "", tail).strip()
                        if tail_clean:
                            candidates.append(tail_clean)
                clean = re.sub(r"\D", "", raw).strip()
                if clean:
                    candidates.append(clean)
                for candidate in candidates:
                    c = str(candidate or "").strip()
                    if not c or c in seen:
                        continue
                    seen.add(c)
                    aliases.append(c)
            return aliases

        def _build_escalation_structured_payload(
            current_payload: Any,
            *,
            ai_request_type: str,
            escalation_reason: str,
            escalation_id: str,
        ) -> dict[str, Any]:
            payload: dict[str, Any]
            if isinstance(current_payload, dict):
                payload = dict(current_payload)
            elif isinstance(current_payload, str):
                try:
                    parsed = json.loads(current_payload)
                    payload = dict(parsed) if isinstance(parsed, dict) else {}
                except Exception:
                    payload = {}
            else:
                payload = {}

            metadata = payload.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            metadata["ai_request_type"] = ai_request_type
            metadata["escalation_reason"] = escalation_reason
            metadata["escalation_type"] = ai_request_type
            metadata["reason"] = escalation_reason
            metadata["escalation_id"] = escalation_id
            payload["metadata"] = metadata

            payload["ai_request_type"] = ai_request_type
            payload["escalation_reason"] = escalation_reason
            payload["escalation_type"] = ai_request_type
            payload["reason"] = escalation_reason
            payload["escalation_id"] = escalation_id
            return payload

        def _link_trigger_message_to_escalation(
            *,
            escalation_id: str,
            resolved_property_id: Optional[str | int],
        ) -> None:
            message_text = str(guest_message or "").strip()
            if not escalation_id or not message_text:
                return
            try:
                from core.db import supabase
            except Exception:
                return

            clean_value = _clean_chat_id(guest_chat_id) or _clean_chat_id(clean_chat_id)
            if not clean_value:
                return

            context_id = str(guest_chat_id or "").strip()
            has_context_id = ":" in context_id
            candidate_chat_ids: list[str] = []
            for alias in _chat_aliases(guest_chat_id, clean_chat_id):
                normalized = _clean_chat_id(alias) or str(alias or "").strip()
                if not normalized or normalized in candidate_chat_ids:
                    continue
                candidate_chat_ids.append(normalized)
            if clean_value and clean_value not in candidate_chat_ids:
                candidate_chat_ids.append(clean_value)
            if not candidate_chat_ids:
                return

            select_candidates = [
                "id, role, content, created_at, original_chat_id, property_id, structured_payload, escalation_id",
                "id, role, content, created_at, original_chat_id, property_id, structured_payload",
                "id, role, content, created_at, original_chat_id, property_id",
                "message_id, role, content, created_at, original_chat_id, property_id, structured_payload, escalation_id",
                "message_id, role, content, created_at, original_chat_id, property_id, structured_payload",
                "message_id, role, content, created_at, original_chat_id, property_id",
            ]

            matched_row: Optional[dict[str, Any]] = None
            for chat_candidate in candidate_chat_ids:
                for strict_property in (True, False):
                    if strict_property and resolved_property_id is None:
                        continue
                    for strict_context in (True, False):
                        if strict_context and not has_context_id:
                            continue
                        rows = None
                        for select_fields in select_candidates:
                            try:
                                query = (
                                    supabase.table("chat_history")
                                    .select(select_fields)
                                    .eq("conversation_id", chat_candidate)
                                    .in_("role", ["guest", "user"])
                                    .eq("content", message_text)
                                )
                                if strict_property:
                                    query = query.eq("property_id", resolved_property_id)
                                if strict_context:
                                    query = query.eq("original_chat_id", context_id)
                                rows = (
                                    query.order("created_at", desc=True)
                                    .limit(5)
                                    .execute()
                                    .data
                                    or []
                                )
                                break
                            except Exception:
                                rows = None
                                continue
                        if rows:
                            matched_row = rows[0]
                            break
                    if matched_row:
                        break
                if matched_row:
                    break

            if not matched_row:
                log.info(
                    "No se encontró mensaje disparador para enlazar escalación %s (chat=%s).",
                    escalation_id,
                    guest_chat_id,
                )
                return

            row_id = matched_row.get("id")
            row_id_field = "id"
            if row_id is None:
                row_id = matched_row.get("message_id")
                row_id_field = "message_id"
            if row_id is None:
                return

            existing_escalation_id = str(matched_row.get("escalation_id") or "").strip()
            if existing_escalation_id and existing_escalation_id != escalation_id:
                log.info(
                    "Se omite relink de mensaje %s=%s: ya vinculado a escalación %s.",
                    row_id_field,
                    row_id,
                    existing_escalation_id,
                )
                return

            structured_payload = _build_escalation_structured_payload(
                matched_row.get("structured_payload"),
                ai_request_type=str(escalation_type or "").strip(),
                escalation_reason=str(reason or "").strip(),
                escalation_id=escalation_id,
            )

            update_candidates = []
            if not existing_escalation_id:
                update_candidates.append(
                    {"escalation_id": escalation_id, "structured_payload": structured_payload}
                )
                update_candidates.append({"escalation_id": escalation_id})
            update_candidates.append({"structured_payload": structured_payload})

            for updates in update_candidates:
                if not updates:
                    continue
                try:
                    (
                        supabase.table("chat_history")
                        .update(updates)
                        .eq(row_id_field, row_id)
                        .execute()
                    )
                    log.info(
                        "Mensaje disparador enlazado a escalación %s (%s=%s).",
                        escalation_id,
                        row_id_field,
                        row_id,
                    )
                    return
                except Exception:
                    continue

        escalation_flag_targets: list[str] = []
        if self.memory_manager:
            try:
                aliases = _chat_aliases(guest_chat_id)
                for alias in aliases:
                    if alias not in escalation_flag_targets:
                        escalation_flag_targets.append(alias)
                try:
                    last_mem = self.memory_manager.get_flag(guest_chat_id, "last_memory_id")
                except Exception:
                    last_mem = None
                if isinstance(last_mem, str) and last_mem.strip() and last_mem.strip() not in escalation_flag_targets:
                    escalation_flag_targets.append(last_mem.strip())
                for target in escalation_flag_targets:
                    self.memory_manager.set_flag(target, "escalation_in_progress", True)
                    self.memory_manager.clear_flag(target, "last_escalation_followup_message")
            except Exception:
                escalation_flag_targets = []

        def _text_tokens(value: str) -> set[str]:
            words = re.findall(r"[a-z0-9áéíóúñü]{3,}", str(value or "").lower())
            return set(words)

        def _looks_like_followup(prev_message: str, new_message: str, reason_text: str, context_text: str) -> bool:
            ctx = f"{reason_text}\n{context_text}".lower()
            if any(token in ctx for token in ("ampliación", "ampliacion", "escalación en progreso", "escalacion en progreso", "follow-up", "seguimiento")):
                return True
            prev_tokens = _text_tokens(prev_message)
            new_tokens = _text_tokens(new_message)
            if not prev_tokens or not new_tokens:
                return False
            inter = len(prev_tokens & new_tokens)
            union = len(prev_tokens | new_tokens)
            if union == 0:
                return False
            # Alta similitud léxica => misma consulta con reformulación.
            return (inter / union) >= 0.7

        clean_chat_id = _clean_chat_id(guest_chat_id) or guest_chat_id
        resolved_prop_id = property_id
        if self.memory_manager and resolved_prop_id is None:
            try:
                resolved_prop_id = self.memory_manager.get_flag(guest_chat_id, "property_id")
                if resolved_prop_id is None:
                    last_mem = self.memory_manager.get_flag(guest_chat_id, "last_memory_id")
                    if isinstance(last_mem, str):
                        resolved_prop_id = self.memory_manager.get_flag(last_mem, "property_id")
            except Exception:
                resolved_prop_id = property_id

        existing_pending = None
        try:
            existing_pending = get_latest_pending_escalation(
                guest_chat_id,
                property_id=resolved_prop_id,
            )
        except Exception:
            existing_pending = None

        existing_id = str((existing_pending or {}).get("escalation_id") or "").strip()
        prev_pending_msg = str((existing_pending or {}).get("guest_message") or "").strip()
        # Regla operativa: si existe una escalación pendiente para este chat/property,
        # siempre se fusiona la nueva consulta en la misma escalación activa.
        reuse_existing = bool(existing_id)
        escalation_id = (
            existing_id
            if reuse_existing
            else f"esc_{clean_chat_id}_{int(datetime.utcnow().timestamp())}"
        )
        guest_lang = _guest_lang()
        # Persistimos la escalación antes de emitir eventos para evitar
        # parpadeos en Chatter por carreras entre socket y lectura REST.
        try:
            now_iso = datetime.utcnow().isoformat()
            if reuse_existing:
                existing_ts = str((existing_pending or {}).get("timestamp") or "").strip() or now_iso
                prev_msg = str((existing_pending or {}).get("guest_message") or "").strip()
                prev_reason = str(
                    (existing_pending or {}).get("escalation_reason")
                    or (existing_pending or {}).get("reason")
                    or ""
                ).strip()
                prev_context = str((existing_pending or {}).get("context") or "").strip()

                def _merge_lines(base: str, extra: str) -> str:
                    b = str(base or "").strip()
                    e = str(extra or "").strip()
                    if not b:
                        return e
                    if not e:
                        return b
                    if e.lower() in b.lower():
                        return b
                    return f"{b}\n{e}".strip()

                merged_message = _synthesize_escalation_query(prev_msg, guest_message)
                merged_reason = _merge_lines(prev_reason, reason)
                merged_context = _merge_lines(prev_context, context)
                update_payload = {
                    "guest_message": merged_message,
                    "escalation_type": escalation_type or (existing_pending or {}).get("escalation_type"),
                    "escalation_reason": merged_reason,
                    "context": merged_context,
                    # Mantener la hora real de creación de la escalación.
                    "timestamp": existing_ts,
                }
                if resolved_prop_id is not None:
                    update_payload["property_id"] = resolved_prop_id
                update_escalation(escalation_id, update_payload)
                esc_record = Escalation(
                    escalation_id=escalation_id,
                    guest_chat_id=str(guest_chat_id or "").strip(),
                    guest_message=merged_message,
                    escalation_type=str(update_payload.get("escalation_type") or escalation_type),
                    escalation_reason=merged_reason,
                    context=merged_context,
                    timestamp=existing_ts,
                    property_id=update_payload.get("property_id"),
                )
                self.escalations[escalation_id] = esc_record
            else:
                esc_record = Escalation(
                    escalation_id=escalation_id,
                    guest_chat_id=str(guest_chat_id or "").strip(),
                    guest_message=guest_message,
                    escalation_type=escalation_type,
                    escalation_reason=reason,
                    context=context,
                    timestamp=now_iso,
                    property_id=resolved_prop_id,
                )
                self.escalations[escalation_id] = esc_record
                save_escalation(vars(esc_record))
        except Exception as exc:
            log.warning("No se pudo pre-persistir escalación %s: %s", escalation_id, exc)

        try:
            _link_trigger_message_to_escalation(
                escalation_id=escalation_id,
                resolved_property_id=resolved_prop_id,
            )
        except Exception as exc:
            log.debug(
                "No se pudo vincular mensaje disparador con escalación %s: %s",
                escalation_id,
                exc,
            )
        if self.memory_manager and resolved_prop_id is not None:
            try:
                for key in [str(guest_chat_id or "").strip(), str(clean_chat_id or "").strip()]:
                    if key:
                        self.memory_manager.set_flag(key, "property_id", resolved_prop_id)
            except Exception:
                pass
        # Emitir escalation.created en tiempo real (fallback seguro).
        try:
            prop_id = None
            if self.memory_manager:
                try:
                    prop_id = self.memory_manager.get_flag(guest_chat_id, "property_id")
                    if prop_id is None:
                        last_mem = self.memory_manager.get_flag(guest_chat_id, "last_memory_id")
                        if isinstance(last_mem, str):
                            prop_id = self.memory_manager.get_flag(last_mem, "property_id")
                    if prop_id is None and hasattr(self.memory_manager, "get_last_property_id_hint"):
                        prop_id = self.memory_manager.get_last_property_id_hint(guest_chat_id)
                except Exception:
                    prop_id = None
            if prop_id is None and resolved_prop_id is not None:
                prop_id = resolved_prop_id
            rooms = [f"chat:{alias}" for alias in _chat_aliases(guest_chat_id, clean_chat_id)]
            rooms.append("channel:whatsapp")
            if prop_id is not None:
                rooms.append(f"property:{prop_id}")
            creation_event = "escalation.updated" if reuse_existing else "escalation.created"
            await emit_event(
                creation_event,
                {
                    "chat_id": clean_chat_id,
                    "escalation_id": escalation_id,
                    "type": escalation_type,
                    "reason": reason,
                    "context": reason,
                    "timestamp": esc_record.timestamp,
                    "created_at": esc_record.timestamp,
                    "property_id": prop_id,
                    "rooms": rooms,
                },
                rooms=rooms,
            )
            await emit_event(
                "chat.updated",
                {
                    "chat_id": clean_chat_id,
                    "needs_action": _needs_action_es(guest_lang),
                    "needs_action_type": escalation_type,
                    "needs_action_reason": (
                        f"{reason} (Idioma huésped: {guest_lang})" if (reason or "").strip() else None
                    ),
                    "timestamp": esc_record.timestamp,
                    "proposed_response": None,
                    "escalation_id": escalation_id,
                    "escalation_messages": [],
                    "property_id": prop_id,
                    "rooms": rooms,
                },
                rooms=rooms,
            )
            await emit_event(
                "escalation.chat.updated",
                {
                    "chat_id": clean_chat_id,
                    "escalation_id": escalation_id,
                    "messages": [],
                    "property_id": prop_id,
                    "rooms": rooms,
                },
                rooms=rooms,
            )
        except Exception:
            pass

        prompt = (
            ("Actualización de escalación existente:\n" if reuse_existing else "Nueva escalación:\n")
            +
            f"- ID: {escalation_id}\n"
            f"- Chat ID: {guest_chat_id}\n"
            f"- Tipo: {escalation_type}\n"
            f"- Mensaje: {guest_message}\n"
            f"- Razón: {reason}\n"
            f"- Contexto: {context}\n\n"
            "Usa la tool 'notificar_encargado' con estos datos."
        )

        try:
            return await self.ainvoke(
                user_input=prompt,
                chat_id=guest_chat_id,
                escalation_id=escalation_id,
                escalation_context=f"MAIN_AUTO_{escalation_type.upper()}",
                context_window=0,
                chat_history=[],
            )
        except Exception:
            if self.memory_manager:
                try:
                    for target in escalation_flag_targets:
                        self.memory_manager.clear_flag(target, "escalation_in_progress")
                except Exception:
                    pass
            raise

    async def process_manager_reply(
        self,
        escalation_id: str,
        manager_reply: str,
        chat_id: Optional[str] = None,
    ) -> str:

        chat_id = chat_id or self._resolve_guest_chat_id(escalation_id)
        draft_result = await self.ainvoke(
            user_input=f"Respuesta del encargado: {manager_reply}",
            chat_id=chat_id,
            escalation_id=escalation_id,
            escalation_context="HUMAN_RESPONSE",
            context_window=20,
        )
        return self._ensure_draft_preview(
            escalation_id=escalation_id,
            manager_reply=manager_reply,
            draft_result=draft_result,
        )

    async def send_confirmed_response(
        self,
        escalation_id: str,
        confirmed: bool = True,
        adjustments: str = "",
    ) -> str:

        chat_id = self._resolve_guest_chat_id(escalation_id)

        prompt = (
            f"Confirmación para la escalación {escalation_id}:\n"
            f"- Confirmado: {confirmed}\n"
            f"- Ajustes: {adjustments}\n\n"
            "Usa la tool 'confirmar_y_enviar_respuesta'."
        )

        return await self.ainvoke(
            user_input=prompt,
            chat_id=chat_id,
            escalation_id=escalation_id,
            escalation_context="HUMAN_CONFIRMATION",
            context_window=20,
        )

    def _build_system_prompt(self, escalation_context: str) -> str:
        base = load_prompt("interno_prompt.txt") or self._get_default_prompt()
        ctx = get_time_context()

        extra = ""
        c = escalation_context.upper()

        if "SUPERVISOR_INPUT" in c:
            extra = "\n\nCONTEXTO: Mensaje inapropiado detectado."
        elif "SUPERVISOR_OUTPUT" in c:
            extra = "\n\nCONTEXTO: Respuesta incoherente detectada."
        elif "MAIN_AUTO" in c:
            extra = "\n\nCONTEXTO: Escalación automática del MainAgent."
        elif "HUMAN_DIRECT" in c:
            extra = "\n\nCONTEXTO: El huésped pidió hablar con el encargado."

        return f"{ctx}\n{base}{extra}"

    def _get_default_prompt(self) -> str:
        return (
            "Eres el Agente Interno del Sistema de IA Hotelera.\n"
            "Coordinas entre encargado y huésped.\n"
            "Herramientas disponibles:\n"
            "- notificar_encargado\n"
            "- generar_borrador_respuesta\n"
            "- confirmar_y_enviar_respuesta\n"
        )

    async def ask_add_to_knowledge_base(
        self,
        chat_id: str,
        escalation_id: str,
        topic: str,
        response_content: str,
        hotel_name: str,
        superintendente_agent=None,
    ) -> str:
        """
        Preguntar al encargado si quiere agregar la respuesta a KB
        Llamado después de que se envía respuesta al huésped
        """

        try:
            log.info("Preguntando sobre agregar a KB: %s", topic)

            draft = self._create_kb_draft(
                topic=topic,
                content=response_content,
            )

            await self._safe_call(
                getattr(self.memory_manager, "save", None),
                conversation_id=chat_id,
                role="system",
                content=f"[KB_DRAFT_PENDING] {escalation_id}",
            )

            question = f"""¿Te gustaría agregar esta información a la base de conocimientos del hotel?
📋 TEMA: {topic}
📝 CONTENIDO:
{draft}
---
Responde:
✅ "sí" / "ok" / "confirmar" → Agregar
❌ "no" / "descartar" → Rechazar
"""

            return question

        except Exception as exc:
            log.error("Error en ask_add_to_knowledge_base: %s", exc)
            raise

    async def process_kb_response(
        self,
        chat_id: str,
        escalation_id: str,
        manager_response: str,
        topic: str,
        draft_content: str,
        hotel_name: str,
        superintendente_agent=None,
        pending_state: Optional[dict[str, Any]] = None,
        source: str = "escalation",
    ) -> str:
        """Procesar respuesta del encargado sobre agregar a KB"""

        response_lower = manager_response.lower().strip()

        if self._is_affirmative_kb_response(response_lower):
            if not superintendente_agent:
                return "⚠️ Superintendente no disponible para procesar"

            log.info("Encargado aprobó agregar a KB: %s", topic)

            result = await superintendente_agent.handle_kb_addition(
                topic=topic,
                content=draft_content,
                encargado_id=chat_id,
                hotel_name=hotel_name,
                source=source,
            )

            return result.get("message", "Error procesando KB addition")

        if self._is_rejection_kb_response(response_lower):
            return "✓ Información descartada. No se agregó a la base de conocimientos."

        # 🧩 Aplicar feedback al borrador existente y devolver nueva propuesta
        new_topic, new_content = self._apply_kb_feedback(topic, draft_content, manager_response)

        category = (pending_state or {}).get("category") or "general"

        ai_topic, ai_category, ai_content = await self._refine_kb_with_ai(
            topic=new_topic or topic,
            category=category,
            draft_content=new_content,
            feedback=manager_response,
        )

        final_topic = ai_topic or new_topic or topic
        final_category = ai_category or category
        final_content = ai_content or new_content

        if pending_state is not None:
            pending_state["content"] = final_content
            pending_state["topic"] = final_topic or pending_state.get("topic", topic)
            pending_state["category"] = final_category

        # Guarda marcador actualizado en memoria para que la recuperación use la versión ajustada
        kb_marker = f"[KB_DRAFT]|{hotel_name}|{final_topic}|{final_category}|{final_content}"
        await self._safe_call(
            getattr(self.memory_manager, "save", None),
            conversation_id=chat_id,
            role="system",
            content=kb_marker,
        )

        await self._safe_call(
            getattr(self.memory_manager, "save", None),
            conversation_id=chat_id,
            role="system",
            content=f"[KB_DRAFT_ADJUSTED] {escalation_id}",
        )

        preview = (
            "📝 Propuesta para base de conocimientos (ajustada):\n"
            f"TEMA: {final_topic}\n"
            f"CATEGORÍA: {final_category}\n"
            f"CONTENIDO:\n{final_content}\n\n"
            "✅ Responde 'ok' para agregarla.\n"
            "📝 Envía ajustes si quieres editarla.\n"
            "❌ Responde 'no' para descartarla."
        )
        return preview

    def _create_kb_draft(self, topic: str, content: str) -> str:
        """Crear borrador limpio y estructurado para KB"""

        cleaned = re.sub(r"\n\n+", "\n", content.strip())
        cleaned = re.sub(r"\s+", " ", cleaned)

        lines = [
            cleaned,
            f"\n[Tema: {topic}]",
            f"[Añadido: {datetime.utcnow().strftime('%d/%m/%Y')}]",
        ]

        return "\n".join(lines)

    def _apply_kb_feedback(self, topic: str, content: str, feedback: str) -> tuple[str, str]:
        """
        Aplica heurísticas simples para incorporar correcciones del encargado
        al borrador de KB (ej. 'queria decir pavo').
        """
        topic = topic or ""
        content = content or ""
        fb = feedback or ""
        fb_lower = fb.lower()

        # Intentar detectar patrón "quería/quise decir ..."
        replacement = None
        match = re.search(r"(quer[ií]a|quise)\s+decir\s+(.+)", fb_lower, flags=re.IGNORECASE)
        if match:
            replacement = feedback[match.start(2) :].strip(" .")
            # Usa solo la primera palabra si el feedback trae varias (evita frases largas como "pavo cambialo")
            replacement = replacement.split()[0] if replacement else replacement

        # Detectar patrón "cambia X por Y" o "cámbialo por Y"
        target_hint = ""
        if not replacement:
            swap_match = re.search(
                r"cambi(?:a|ar|alo|é)\s+(?:el|la|lo|los|las)?\s*([\wáéíóúñ]+)?\s*por\s+([\wáéíóúñ]+)",
                fb_lower,
                flags=re.IGNORECASE,
            )
            if swap_match:
                target_hint = (swap_match.group(1) or "").strip(" .")
                replacement = (swap_match.group(2) or "").strip(" .")

        # Si hay replacement, reemplazar primer término relevante en topic y content
        def _swap(text: str, target: str) -> str:
            if not target or not text or not replacement:
                return text
            # Reemplazo simple y case-insensitive
            return re.sub(re.escape(target), replacement, text, flags=re.IGNORECASE)

        if replacement:
            # Buscar candidato en topic (palabra no genérica)
            stop = {
                "disponibilidad",
                "hotel",
                "restaurante",
                "servicios",
                "servicio",
                "informar",
                "ofrece",
                "categoria",
                "categoría",
                "ubicacion",
                "ubicación",
                "noche",
                "pueblo",
                "cercano",
                "cerca",
                "hoy",
                "esta",
                "este",
                "nuestra",
                "menu",
                "menú",
            }
            tokens = [t.strip(" ,.;:") for t in topic.split() if len(t.strip(" ,.;:")) > 3]
            target_token = None
            if target_hint:
                target_token = target_hint
            for tok in tokens:
                if tok.lower() not in stop:
                    target_token = tok
                    break
            if target_token:
                topic = _swap(topic, target_token)
                content = _swap(content, target_token)

        return topic, content

    def _is_affirmative_kb_response(self, text: str) -> bool:
        """
        Detecta confirmaciones cortas para KB y evita dispararse con frases largas
        (ej. 'que si necesita pañuelos' no debe contarse como 'sí').
        """
        clean = re.sub(r"[¡!¿?.]", "", text or "").strip()
        tokens = [t for t in re.findall(r"[a-záéíóúñ]+", clean) if t]

        affirmative = {"si", "sí", "ok", "okay", "okey", "dale", "va", "vale", "listo", "confirmo", "confirmar"}
        if clean in affirmative:
            return True

        return 0 < len(tokens) <= 2 and all(tok in affirmative for tok in tokens)

    def _is_rejection_kb_response(self, text: str) -> bool:
        clean = re.sub(r"[¡!¿?.]", "", text or "").strip()
        tokens = [t for t in re.findall(r"[a-záéíóúñ]+", clean) if t]

        negative = {"no", "nop", "descartar", "descarto", "rechazar", "rechazo", "cancela", "cancelar"}
        if clean in negative:
            return True

        return 0 < len(tokens) <= 3 and all(tok in negative or tok == "gracias" for tok in tokens)

    async def _refine_kb_with_ai(
        self,
        *,
        topic: str,
        category: str,
        draft_content: str,
        feedback: str,
    ) -> tuple[str, str, str]:
        """
        Usa el LLM interno para reescribir el borrador de KB según el feedback del encargado.
        Mantiene tono apto para huéspedes y devuelve campos estructurados.
        """

        try:
            prompt = (
                "Eres un asistente de conocimiento hotelero. Ajusta el borrador para la base de conocimientos "
                "siguiendo exactamente las correcciones del encargado. Mantén un tono neutro y claro para huéspedes, "
                "sin instrucciones internas ni emojis. Devuelve solo:\n"
                "TEMA: <título breve>\n"
                "CATEGORÍA: <categoría>\n"
                "CONTENIDO:\n"
                "<texto final en 3-6 frases cortas>\n"
                "Evita listas largas y no inventes datos."
            )

            user_msg = (
                f"Borrador actual:\nTEMA: {topic}\nCATEGORÍA: {category}\nCONTENIDO:\n{draft_content}\n\n"
                f"Indicaciones del encargado:\n{feedback}"
            )

            response = await self.llm.ainvoke(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_msg},
                ]
            )

            text = (response.content or "").strip()
            if not text:
                return topic, category, draft_content

            topic_match = re.search(r"tema\s*:\s*(.+)", text, flags=re.IGNORECASE)
            category_match = re.search(r"categor[ií]a\s*:\s*(.+)", text, flags=re.IGNORECASE)
            content_match = re.search(r"contenido\s*:\s*(.+)", text, flags=re.IGNORECASE | re.DOTALL)

            new_topic = topic_match.group(1).strip() if topic_match else topic
            new_category = category_match.group(1).strip() if category_match else category
            new_content = content_match.group(1).strip() if content_match else draft_content

            # Evita pipes que rompen el marcador [KB_DRAFT]
            new_topic = new_topic.replace("|", "/")
            new_category = new_category.replace("|", "/")
            new_content = new_content.replace("|", "/")

            return new_topic or topic, new_category or category, new_content or draft_content

        except Exception as exc:
            log.warning("No se pudo refinar KB con IA: %s", exc, exc_info=True)
            return topic, category, draft_content

    def _ensure_draft_preview(self, escalation_id: str, manager_reply: str, draft_result: str) -> str:
        """
        Refuerza el flujo de borradores:
        - Asegura que haya draft_response guardado en memoria/DB
        - Devuelve un mensaje con instrucciones claras de OK/modificar si el agente no las generó
        """

        esc = self.escalations.get(escalation_id)
        base_draft = (manager_reply or "").strip()

        if esc:
            if esc.draft_response and esc.draft_response.strip():
                base_draft = esc.draft_response.strip()
            else:
                esc.draft_response = base_draft
                try:
                    from core.escalation_db import update_escalation
                    update_escalation(escalation_id, {"draft_response": base_draft})
                except Exception as exc:
                    log.debug("No se pudo actualizar draft_response en DB: %s", exc)

        preview = (draft_result or "").strip()
        if not base_draft:
            return preview or "No se pudo generar un borrador."

        normalized = preview.lower()
        has_prompt = "borrador" in normalized and "ok" in normalized

        if has_prompt:
            return preview

        return (
            "📝 *BORRADOR DE RESPUESTA PROPUESTO:*\n\n"
            f"{base_draft}\n\n"
            "✏️ Si deseas modificar el texto, escribe tus ajustes directamente.\n"
            "✅ Si estás conforme, responde con 'OK' para enviarlo al huésped."
        )

    def _schedule_flag_cleanup(self, chat_id: str, delay: int = 90) -> None:
        if not self.memory_manager:
            return

        async def cleanup():
            await asyncio.sleep(delay)
            try:
                self.memory_manager.clear_flag(chat_id, "escalation_in_progress")
            except Exception:
                pass

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(cleanup())
        except RuntimeError:
            pass

    def _resolve_guest_chat_id(self, escalation_id: str) -> Optional[str]:

        esc = self.escalations.get(escalation_id)
        if esc:
            return esc.guest_chat_id

        try:
            from core.escalation_db import get_escalation
            record = get_escalation(escalation_id)
            if record:
                return record.get("guest_chat_id")
        except Exception:
            pass

        return None

    async def _safe_call(self, func: Optional[Any], *args, **kwargs):
        if not func:
            return None
        try:
            res = func(*args, **kwargs)
            if inspect.isawaitable(res):
                return await res
            return res
        except Exception as exc:
            log.warning("Error en llamada segura: %s", exc)
            raise
