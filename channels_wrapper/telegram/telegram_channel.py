from channels_wrapper.base_channel import BaseChannel

class TelegramChannel(BaseChannel):
    def __init__(self):
        super().__init__(openai_api_key="OPENAI_API_KEY")

    def register_routes(self, app):
        pass  # Aquí /webhook/telegram

    def send_message(self, user_id: str, text: str):
        pass  # Implementación API Telegram

    def extract_message_data(self, payload: dict):
        pass  # Adaptar al formato de Telegram