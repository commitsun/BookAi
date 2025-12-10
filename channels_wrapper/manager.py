import importlib
import inspect
import os
import traceback
import logging
import asyncio
import time
from channels_wrapper.base_channel import BaseChannel  # 👈 Verificación de herencia

log = logging.getLogger("ChannelManager")


class ChannelManager:
    """
    Administra los canales (WhatsApp, Telegram, etc.)
    cargándolos dinámicamente desde `channels_wrapper/`.
    Cada canal debe heredar de BaseChannel y aceptar `openai_api_key` en su constructor.
    """

    def __init__(self, memory_manager=None):
        self.channels = {}
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self._recent_sends = {}  # {(channel, chat_id): (message, timestamp)}
        self._dedup_window = 8.0
        self.memory_manager = memory_manager
        self._load_channels()

    # ------------------------------------------------------------------
    # 📦 Carga dinámica de canales
    # ------------------------------------------------------------------
    def _load_channels(self):
        """Carga los módulos de canal disponibles."""
        possible_channels = {
            "whatsapp": "channels_wrapper.whatsapp.whatsapp_meta",
            "telegram": "channels_wrapper.telegram.telegram_channel",
        }

        for name, module_path in possible_channels.items():
            try:
                module = importlib.import_module(module_path)

                # Buscar la clase que herede de BaseChannel
                channel_class = next(
                    (
                        cls
                        for _, cls in inspect.getmembers(module, inspect.isclass)
                        if issubclass(cls, BaseChannel) and cls is not BaseChannel
                    ),
                    None,
                )

                if not channel_class:
                    log.warning(f"⚠️ No se encontró clase válida para canal '{name}' en {module_path}")
                    continue

                # Instanciar canal
                channel_instance = channel_class(openai_api_key=self.openai_api_key)
                self.channels[name] = channel_instance
                log.info(f"✅ Canal '{name}' cargado correctamente desde {module_path}")

            except Exception as e:
                log.error(f"❌ Error cargando canal '{name}' ({module_path}): {e}", exc_info=True)

        if not self.channels:
            log.warning("⚠️ No se cargó ningún canal. Verifica los módulos en channels_wrapper/*")

    # ------------------------------------------------------------------
    # 🔌 Registro en FastAPI
    # ------------------------------------------------------------------
    def register_all(self, app, hybrid_agent=None):
        """
        Registra todos los canales en la app FastAPI.
        Si se pasa un `hybrid_agent`, se inyecta en cada canal.
        """
        for name, channel in self.channels.items():
            try:
                if hybrid_agent:
                    channel.agent = hybrid_agent

                if hasattr(channel, "register_routes"):
                    channel.register_routes(app)
                    log.info(f"🔗 Canal '{name}' registrado correctamente en FastAPI.")
                else:
                    log.warning(f"⚠️ Canal '{name}' no implementa register_routes().")

            except Exception as e:
                log.error(f"💥 Error registrando canal '{name}': {e}", exc_info=True)

    # ------------------------------------------------------------------
    # 💬 Envío de mensajes
    # ------------------------------------------------------------------
    async def send_message(self, chat_id: str, message: str, channel: str = "whatsapp"):
        """
        Envía un mensaje al canal especificado (WhatsApp, Telegram, etc.).
        Soporta métodos síncronos y asíncronos.
        """
        try:
            channel_obj = self.channels.get(channel)
            if not channel_obj:
                raise ValueError(f"Canal no encontrado o no cargado: {channel}")

            # 👇 Fuerza el chat_id correcto para Telegram
            if channel == "telegram":
                from core.config import Settings as C
                # Solo usa el chat de entorno si no se proporcionó ninguno
                chat_id = chat_id or C.TELEGRAM_CHAT_ID

            # 🛑 Filtro anti-duplicados de salida (mensajes idénticos en pocos segundos)
            msg_norm = (message or "").strip()
            key = (channel, chat_id)
            last = self._recent_sends.get(key)
            now = time.monotonic()
            if last:
                last_msg, ts = last
                if msg_norm and msg_norm == last_msg and (now - ts) < self._dedup_window:
                    log.info("↩️ Envío duplicado evitado (%s → %s)", channel, chat_id)
                    return
            self._recent_sends[key] = (msg_norm, now)

            send_fn = getattr(channel_obj, "send_message", None)
            if not send_fn:
                raise AttributeError(f"El canal '{channel}' no implementa send_message().")

            if asyncio.iscoroutinefunction(send_fn):
                await send_fn(chat_id, message)
            else:
                send_fn(chat_id, message)

            log.info(f"📤 [{channel}] Mensaje enviado a {chat_id}: {message[:80]}...")

        except Exception as e:
            log.error(f"❌ Error enviando mensaje por canal '{channel}': {e}", exc_info=True)


    # ------------------------------------------------------------------
    # 💬 Envío de plantillas (WhatsApp)
    # ------------------------------------------------------------------
    async def send_template_message(
        self,
        chat_id: str,
        template_id: str,
        parameters: dict | list | None = None,
        *,
        language: str = "es",
        channel: str = "whatsapp",
    ):
        """
        Envía una plantilla preaprobada (ej: WhatsApp).
        Aplica deduplicación ligera para evitar reenvíos repetidos en pocos segundos.
        """
        try:
            channel_obj = self.channels.get(channel)
            if not channel_obj:
                raise ValueError(f"Canal no encontrado o no cargado: {channel}")

            send_fn = getattr(channel_obj, "send_template_message", None)
            if not send_fn:
                raise AttributeError(f"El canal '{channel}' no implementa send_template_message().")

            payload_hash = f"{template_id}|{parameters}"
            key = (channel, chat_id, "template")
            last = self._recent_sends.get(key)
            now = time.monotonic()
            if last:
                last_hash, ts = last
                if payload_hash == last_hash and (now - ts) < self._dedup_window:
                    log.info("↩️ Envío de plantilla duplicado evitado (%s → %s)", channel, chat_id)
                    return
            self._recent_sends[key] = (payload_hash, now)

            result = None
            if asyncio.iscoroutinefunction(send_fn):
                result = await send_fn(chat_id, template_id, parameters=parameters, language=language)
            else:
                result = send_fn(chat_id, template_id, parameters=parameters, language=language)

            # Considera éxito si no devuelve nada o es truthy
            ok = True if result is None else bool(result)
            if not ok:
                raise RuntimeError(f"El canal '{channel}' no confirmó el envío de la plantilla.")

            log.info("📤 [%s] Plantilla '%s' enviada a %s", channel, template_id, chat_id)
        except Exception as e:
            log.error(f"❌ Error enviando plantilla por canal '{channel}': {e}", exc_info=True)
            raise


    # ------------------------------------------------------------------
    # 🧩 Utilidad: listar canales activos
    # ------------------------------------------------------------------
    def list_channels(self):
        """Devuelve la lista de canales cargados actualmente."""
        return list(self.channels.keys())
