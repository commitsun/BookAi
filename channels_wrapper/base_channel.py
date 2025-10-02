class BaseChannel:
    def send_message(self, user_id: str, text: str):
        raise NotImplementedError

    def receive_message(self, payload: dict):
        raise NotImplementedError
