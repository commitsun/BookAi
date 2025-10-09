import time
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any
from core.db import get_conversation_history, save_message


class MemoryManager:
    """
    🧠 Memoria híbrida (runtime + persistente via Supabase).

    - Guarda los últimos turnos recientes en RAM por conversation_id.
    - Consulta Supabase en cada mensaje para mantener el contexto persistente.
    - Combina automáticamente los mensajes de RAM + DB.
    - Limita por número de mensajes recientes y antigüedad (por fecha).
    """

    def __init__(self, max_runtime_messages: int = 12, db_history_days: int = 7):
        self.runtime_memory: Dict[str, List[Dict[str, Any]]] = {}
        self.max_runtime_messages = max_runtime_messages
        self.db_history_days = db_history_days

    def _clean_id(self, conversation_id: str) -> str:
        return str(conversation_id).replace("+", "").strip()

    def get_context(self, conversation_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        cid = self._clean_id(conversation_id)

        try:
            # 🧩 Mensajes en RAM
            local_msgs = self.runtime_memory.get(cid, [])

            # 💾 Mensajes en DB (últimos X días)
            since = datetime.utcnow() - timedelta(days=self.db_history_days)
            db_msgs = get_conversation_history(cid, limit=limit, since=since) or []

            # 🧠 Mezclar y ordenar
            combined = db_msgs + local_msgs

            def parse_ts(msg):
                ts = msg.get("created_at")
                if isinstance(ts, (int, float)):
                    return float(ts)
                try:
                    return datetime.fromisoformat(ts).timestamp()
                except Exception:
                    return time.time()

            combined_sorted = sorted(combined, key=parse_ts)
            recent = combined_sorted[-limit:]

            logging.info(
                f"🧠 Contexto cargado para {cid}: {len(recent)} mensajes "
                f"(RAM={len(local_msgs)}, DB={len(db_msgs)})"
            )
            return recent

        except Exception as e:
            logging.error(f"⚠️ Error recuperando contexto de {cid}: {e}", exc_info=True)
            return []

    def save(self, conversation_id: str, role: str, content: str):
        cid = self._clean_id(conversation_id)
        entry = {
            "role": role,
            "content": content,
            "created_at": time.time(),
        }

        self.runtime_memory.setdefault(cid, []).append(entry)
        if len(self.runtime_memory[cid]) > self.max_runtime_messages:
            self.runtime_memory[cid] = self.runtime_memory[cid][-self.max_runtime_messages:]

        try:
            save_message(cid, role, content)
        except Exception as e:
            logging.warning(f"⚠️ No se pudo guardar mensaje en Supabase ({cid}): {e}")

    def clear(self, conversation_id: str):
        cid = self._clean_id(conversation_id)
        if cid in self.runtime_memory:
            del self.runtime_memory[cid]
            logging.info(f"🧹 Memoria temporal limpiada para {cid}")
