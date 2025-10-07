from channels_wrapper.whatsapp.whatsapp_meta import WhatsAppChannel
# from channels_wrapper.telegram.telegram_channel import TelegramChannel

class ChannelManager:
    """Gestiona todos los canales disponibles."""

    def __init__(self):
        self.channels = {
            "whatsapp": WhatsAppChannel(),
            # "telegram": TelegramChannel(),
        }

    def get_channel(self, name: str):
        return self.channels.get(name)
