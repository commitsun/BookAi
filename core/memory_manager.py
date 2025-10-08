import time
from datetime import datetime, timedelta
from typing import List, Dict, Any
from core.db import get_conversation_history, save_message


class MemoryManager:
    """
    Memoria híbrida (runtime + persistente).
    - Mantiene los últimos turnos recientes en RAM por conversation_id.
    - Consulta Supabase solo si no hay suficiente contexto en RAM.
    - Limita por nº de mensajes y antigüedad (por fecha).
    """

    def __init__(self, max_runtime_messages: int = 8, db_history_days: int = 7):
        """
        max_runtime_messages: número de mensajes a mantener en memoria RAM por conversación.
        db_history_days: días hacia atrás que se consultan desde Supabase.
        """
        self.runtime_memory: Dict[str, List[Dict[str, Any]]] = {}
        self.max_runtime_messages = max_runtime_messages
        self.db_history_days = db_history_days

    # -------------------------------
    # Normaliza IDs de conversación
    # -------------------------------
    def _clean_id(self, conversation_id: str) -> str:
        return str(conversation_id).replace("+", "").strip()

    # -------------------------------
    # Leer contexto combinado
    # -------------------------------
    def get_context(self, conversation_id: str, limit: int = 10) -> List[dict]:
        """
        Devuelve los mensajes recientes combinando:
        - memoria local (RAM)
        - histórico en Supabase (solo últimos X días)
        """
        cid = self._clean_id(conversation_id)

        # Recuperar memoria local
        local_msgs = self.runtime_memory.get(cid, [])
        needed = max(0, limit - len(local_msgs))

        db_msgs = []
        if needed > 0:
            since = datetime.utcnow() - timedelta(days=self.db_history_days)
            db_msgs = get_conversation_history(cid, limit=needed, since=since)

        # Normalizar timestamps (pueden venir como float o ISO)
        def parse_ts(msg):
            ts = msg.get("created_at")
            if isinstance(ts, (int, float)):
                return float(ts)
            try:
                return datetime.fromisoformat(ts).timestamp()
            except Exception:
                return time.time()

        combined = (db_msgs or []) + (local_msgs or [])
        combined_sorted = sorted(combined, key=parse_ts)
        return combined_sorted[-limit:]

    # -------------------------------
    # Guardar en RAM + Supabase
    # -------------------------------
    def save(self, conversation_id: str, role: str, content: str):
        """
        Guarda el mensaje en memoria temporal (RAM) y en Supabase.
        """
        cid = self._clean_id(conversation_id)

        entry = {
            "role": role,
            "content": content,
            "created_at": time.time(),
        }

        # Guardar en memoria RAM
        self.runtime_memory.setdefault(cid, []).append(entry)
        if len(self.runtime_memory[cid]) > self.max_runtime_messages:
            self.runtime_memory[cid] = self.runtime_memory[cid][-self.max_runtime_messages:]

        # Guardar persistente (Supabase)
        try:
            save_message(cid, role, content)
        except Exception as e:
            print(f"⚠️ No se pudo guardar en Supabase: {e}")

    # -------------------------------
    # Limpiar memoria local
    # -------------------------------
    def clear(self, conversation_id: str):
        """
        Elimina la memoria temporal de una conversación (RAM).
        No borra la base de datos.
        """
        cid = self._clean_id(conversation_id)
        if cid in self.runtime_memory:
            del self.runtime_memory[cid]
