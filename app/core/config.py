from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    database_url: str  # postgresql+asyncpg://user:pass@host/db

    # Socket.IO
    socket_cors_origins: list[str] = []

    # App
    debug: bool = False

    # OpenAI API key for system-level services (Whisper transcription, vision)
    openai_api_key: str | None = None


settings = Settings()
