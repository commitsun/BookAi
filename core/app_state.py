"""Estado compartido y componentes iniciales del sistema."""

from __future__ import annotations

import logging
import os
import pickle
from collections import deque
from typing import Optional

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


# Contenedor liviano del estado global y dependencias compartidas.
# Se usa en el flujo de estado compartido de aplicación y persistencia de tracking como pieza de organización, contrato de datos o punto de extensión.
# Sus instancias concentran flags o buffers de estado que otros componentes consultan durante el flujo.
# No produce efectos por sí sola; sirve como estructura tipada para mover información entre capas.
class AppState:
    """Contenedor liviano del estado global y dependencias compartidas."""

    # Inicializa el estado interno y las dependencias de `AppState`.
    # Se usa dentro de `AppState` en el flujo de estado compartido de aplicación y persistencia de tracking.
    # Recibe `idle_seconds` como entrada principal según la firma.
    # No devuelve valor; deja la instancia preparada con sus dependencias y estado inicial. Puede consultar o escribir en base de datos.
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
        env_idle = os.getenv("MESSAGE_BUFFER_IDLE_SECONDS", "").strip()
        try:
            effective_idle = float(env_idle) if env_idle else float(idle_seconds)
        except Exception:
            effective_idle = float(idle_seconds)
        self.buffer_manager = MessageBufferManager(idle_seconds=effective_idle)
        self.log.info("🕒 Message buffer idle_seconds=%.2f", effective_idle)
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
        self._tracking_mtime: Optional[float] = None
        self.load_tracking()

    # Persiste el tracking.
    # Se usa dentro de `AppState` en el flujo de estado compartido de aplicación y persistencia de tracking.
    # No recibe parámetros externos; trabaja con estado capturado por el cierre o atributos de instancia.
    # No devuelve un valor de negocio; deja aplicado el cambio de estado o registro correspondiente. Sin efectos secundarios relevantes.
    def save_tracking(self):
        try:
            with open(TRACK_FILE, "wb") as f:
                pickle.dump(self.tracking, f)
            try:
                self._tracking_mtime = os.path.getmtime(TRACK_FILE)
            except Exception:
                self._tracking_mtime = None
        except Exception as exc:
            self.log.warning("No se pudo guardar tracking: %s", exc)

    # Carga el tracking.
    # Se usa dentro de `AppState` en el flujo de estado compartido de aplicación y persistencia de tracking.
    # No recibe parámetros externos; trabaja con estado capturado por el cierre o atributos de instancia.
    # No devuelve un valor relevante; deja preparado el estado o ejecuta la acción necesaria. Sin efectos secundarios relevantes.
    def load_tracking(self):
        if not os.path.exists(TRACK_FILE):
            return
        try:
            current_mtime = os.path.getmtime(TRACK_FILE)
            if self._tracking_mtime is not None and current_mtime == self._tracking_mtime:
                return
            with open(TRACK_FILE, "rb") as f:
                loaded = pickle.load(f)
            self.tracking = loaded if isinstance(loaded, dict) else {}
            self._tracking_mtime = current_mtime
            self.log.info("Tracking restaurado (%s items)", len(self.tracking))
        except Exception as exc:
            self.log.warning("No se pudo cargar tracking: %s", exc)
