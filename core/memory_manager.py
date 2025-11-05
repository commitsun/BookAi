import time
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
    """

    def __init__(self, max_runtime_messages: int = 12, db_history_days: int = 7):
        self.runtime_memory: Dict[str, List[Dict[str, Any]]] = {}
        self.max_runtime_messages = max_runtime_messages
        self.db_history_days = db_history_days

    # ----------------------------------------------------------------------
    def _clean_id(self, conversation_id: str) -> str:
        """Normaliza el ID (quita '+' y espacios)."""
        return str(conversation_id).replace("+", "").strip()

    # ----------------------------------------------------------------------
    def get_memory(self, conversation_id: str, limit: int = 10) -> List[Dict[str, Any]]:
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
            db_msgs = get_conversation_history(cid, limit=limit, since=since) or []

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
    def save(self, conversation_id: str, role: str, content: str) -> None:
        """
        Guarda un mensaje tanto en memoria local como en Supabase.
        Cumple con la restricción de Supabase (role ∈ {'user','assistant','system','tool'}).
        No añade etiquetas ni prefijos en el contenido.
        """
        cid = self._clean_id(conversation_id)

        # Normaliza el rol
        valid_roles = {"user", "assistant", "system", "tool"}
        normalized_role = role if role in valid_roles else "assistant"

        entry = {
            "role": normalized_role,
            "content": content.strip(),
            "created_at": datetime.utcnow().isoformat(),
        }

        # Guardar en RAM
        self.runtime_memory.setdefault(cid, []).append(entry)
        if len(self.runtime_memory[cid]) > self.max_runtime_messages:
            self.runtime_memory[cid] = self.runtime_memory[cid][-self.max_runtime_messages:]

        # Guardar en Supabase
        try:
            save_message(cid, normalized_role, entry["content"])
            log.debug(f"💾 Guardado en Supabase: ({cid}, {normalized_role})")
        except Exception as e:
            log.warning(f"⚠️ Error guardando mensaje en Supabase: {e}")


    # ----------------------------------------------------------------------
    def clear(self, conversation_id: str) -> None:
        """
        Limpia la memoria temporal de una conversación.
        """
        cid = self._clean_id(conversation_id)
        if cid in self.runtime_memory:
            del self.runtime_memory[cid]
            log.info(f"🧹 Memoria temporal limpiada para {cid}")
    # ----------------------------------------------------------------------
    def update_memory(self, conversation_id: str, role: str, content: str) -> None:
        """
        Alias retrocompatible de `save()` usado por los agentes antiguos (InfoAgent, etc.)
        Permite guardar mensajes en memoria sin romper compatibilidad.
        """
        try:
            self.save(conversation_id=conversation_id, role=role, content=content)
        except Exception as e:
            log.warning(f"⚠️ Error en update_memory (alias de save): {e}")

    # ----------------------------------------------------------------------
    def get_memory_as_messages(self, conversation_id: str, limit: int = 10):
        """
        🔄 Devuelve la memoria en formato LangChain (HumanMessage / AIMessage / SystemMessage).

        Esto permite que los agentes puedan reconstruir el contexto correctamente
        sin errores de tipo (dict → ChatMessage).
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

                if role == "user":
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
