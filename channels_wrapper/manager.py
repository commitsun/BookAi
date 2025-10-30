import importlib
import inspect
import os
import traceback
import logging
import asyncio

log = logging.getLogger("ChannelManager")


class ChannelManager:
    """
    Administra los canales (WhatsApp, Telegram, etc.)
    cargándolos dinámicamente desde `channels_wrapper/`.
    Cada canal debe heredar de BaseChannel y aceptar `openai_api_key` en su constructor.
    """

    def __init__(self):
        self.channels = {}
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self._load_channels()

    # ------------------------------------------------------------------
    # 📦 Carga dinámica de canales
    # ------------------------------------------------------------------
    def _load_channels(self):
        """Carga los módulos de canal disponibles."""
        possible_channels = {
            "whatsapp": "channels_wrapper.whatsapp.whatsapp_meta",
            "telegram": "channels_wrapper.telegram.telegram_channel",
            # puedes añadir más canales aquí
        }

        for name, module_path in possible_channels.items():
            try:
                module = importlib.import_module(module_path)

                # Buscar la clase del canal
                channel_class = next(
                    (
                        cls
                        for _, cls in inspect.getmembers(module, inspect.isclass)
                        if cls.__module__ == module_path
                    ),
                    None,
                )

                if not channel_class:
                    log.warning(f"⚠️ No se encontró clase válida para canal '{name}' en {module_path}")
                    continue

                # Instanciar canal
                channel_instance = channel_class(openai_api_key=self.openai_api_key)
                self.channels[name] = channel_instance
                log.info(f"✅ Canal '{name}' cargado desde {module_path}")

            except Exception as e:
                log.error(f"❌ Error cargando canal '{name}' ({module_path}): {e}", exc_info=True)

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
                    log.info(f"🔗 Canal '{name}' registrado en FastAPI.")
                else:
                    log.warning(f"⚠️ Canal '{name}' no implementa register_routes().")

            except Exception as e:
                log.error(f"❌ Error registrando canal '{name}': {e}", exc_info=True)

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
                raise ValueError(f"Canal no encontrado: {channel}")

            send_fn = getattr(channel_obj, "send_message", None)
            if not send_fn:
                raise AttributeError(f"El canal '{channel}' no implementa send_message().")

            if asyncio.iscoroutinefunction(send_fn):
                await send_fn(chat_id, message)
            else:
                send_fn(chat_id, message)

            log.info(f"📤 [{channel}] Mensaje enviado a {chat_id}: {message[:80]}...")

        except Exception as e:
            log.error(f"❌ Error enviando mensaje a {channel}: {e}", exc_info=True)
