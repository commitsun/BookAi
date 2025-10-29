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
        """
        cid = self._clean_id(conversation_id)
        entry = {
            "role": role,
            "content": content,
            "created_at": datetime.utcnow().isoformat(),
        }

        # Guardar en RAM
        self.runtime_memory.setdefault(cid, []).append(entry)
        if len(self.runtime_memory[cid]) > self.max_runtime_messages:
            self.runtime_memory[cid] = self.runtime_memory[cid][-self.max_runtime_messages:]

        # Guardar en Supabase
        try:
            save_message(cid, role, content)
            log.debug(f"💾 Guardado en Supabase: ({cid}, {role})")
        except Exception as e:
            log.warning(f"⚠️ No se pudo guardar mensaje en Supabase ({cid}): {e}")

    # ----------------------------------------------------------------------
    def clear(self, conversation_id: str) -> None:
        """
        Limpia la memoria temporal de una conversación.
        """
        cid = self._clean_id(conversation_id)
        if cid in self.runtime_memory:
            del self.runtime_memory[cid]
            log.info(f"🧹 Memoria temporal limpiada para {cid}")
