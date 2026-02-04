"""
core/config.py
====================================================
Configuración centralizada de entorno y modelos LLM.
Lee todo desde el archivo .env al inicio del sistema.
====================================================
"""

import os
from enum import Enum
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

# Cargar variables del .env
load_dotenv()


# =============================================================
# ⚙️ CONFIGURACIÓN GENERAL (.env)
# =============================================================
class Settings:
    """Variables de entorno globales accesibles desde todo el sistema."""

    # Claves API
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    LANGCHAIN_API_KEY = os.getenv("LANGCHAIN_API_KEY")
    LANGCHAIN_ENDPOINT = os.getenv("LANGCHAIN_ENDPOINT")
    LANGCHAIN_PROJECT = os.getenv("LANGCHAIN_PROJECT")

    # WhatsApp / Meta
    WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
    WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
    WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")

    # Telegram / encargado
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

    # Supabase / almacenamiento
    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY")
    TEMP_KB_TABLE = os.getenv("TEMP_KB_TABLE", "kb_daily_cache")

    # AWS / S3
    S3_BUCKET = os.getenv("S3_BUCKET")
    AWS_DEFAULT_REGION = os.getenv("AWS_DEFAULT_REGION")

    # Roomdoo / PMS
    ROOMDOO_BEARER_TOKEN = os.getenv("ROOMDOO_BEARER_TOKEN")
    ROOMDOO_LOGIN_URL = os.getenv("ROOMDOO_LOGIN_URL")
    ROOMDOO_USERNAME = os.getenv("ROOMDOO_USERNAME")
    ROOMDOO_PASSWORD = os.getenv("ROOMDOO_PASSWORD")
    ROOMDOO_AVAIL_URL = os.getenv("ROOMDOO_AVAIL_URL")
    ROOMDOO_PMS_PROPERTY_ID = os.getenv("ROOMDOO_PMS_PROPERTY_ID")

    # MCP / Infraestructura
    ENDPOINT_MCP = os.getenv("ENDPOINT_MCP")

    # CORS / Frontend
    # Comma-separated list, "*" for all. Example: "https://app.example.com,http://localhost:3000"
    CORS_ORIGINS = os.getenv("CORS_ORIGINS", "")

    # Control de modelos (usado por ModelConfig)
    MODEL_MAIN = os.getenv("MODEL_MAIN", "gpt-4.1")
    MODEL_SUBAGENT = os.getenv("MODEL_SUBAGENT", "gpt-4.1")
    MODEL_SUPERVISOR = os.getenv("MODEL_SUPERVISOR", "gpt-4.1")
    MODEL_INTERNAL = os.getenv("MODEL_INTERNAL", "gpt-4.1")

    TEMP_MAIN = float(os.getenv("TEMP_MAIN", "0.3"))
    TEMP_SUBAGENT = float(os.getenv("TEMP_SUBAGENT", "0.2"))
    TEMP_SUPERVISOR = float(os.getenv("TEMP_SUPERVISOR", "0.2"))
    TEMP_INTERNAL = float(os.getenv("TEMP_INTERNAL", "0.2"))
    TEMP_SUPERINTENDENTE = float(os.getenv("SUPERINTENDENTE_TEMP", "0.2"))

    SUPERINTENDENTE_MODEL = os.getenv("SUPERINTENDENTE_MODEL", MODEL_INTERNAL)
    SUPERINTENDENTE_S3_PREFIX = os.getenv("SUPERINTENDENTE_S3_PREFIX", "")
    SUPERINTENDENTE_S3_DOC = os.getenv("SUPERINTENDENTE_S3_DOC", "")
    SUPERINTENDENTE_HISTORY_TABLE = os.getenv(
        "SUPERINTENDENTE_HISTORY_TABLE",
        "superintendente_history",
    )

    # Plantillas WhatsApp
    TEMPLATE_SUPABASE_TABLE = os.getenv("TEMPLATE_SUPABASE_TABLE", "whatsapp_templates")

    # Reservas por chat
    CHAT_RESERVATIONS_TABLE = os.getenv("CHAT_RESERVATIONS_TABLE", "chat_reservations")


# =============================================================
# ⚙️ ENUM DE TIER
# =============================================================
class ModelTier(str, Enum):
    MAIN = "main"              # Orquestador principal
    SUBAGENT = "subagent"      # Subagentes (InfoAgent, DispoPreciosAgent)
    SUPERVISOR = "supervisor"  # Validadores Input/Output
    INTERNAL = "internal"      # Escalaciones internas
    SUPERINTENDENTE = "superintendente"  # Gestión de conocimiento/estrategia


# =============================================================
# 🧠 CONFIGURACIÓN CENTRALIZADA DE MODELOS LLM
# =============================================================
class ModelConfig:
    """
    Configuración centralizada de modelos LLM.
    Lee todo desde Settings (.env) y genera objetos ChatOpenAI uniformes.
    """

    MODELS = {
        ModelTier.MAIN: {
            "name": Settings.MODEL_MAIN,
            "temperature": Settings.TEMP_MAIN,
        },
        ModelTier.SUBAGENT: {
            "name": Settings.MODEL_SUBAGENT,
            "temperature": Settings.TEMP_SUBAGENT,
        },
        ModelTier.SUPERVISOR: {
            "name": Settings.MODEL_SUPERVISOR,
            "temperature": Settings.TEMP_SUPERVISOR,
        },
        ModelTier.INTERNAL: {
            "name": Settings.MODEL_INTERNAL,
            "temperature": Settings.TEMP_INTERNAL,
        },
        ModelTier.SUPERINTENDENTE: {
            "name": Settings.SUPERINTENDENTE_MODEL,
            "temperature": Settings.TEMP_SUPERINTENDENTE,
        },
    }

    @classmethod
    def get_model(cls, tier: ModelTier) -> tuple[str, float]:
        """Retorna (model_name, temperature) para el tier solicitado."""
        config = cls.MODELS.get(tier)
        if not config:
            raise ValueError(f"Tier desconocido: {tier}")
        return config["name"], config["temperature"]

    @classmethod
    def get_llm(cls, tier: ModelTier) -> ChatOpenAI:
        """Devuelve un ChatOpenAI configurado con el modelo y temperatura del tier."""
        name, temp = cls.get_model(tier)
        return ChatOpenAI(model=name, temperature=temp)


# =============================================================
# 🔍 FUNCIÓN DE DEBUG OPCIONAL
# =============================================================
def print_model_summary():
    """Imprime los modelos activos por tier (útil en desarrollo)."""
    print("\n✅ MODELOS LLM ACTIVOS\n" + "=" * 40)
    for tier, conf in ModelConfig.MODELS.items():
        print(f"{tier.value.upper():<12} → {conf['name']} (temp {conf['temperature']})")
    print("=" * 40 + "\n")


# =============================================================
# 🧪 TEST LOCAL OPCIONAL
# =============================================================
if __name__ == "__main__":
    print_model_summary()
