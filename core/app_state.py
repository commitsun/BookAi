"""Estado compartido y componentes iniciales del sistema."""

from __future__ import annotations

import logging
import os
import pickle
from collections import deque

from channels_wrapper.manager import ChannelManager
from agents.supervisor_input_agent import SupervisorInputAgent
from agents.supervisor_output_agent import SupervisorOutputAgent
from agents.interno_agent import InternoAgent
from agents.superintendente_agent import SuperintendenteAgent
from core.config import Settings
from core.template_registry import TemplateRegistry
from core.memory_manager import MemoryManager
from core.message_buffer import MessageBufferManager
from core.db import supabase

TRACK_FILE = "/tmp/escalation_tracking.pkl"


class AppState:
    """Contenedor liviano del estado global y dependencias compartidas."""

    def __init__(self, idle_seconds: float = 15.0):
        self.log = logging.getLogger("AppState")

        # Dependencias de agentes y canales
        self.memory_manager = MemoryManager()
        try:
            self.template_registry = TemplateRegistry.from_supabase(
                supabase, table=Settings.TEMPLATE_SUPABASE_TABLE
            )
        except Exception as exc:
            self.log.warning("No se pudo cargar registry desde Supabase: %s", exc)
            self.template_registry = TemplateRegistry()

        self.supervisor_input = SupervisorInputAgent(memory_manager=self.memory_manager)
        self.supervisor_output = SupervisorOutputAgent(memory_manager=self.memory_manager)
        self.channel_manager = ChannelManager(memory_manager=self.memory_manager)
        self.buffer_manager = MessageBufferManager(idle_seconds=idle_seconds)
        self.interno_agent = InternoAgent(memory_manager=self.memory_manager)
        self.supabase_client = supabase
        self.superintendente_agent = SuperintendenteAgent(
            memory_manager=self.memory_manager,
            supabase_client=self.supabase_client,
            channel_manager=self.channel_manager,
            template_registry=self.template_registry,
        )

        # Estado efímero de la sesión
        self.chat_lang: dict[str, str] = {}
        self.telegram_pending_confirmations: dict = {}
        self.telegram_pending_kb_addition: dict = {}
        self.telegram_pending_kb_removal: dict = {}
        self.superintendente_chats: dict = {}
        self.superintendente_pending_wa: dict = {}
        self.superintendente_pending_tpl: dict = {}
        self.superintendente_pending_review: dict = {}
        self.superintendente_pending_broadcast: dict = {}
        self.processed_whatsapp_ids: set[str] = set()
        self.processed_whatsapp_queue: deque[str] = deque(maxlen=5000)
        self.processed_template_keys: set[str] = set()
        self.processed_template_queue: deque[str] = deque(maxlen=2000)

        # Tracking mínimo persistente (retrocompatibilidad)
        self.tracking: dict = {}
        self.load_tracking()

    # ---------------------------------------------------------
    # Persistencia mínima para restaurar tracking tras reinicios
    # ---------------------------------------------------------
    def save_tracking(self):
        try:
            with open(TRACK_FILE, "wb") as f:
                pickle.dump(self.tracking, f)
        except Exception as exc:
            self.log.warning("No se pudo guardar tracking: %s", exc)

    def load_tracking(self):
        if not os.path.exists(TRACK_FILE):
            return
        try:
            with open(TRACK_FILE, "rb") as f:
                self.tracking.update(pickle.load(f))
            self.log.info("Tracking restaurado (%s items)", len(self.tracking))
        except Exception as exc:
            self.log.warning("No se pudo cargar tracking: %s", exc)
