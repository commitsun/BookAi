# channels_wrapper/manager.py
import importlib
import inspect
import os
import traceback

class ChannelManager:
    """
    Carga y gestiona dinámicamente los canales (WhatsApp, Telegram, etc.)
    desde la carpeta `channels_wrapper/`.

    Cada canal debe tener una clase que herede de BaseChannel y aceptar
    el parámetro `openai_api_key` en su constructor.
    """

    def __init__(self):
        self.channels = {}
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self._load_channels()

    # ------------------------------------------------------------------
    # 📦 Carga dinámica de canales
    # ------------------------------------------------------------------
    def _load_channels(self):
        """
        Busca e importa dinámicamente los canales disponibles en 
        `channels_wrapper/`. Cada canal debe heredar de BaseChannel.
        """
        possible_channels = {
            "whatsapp": "channels_wrapper.whatsapp.whatsapp_meta",
             "telegram": "channels_wrapper.telegram.telegram_channel",
            # Si quieres añadir más:
            # "webchat": "channels_wrapper.webchat.webchat_channel",
        }

        for name, module_path in possible_channels.items():
            try:
                module = importlib.import_module(module_path)

                # Buscar clase que herede de BaseChannel
                channel_class = None
                for _, cls in inspect.getmembers(module, inspect.isclass):
                    if cls.__module__ == module_path:
                        channel_class = cls
                        break

                if not channel_class:
                    print(f"⚠️ No se encontró clase de canal válida en {module_path}")
                    continue

                # Instanciar canal con la API key
                self.channels[name] = channel_class(openai_api_key=self.openai_api_key)
                print(f"✅ Canal '{name}' cargado correctamente desde {module_path}")

            except Exception as e:
                print(f"⚠️ Error cargando canal '{name}': {e}")
                traceback.print_exc()

    # ------------------------------------------------------------------
    # 🔌 Registro de canales en FastAPI
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

                channel.register_routes(app)
                print(f"🔗 Canal '{name}' registrado en FastAPI correctamente.")

            except Exception as e:
                print(f"⚠️ Error registrando canal '{name}' en FastAPI: {e}")
                traceback.print_exc()