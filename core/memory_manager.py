import asyncio
import time
import re
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from core.db import (
    get_conversation_history,
    is_chat_visible_in_list,
    save_message,
    get_last_property_id_for_conversation,
    get_last_property_id_for_original_chat,
    upsert_chat_reservation,
)

log = logging.getLogger("MemoryManager")


class MemoryManager:
    """
    🧠 Memoria híbrida (RAM + Supabase) para cada conversación.
    ==========================================================
    - Mantiene un buffer temporal por `conversation_id`
    - Guarda y recupera mensajes de la tabla `chat_history` en Supabase
    - Mezcla automáticamente mensajes recientes de RAM + DB
    - 🆕 Añade soporte para flags de estado (ej. escalación activa)
    """

    def __init__(self, max_runtime_messages: int = 40, db_history_days: int = 7):
        self.runtime_memory: Dict[str, List[Dict[str, Any]]] = {}
        self.state_flags: Dict[str, Dict[str, Any]] = {}  # 🆕 flags de sesión por chat_id
        self.max_runtime_messages = max_runtime_messages
        self.db_history_days = db_history_days

    # ----------------------------------------------------------------------
    def _clean_id(self, conversation_id: str) -> str:
        """Normaliza el ID (quita '+' y espacios)."""
        return str(conversation_id).replace("+", "").strip()

    def _normalize_phone(self, value: str) -> str:
        digits = re.sub(r"\D", "", value or "")
        if digits:
            return digits
        return str(value or "").replace("+", "").strip()

    def _resolve_db_conversation_id(self, conversation_id: str) -> str:
        guest_number = self.get_flag(conversation_id, "guest_number")
        if guest_number:
            return self._normalize_phone(str(guest_number))
        if ":" in str(conversation_id):
            tail = str(conversation_id).split(":")[-1]
            return self._normalize_phone(tail)
        return self._normalize_phone(str(conversation_id))

    def _resolve_property_id(self, conversation_id: str):
        prop_id = self.get_flag(conversation_id, "property_id") or self.get_flag(
            conversation_id,
            "pms_property_id",
        )
        if prop_id is None:
            # 1) Fallback por historial (conversation/original_chat_id)
            try:
                hint = self.get_last_property_id_hint(conversation_id)
            except Exception:
                hint = None
            if hint is not None:
                try:
                    self.set_flag(conversation_id, "property_id", hint)
                except Exception:
                    pass
                return hint

            # 2) Fallback por instancia cuando existe una única property asociada
            try:
                instance_id = self.get_flag(conversation_id, "instance_id") or self.get_flag(
                    conversation_id,
                    "instance_hotel_code",
                )
                if instance_id:
                    from core.instance_context import fetch_properties_by_code, DEFAULT_PROPERTY_TABLE

                    table = self.get_flag(conversation_id, "property_table") or DEFAULT_PROPERTY_TABLE
                    rows = fetch_properties_by_code(str(table), str(instance_id)) if table else []
                    if isinstance(rows, list) and rows:
                        prop_ids = {
                            (row.get("property_id") if row.get("property_id") is not None else row.get("id"))
                            for row in rows
                            if isinstance(row, dict)
                            and ((row.get("property_id") is not None) or (row.get("id") is not None))
                        }
                        if len(prop_ids) == 1:
                            inferred = next(iter(prop_ids))
                            try:
                                self.set_flag(conversation_id, "property_id", inferred)
                            except Exception:
                                pass
                            return inferred
            except Exception:
                pass
            return None
        # Si es un chat compuesto (instancia:telefono) y no hay instance_id, no arrastrar property_id.
        try:
            if isinstance(conversation_id, str) and ":" in conversation_id:
                instance_id = self.get_flag(conversation_id, "instance_id") or self.get_flag(
                    conversation_id,
                    "instance_hotel_code",
                )
                if not instance_id:
                    return None
        except Exception:
            return None
        # Guardrail: si tenemos instance_id, valida que el property_id pertenece a esa instancia.
        try:
            instance_id = self.get_flag(conversation_id, "instance_id") or self.get_flag(
                conversation_id,
                "instance_hotel_code",
            )
            if instance_id:
                from core.instance_context import fetch_property_by_id, fetch_instance_by_code, DEFAULT_PROPERTY_TABLE

                table = self.get_flag(conversation_id, "property_table") or DEFAULT_PROPERTY_TABLE
                payload = fetch_property_by_id(str(table), prop_id) if table else {}
                prop_instance = payload.get("instance_id") or payload.get("instance_url")
                instance_aliases = {str(instance_id).strip()}
                try:
                    inst_payload = fetch_instance_by_code(str(instance_id).strip()) or {}
                    for key in ("instance_id", "instance_url"):
                        val = inst_payload.get(key)
                        if val:
                            instance_aliases.add(str(val).strip())
                except Exception:
                    pass
                property_aliases = set()
                if prop_instance:
                    property_aliases.add(str(prop_instance).strip())
                for key in ("instance_id", "instance_url"):
                    val = payload.get(key)
                    if val:
                        property_aliases.add(str(val).strip())
                if instance_aliases and property_aliases and instance_aliases.isdisjoint(property_aliases):
                    log.warning(
                        "⚠️ property_id no coincide con instance_id; se conserva por compatibilidad. chat_id=%s property_id=%s instance_id=%s prop_instance=%s",
                        conversation_id,
                        prop_id,
                        instance_id,
                        prop_instance,
                    )
                    return prop_id
        except Exception:
            return None
        return prop_id

    def _resolve_history_table(self, conversation_id: str) -> str:
        table = self.get_flag(conversation_id, "history_table")
        return str(table).strip() if table else "chat_history"

    def _is_recent_runtime_duplicate(
        self,
        conversation_id: str,
        role: str,
        content: str,
        channel: Optional[str],
        window_seconds: int = 6,
    ) -> bool:
        """Evita persistir duplicados consecutivos muy próximos del mismo mensaje."""
        cid = self._clean_id(conversation_id)
        msgs = self.runtime_memory.get(cid) or []
        if not msgs:
            return False
        last = msgs[-1] or {}
        if (last.get("role") or "").strip().lower() != (role or "").strip().lower():
            return False
        if str(last.get("content") or "").strip() != str(content or "").strip():
            return False
        last_channel = (last.get("channel") or "").strip().lower()
        current_channel = (channel or "").strip().lower()
        if last_channel and current_channel and last_channel != current_channel:
            return False
        try:
            last_ts = datetime.fromisoformat(str(last.get("created_at") or ""))
        except Exception:
            return False
        now = datetime.utcnow()
        return (now - last_ts).total_seconds() <= max(1, int(window_seconds))

    def get_last_property_id_hint(self, conversation_id: str, limit: int = 30) -> Optional[int]:
        """
        Busca el último property_id en el historial, incluso si no está en memoria.
        """
        try:
            table = self._resolve_history_table(conversation_id)
            if isinstance(conversation_id, str) and ":" in conversation_id:
                prop = get_last_property_id_for_original_chat(
                    conversation_id,
                    table=table,
                    limit=limit,
                )
            else:
                db_conversation_id = self._resolve_db_conversation_id(conversation_id)
                prop = get_last_property_id_for_conversation(
                    db_conversation_id,
                    table=table,
                    limit=limit,
                )
            if prop is None:
                return None
            return int(prop)
        except Exception:
            return None

    def has_history(self, conversation_id: str, limit: int = 1) -> bool:
        """
        Devuelve True si hay historial (RAM o DB) para el conversation_id.
        """
        try:
            if self.runtime_memory.get(self._clean_id(conversation_id)):
                return True
            db_conversation_id = self._resolve_db_conversation_id(conversation_id)
            table = self._resolve_history_table(conversation_id)
            property_id = self._resolve_property_id(conversation_id)
            rows = get_conversation_history(
                db_conversation_id,
                limit=limit,
                table=table,
                property_id=property_id,
            )
            return bool(rows)
        except Exception:
            return False

    # ----------------------------------------------------------------------
    def get_memory(self, conversation_id: str, limit: int = 40) -> List[Dict[str, Any]]:
        """
        Recupera el contexto (mensajes recientes) combinando Supabase + memoria local.
        Devuelve una lista de dicts con `role`, `content`, `created_at`.
        """
        cid = self._clean_id(conversation_id)

        try:
            # Mensajes en RAM
            local_msgs = self.runtime_memory.get(cid, [])

            # Mensajes en DB (últimos X días)
            since = datetime.utcnow() - timedelta(days=self.db_history_days)
            db_conversation_id = self._resolve_db_conversation_id(conversation_id)
            property_id = self._resolve_property_id(conversation_id)
            history_table = self._resolve_history_table(conversation_id)
            db_msgs = (
                get_conversation_history(
                    db_conversation_id,
                    limit=limit,
                    since=since,
                    property_id=property_id,
                    table=history_table,
                )
                or []
            )
            if property_id is not None and not db_msgs:
                # Fallback: si no hay mensajes con property_id, reintenta sin filtro.
                db_msgs = (
                    get_conversation_history(
                        db_conversation_id,
                        limit=limit,
                        since=since,
                        property_id=None,
                        table=history_table,
                    )
                    or []
                )

            # Fusionar ambos
            combined = db_msgs + local_msgs

            # Ordenar por fecha
            def parse_ts(msg):
                ts = msg.get("created_at")
                if isinstance(ts, (int, float)):
                    return float(ts)
                if isinstance(ts, datetime):
                    return ts.timestamp()
                try:
                    return datetime.fromisoformat(str(ts)).timestamp()
                except Exception:
                    return time.time()

            combined_sorted = sorted(combined, key=parse_ts)
            recent = combined_sorted[-limit:]

            # Si faltan datos persistentes, intenta inferirlos del historial reciente.
            try:
                folio_flag = self.get_flag(conversation_id, "folio_id")
                checkin_flag = self.get_flag(conversation_id, "checkin")
                checkout_flag = self.get_flag(conversation_id, "checkout")
                if not (folio_flag and checkin_flag and checkout_flag):
                    for msg in reversed(recent):
                        content = msg.get("content") or ""
                        if not isinstance(content, str):
                            continue
                        if not folio_flag:
                            m = re.search(r"(localizador|folio(?:_id)?|reserva)\s*[:#]?\s*([A-Za-z0-9-]{4,})", content, re.IGNORECASE)
                            if m:
                                folio_flag = m.group(2)
                                self.set_flag(conversation_id, "folio_id", folio_flag)
                        if not checkin_flag:
                            m = re.search(r"(entrada|check[- ]?in)\s*[:#]?\s*([0-9]{1,2}[-/][0-9]{1,2}[-/][0-9]{2,4})", content, re.IGNORECASE)
                            if m:
                                checkin_flag = m.group(2)
                                self.set_flag(conversation_id, "checkin", checkin_flag)
                        if not checkout_flag:
                            m = re.search(r"(salida|check[- ]?out)\s*[:#]?\s*([0-9]{1,2}[-/][0-9]{1,2}[-/][0-9]{2,4})", content, re.IGNORECASE)
                            if m:
                                checkout_flag = m.group(2)
                                self.set_flag(conversation_id, "checkout", checkout_flag)
                        if folio_flag and checkin_flag and checkout_flag:
                            break
            except Exception:
                pass

            log.info(
                f"🧠 Contexto cargado para {cid}: {len(recent)} mensajes "
                f"(RAM={len(local_msgs)}, DB={len(db_msgs)})"
            )

            return recent

        except Exception as e:
            log.error(f"⚠️ Error recuperando contexto de {cid}: {e}", exc_info=True)
            return []

    # ----------------------------------------------------------------------
    def save(
        self,
        conversation_id: str,
        role: str,
        content: str,
        escalation_id: Optional[str] = None,
        client_name: Optional[str] = None,
        user_id: Optional[int | str] = None,
        user_first_name: Optional[str] = None,
        user_last_name: Optional[str] = None,
        user_last_name2: Optional[str] = None,
        channel: Optional[str] = None,
        original_chat_id: Optional[str] = None,
        bypass_force_guest_role: bool = False,
        skip_recent_duplicate_guard: bool = False,
        structured_payload: Optional[dict | list] = None,
    ) -> None:
        """
        Guarda un mensaje tanto en memoria local como en Supabase.
        Roles base: user/assistant/system/tool. Se mapean a guest/bookai para persistencia.
        No añade etiquetas ni prefijos en el contenido.
        """
        cid = self._clean_id(conversation_id)
        valid_roles = {"user", "assistant", "system", "tool", "guest", "bookai"}
        normalized_role = role if role in valid_roles else "assistant"
        resolved_channel = channel or self.get_flag(conversation_id, "default_channel")
        channel_to_store = resolved_channel or channel
        if normalized_role in {"assistant", "system", "tool"}:
            normalized_role = "bookai"
        elif normalized_role == "user":
            if not bypass_force_guest_role and self.get_flag(conversation_id, "force_guest_role"):
                normalized_role = "guest"
            else:
                normalized_role = "user"

        if normalized_role not in {"guest", "user", "bookai"}:
            normalized_role = "bookai"

        is_guest = normalized_role == "guest"
        if not client_name and is_guest:
            client_name = self.get_flag(cid, "client_name")

        # Guardrail: evita doble guardado consecutivo del mismo mensaje del huésped.
        if (
            not skip_recent_duplicate_guard
            and normalized_role in {"guest", "user"}
            and self._is_recent_runtime_duplicate(
            conversation_id=conversation_id,
            role=normalized_role,
            content=content,
            channel=channel_to_store,
            )
        ):
            log.info("↩️ Duplicado reciente ignorado chat_id=%s role=%s", cid, normalized_role)
            return

        # Si el mensaje trae datos de reserva, actualiza flags (sobrescribe con lo mas reciente).
        try:
            if content:
                targets = [conversation_id]
                if isinstance(conversation_id, str) and ":" in conversation_id:
                    tail = conversation_id.split(":")[-1].strip()
                    if tail:
                        targets.append(tail)
                m = re.search(r"(localizador)\s*[:#]?\s*([A-Za-z0-9/\\-]{4,})", content, re.IGNORECASE)
                if m:
                    for target in targets:
                        self.set_flag(target, "reservation_locator", m.group(2))
                m = re.search(r"(folio(?:_id)?|folio id)\s*[:#]?\s*([A-Za-z0-9]{4,})", content, re.IGNORECASE)
                if m:
                    for target in targets:
                        self.set_flag(target, "folio_id", m.group(2))
                m = re.search(r"(entrada|check[- ]?in)\s*[:#]?\s*([0-9]{1,2}[-/][0-9]{1,2}[-/][0-9]{2,4})", content, re.IGNORECASE)
                if m:
                    for target in targets:
                        self.set_flag(target, "checkin", m.group(2))
                m = re.search(r"(salida|check[- ]?out)\s*[:#]?\s*([0-9]{1,2}[-/][0-9]{1,2}[-/][0-9]{2,4})", content, re.IGNORECASE)
                if m:
                    for target in targets:
                        self.set_flag(target, "checkout", m.group(2))
                folio_flag = self.get_flag(conversation_id, "folio_id")
                locator_flag = self.get_flag(conversation_id, "reservation_locator")
                checkin_flag = self.get_flag(conversation_id, "checkin")
                checkout_flag = self.get_flag(conversation_id, "checkout")
                if (
                    folio_flag
                    and re.fullmatch(r"(?=.*\d)[A-Za-z0-9]{4,}", str(folio_flag))
                    and channel_to_store == "whatsapp"
                    and normalized_role == "guest"
                ):
                    try:
                        resolved_chat_id = tail if isinstance(conversation_id, str) and ":" in conversation_id else conversation_id
                        original_chat_id = None
                        if isinstance(conversation_id, str) and ":" in conversation_id:
                            original_chat_id = conversation_id
                        else:
                            last_mem = self.get_flag(conversation_id, "last_memory_id")
                            if isinstance(last_mem, str) and ":" in last_mem:
                                original_chat_id = last_mem

                        instance_id = (
                            self.get_flag(conversation_id, "instance_id")
                            or self.get_flag(conversation_id, "instance_hotel_code")
                        )
                        upsert_chat_reservation(
                            chat_id=resolved_chat_id,
                            folio_id=str(folio_flag),
                            checkin=checkin_flag,
                            checkout=checkout_flag,
                            property_id=self.get_flag(conversation_id, "property_id"),
                            instance_id=instance_id,
                            original_chat_id=original_chat_id,
                            reservation_locator=locator_flag,
                            source="message",
                        )
                        log.info(
                            "🧾 memory upsert_chat_reservation chat_id=%s folio_id=%s checkin=%s checkout=%s",
                            resolved_chat_id,
                            folio_flag,
                            checkin_flag,
                            checkout_flag,
                        )
                    except Exception:
                        pass
        except Exception:
            pass

        entry = {
            "role": normalized_role,
            "content": content.strip(),
            "created_at": datetime.utcnow().isoformat(),
        }
        if escalation_id:
            entry["escalation_id"] = escalation_id
        if client_name and is_guest:
            entry["client_name"] = client_name
        if user_id is not None and str(user_id).strip() != "":
            try:
                entry["user_id"] = int(str(user_id).strip())
            except Exception:
                pass
        if user_first_name:
            entry["user_first_name"] = str(user_first_name)
        if user_last_name:
            entry["user_last_name"] = str(user_last_name)
        if user_last_name2:
            entry["user_last_name2"] = str(user_last_name2)
        if channel_to_store:
            entry["channel"] = channel_to_store
        if structured_payload is not None:
            entry["structured_payload"] = structured_payload

        # Guardar en RAM
        self.runtime_memory.setdefault(cid, []).append(entry)
        if len(self.runtime_memory[cid]) > self.max_runtime_messages:
            self.runtime_memory[cid] = self.runtime_memory[cid][-self.max_runtime_messages:]

        # Resolver original_chat_id (preferir el huesped si existe).
        resolved_original = original_chat_id
        if not resolved_original:
            last_mem = self.get_flag(conversation_id, "last_memory_id")
            if isinstance(last_mem, str) and ":" in last_mem:
                resolved_original = last_mem.strip()
        if not resolved_original and ":" in str(conversation_id or ""):
            resolved_original = str(conversation_id).strip()
        if not resolved_original:
            guest_number = (
                self.get_flag(conversation_id, "guest_number")
                or self.get_flag(conversation_id, "whatsapp_number")
            )
            if guest_number:
                resolved_original = self._normalize_phone(str(guest_number))

        # Guardar en Supabase
        try:
            db_conversation_id = self._resolve_db_conversation_id(conversation_id)
            property_id = self._resolve_property_id(conversation_id)
            save_message(
                db_conversation_id,
                normalized_role,
                entry["content"],
                escalation_id=escalation_id,
                client_name=client_name if is_guest else None,
                user_id=user_id,
                user_first_name=user_first_name,
                user_last_name=user_last_name,
                user_last_name2=user_last_name2,
                channel=channel_to_store,
                property_id=property_id,
                original_chat_id=resolved_original or cid,
                structured_payload=structured_payload,
                table=self._resolve_history_table(conversation_id),
            )
            log.debug(f"💾 Guardado en Supabase: ({cid}, {normalized_role})")
        except Exception as e:
            log.warning(f"⚠️ Error guardando mensaje en Supabase: {e}")

    # ----------------------------------------------------------------------
    def add_runtime_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        escalation_id: Optional[str] = None,
        client_name: Optional[str] = None,
        user_id: Optional[int | str] = None,
        user_first_name: Optional[str] = None,
        user_last_name: Optional[str] = None,
        user_last_name2: Optional[str] = None,
        channel: Optional[str] = None,
        original_chat_id: Optional[str] = None,
        bypass_force_guest_role: bool = False,
    ) -> None:
        """Guarda un mensaje solo en RAM (sin persistir en Supabase)."""
        cid = self._clean_id(conversation_id)
        valid_roles = {"user", "assistant", "system", "tool", "guest", "bookai"}
        normalized_role = role if role in valid_roles else "assistant"
        resolved_channel = channel or self.get_flag(conversation_id, "default_channel")
        channel_to_store = resolved_channel or channel
        if normalized_role in {"assistant", "system", "tool"}:
            normalized_role = "bookai"
        elif normalized_role == "user":
            if not bypass_force_guest_role and self.get_flag(conversation_id, "force_guest_role"):
                normalized_role = "guest"
            else:
                normalized_role = "user"

        if normalized_role not in {"guest", "user", "bookai"}:
            normalized_role = "bookai"

        is_guest = normalized_role == "guest"
        if not client_name and is_guest:
            client_name = self.get_flag(cid, "client_name")

        entry = {
            "role": normalized_role,
            "content": content.strip(),
            "created_at": datetime.utcnow().isoformat(),
        }
        if escalation_id:
            entry["escalation_id"] = escalation_id
        if client_name and is_guest:
            entry["client_name"] = client_name
        if user_id is not None and str(user_id).strip() != "":
            try:
                entry["user_id"] = int(str(user_id).strip())
            except Exception:
                pass
        if user_first_name:
            entry["user_first_name"] = str(user_first_name)
        if user_last_name:
            entry["user_last_name"] = str(user_last_name)
        if user_last_name2:
            entry["user_last_name2"] = str(user_last_name2)
        if channel_to_store:
            entry["channel"] = channel_to_store
        if original_chat_id:
            entry["original_chat_id"] = original_chat_id

        self.runtime_memory.setdefault(cid, []).append(entry)
        if len(self.runtime_memory[cid]) > self.max_runtime_messages:
            self.runtime_memory[cid] = self.runtime_memory[cid][-self.max_runtime_messages:]

    # ----------------------------------------------------------------------
    def clear(self, conversation_id: str) -> None:
        """Limpia la memoria temporal de una conversación."""
        cid = self._clean_id(conversation_id)
        if cid in self.runtime_memory:
            del self.runtime_memory[cid]
            log.info(f"🧹 Memoria temporal limpiada para {cid}")
        if cid in self.state_flags:  # 🆕 limpiar flags también
            del self.state_flags[cid]
            log.info(f"🧹 Flags de estado limpiados para {cid}")

    # ----------------------------------------------------------------------
    def update_memory(self, conversation_id: str, role: str, content: str) -> None:
        """Alias retrocompatible de `save()` usado por agentes antiguos."""
        try:
            self.save(conversation_id=conversation_id, role=role, content=content)
        except Exception as e:
            log.warning(f"⚠️ Error en update_memory (alias de save): {e}")

    # ----------------------------------------------------------------------
    def get_memory_as_messages(self, conversation_id: str, limit: int = 30):
        """
        🔄 Devuelve la memoria en formato LangChain (HumanMessage / AIMessage / SystemMessage).
        """
        from langchain.schema import HumanMessage, AIMessage, SystemMessage

        try:
            raw_messages = self.get_memory(conversation_id, limit)
            messages = []

            for msg in raw_messages:
                role = msg.get("role", "assistant")
                content = msg.get("content", "")
                if not content:
                    continue

                if role == "guest":
                    messages.append(HumanMessage(content=content))
                elif role == "user":
                    # Mensajes del hotel/propietario: mantener rol user pero no como huésped.
                    messages.append(SystemMessage(content=f"Hotel: {content}"))
                elif role == "system":
                    messages.append(SystemMessage(content=content))
                else:
                    messages.append(AIMessage(content=content))

            log.debug(
                f"🧩 get_memory_as_messages → {len(messages)} mensajes convertidos para {conversation_id}"
            )
            return messages

        except Exception as e:
            log.error(f"⚠️ Error al convertir memoria a mensajes LangChain: {e}", exc_info=True)
            return []

    # ======================================================================
    # 🆕  MÉTODOS NUEVOS: Flags persistentes (estado de escalación, etc.)
    # ======================================================================
    def set_flag(self, conversation_id: str, flag_name: str, value: Any = True) -> None:
        """Marca un flag de estado (ej. escalación activa)."""
        cid = self._clean_id(conversation_id)
        self.state_flags.setdefault(cid, {})[flag_name] = value
        if flag_name == "property_id" and value is not None:
            pending_keys: list[str] = []
            if cid:
                pending_keys.append(cid)
            try:
                last_mem = self.get_flag(conversation_id, "last_memory_id")
            except Exception:
                last_mem = None
            if last_mem:
                pending_keys.append(self._clean_id(last_mem))
            seen: set[str] = set()
            for pending_key in pending_keys:
                key = str(pending_key or "").strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                pending_payload = self.state_flags.get(key, {}).get("pending_property_room_guest_message")
                if not isinstance(pending_payload, dict):
                    continue
                payload = dict(pending_payload)
                payload["property_id"] = value
                try:
                    from core.socket_manager import get_global_socket_manager

                    socket_mgr = get_global_socket_manager()
                except Exception:
                    socket_mgr = None
                emitted = False
                instance_id = (
                    self.get_flag(key, "instance_id")
                    or self.get_flag(key, "instance_hotel_code")
                )
                if socket_mgr and getattr(socket_mgr, "enabled", False):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(
                            socket_mgr.emit(
                                "chat.message.created",
                                payload,
                                rooms=f"property:{value}",
                                instance_id=instance_id,
                            )
                        )
                        emitted = True
                    except Exception as exc:
                        log.error(
                            "[chat.message.created] deferred emission failed for %s: %s",
                            key, exc, exc_info=True,
                        )
                if emitted and key in self.state_flags and "pending_property_room_guest_message" in self.state_flags[key]:
                    del self.state_flags[key]["pending_property_room_guest_message"]
                pending_list_payload = self.state_flags.get(key, {}).get("pending_property_room_chat_list_updated")
                if isinstance(pending_list_payload, dict):
                    list_payload = dict(pending_list_payload)
                    list_payload["property_id"] = value
                    original_chat_id = str(list_payload.pop("_original_chat_id", "") or "").strip()
                    chat_payload = list_payload.get("chat")
                    if isinstance(chat_payload, dict):
                        chat_payload = dict(chat_payload)
                        chat_payload["property_id"] = value
                        list_payload["chat"] = chat_payload
                    emitted_list = False
                    chat_id_for_visibility = str(
                        (chat_payload or {}).get("chat_id") if isinstance(chat_payload, dict) else key
                    ).strip() or str(key or "").strip()
                    channel_name = str(
                        (chat_payload or {}).get("channel") if isinstance(chat_payload, dict) else "whatsapp"
                    ).strip() or "whatsapp"
                    if not original_chat_id and ":" in str(key or ""):
                        original_chat_id = str(key).strip()
                    visible_after = is_chat_visible_in_list(
                        chat_id_for_visibility,
                        property_id=value,
                        channel=channel_name,
                        original_chat_id=original_chat_id or None,
                    )
                    if visible_after and socket_mgr and getattr(socket_mgr, "enabled", False):
                        try:
                            loop = asyncio.get_running_loop()
                            loop.create_task(
                                socket_mgr.emit(
                                    "chat.list.updated",
                                    list_payload,
                                    rooms=f"property:{value}",
                                    instance_id=instance_id,
                                )
                            )
                            emitted_list = True
                        except Exception as exc:
                            log.error(
                                "[chat.list.updated] deferred emission failed for %s: %s",
                                key, exc, exc_info=True,
                            )
                    if emitted_list and key in self.state_flags and "pending_property_room_chat_list_updated" in self.state_flags[key]:
                        del self.state_flags[key]["pending_property_room_chat_list_updated"]
        log.debug(f"🚩 Flag '{flag_name}' = {value} para {cid}")

    def get_flag(self, conversation_id: str, flag_name: str) -> Optional[Any]:
        """Recupera un flag de estado (None si no existe)."""
        cid = self._clean_id(conversation_id)
        return self.state_flags.get(cid, {}).get(flag_name)

    def clear_flag(self, conversation_id: str, flag_name: str) -> None:
        """Elimina un flag de estado."""
        cid = self._clean_id(conversation_id)
        if cid in self.state_flags and flag_name in self.state_flags[cid]:
            del self.state_flags[cid][flag_name]
            log.debug(f"🧹 Flag '{flag_name}' eliminado para {cid}")
