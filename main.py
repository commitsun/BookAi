"""
🚀 Main Entry Point - Sistema de Agentes con Orquestación + Idioma + Buffer
Mantiene el comportamiento previo moviendo la lógica pesada a módulos dedicados.
"""

import logging
import warnings

from fastapi import FastAPI

from core.app_state import AppState
from core.pipeline import process_user_message  # re-export para compatibilidad
from channels_wrapper.telegram.webhook_telegram import register_telegram_routes
from channels_wrapper.whatsapp.webhook_whatsapp import register_whatsapp_routes
from api.template_routes import register_template_routes

# =============================================================
# CONFIG GLOBAL / LOGGING
# =============================================================

warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=UserWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

for noisy_logger in ("langsmith", "langsmith.client"):
    logging.getLogger(noisy_logger).setLevel(logging.ERROR)

log = logging.getLogger("Main")

# =============================================================
# FASTAPI APP
# =============================================================

app = FastAPI(title="HotelAI - Sistema de Agentes ReAct v4")

# =============================================================
# ESTADO COMPARTIDO
# =============================================================

state = AppState()
log.info("✅ Sistema inicializado con agentes y estado compartido")

# =============================================================
# WEBHOOKS REGISTRADOS
# =============================================================

register_whatsapp_routes(app, state)
register_telegram_routes(app, state)
register_template_routes(app, state)

# =============================================================
# HEALTHCHECK
# =============================================================


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "version": "v4-react",
        "description": "Sistema de agentes con InternoAgent ReAct + buffer WhatsApp",
    }


# =============================================================
# LOCAL DEV
# =============================================================

if __name__ == "__main__":
    import uvicorn

    log.info("🚀 Iniciando servidor con InternoAgent ReAct...")
    uvicorn.run(app, host="0.0.0.0", port=8000)
