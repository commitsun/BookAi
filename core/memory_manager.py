import time
import re
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from core.db import get_conversation_history, save_message

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
        return self.get_flag(conversation_id, "property_id") or self.get_flag(
            conversation_id,
            "pms_property_id",
        )

    def _resolve_history_table(self, conversation_id: str) -> str:
        table = self.get_flag(conversation_id, "history_table")
        return str(table).strip() if table else "chat_history"

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
        channel: Optional[str] = None,
        original_chat_id: Optional[str] = None,
        bypass_force_guest_role: bool = False,
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

        entry = {
            "role": normalized_role,
            "content": content.strip(),
            "created_at": datetime.utcnow().isoformat(),
        }
        if escalation_id:
            entry["escalation_id"] = escalation_id
        if client_name and is_guest:
            entry["client_name"] = client_name
        if channel_to_store:
            entry["channel"] = channel_to_store

        # Guardar en RAM
        self.runtime_memory.setdefault(cid, []).append(entry)
        if len(self.runtime_memory[cid]) > self.max_runtime_messages:
            self.runtime_memory[cid] = self.runtime_memory[cid][-self.max_runtime_messages:]

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
                channel=channel_to_store,
                property_id=property_id,
                original_chat_id=original_chat_id or cid,
                table=self._resolve_history_table(conversation_id),
            )
            log.debug(f"💾 Guardado en Supabase: ({cid}, {normalized_role})")
        except Exception as e:
            log.warning(f"⚠️ Error guardando mensaje en Supabase: {e}")

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

                if role in {"user", "guest"}:
                    messages.append(HumanMessage(content=content))
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
