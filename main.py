import os
import warnings
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from channels_wrapper.manager import ChannelManager
from channels_wrapper.telegram.telegram_channel import TelegramChannel
from core.main_agent import HotelAIHybrid
from core.escalation_manager import pending_escalations, mark_pending

# =====================================================
# 🧹 CONFIGURACIÓN GLOBAL
# =====================================================
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=UserWarning)
os.environ["PYTHONWARNINGS"] = "ignore"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

# =====================================================
# 🚀 Inicialización de FastAPI
# =====================================================
app = FastAPI(title="HotelAI - Multi-Channel Hybrid Bot")

# =====================================================
# 🧠 Inicialización del agente híbrido principal
# =====================================================
try:
    hybrid_agent = HotelAIHybrid()
    logging.info("✅ HotelAIHybrid inicializado correctamente.")
except Exception as e:
    logging.error(f"❌ Error al inicializar HotelAIHybrid: {e}", exc_info=True)
    raise e

# =====================================================
# 🔌 Registro de canales dinámicos (WhatsApp, Web, etc.)
# =====================================================
manager = ChannelManager()
for name, channel in manager.channels.items():
    channel.agent = hybrid_agent
    channel.register_routes(app)
    logging.info(f"✅ Canal '{name}' registrado correctamente desde {channel.__class__.__module__}")

# =====================================================
# 💬 Canal TELEGRAM independiente
# =====================================================
telegram_channel = TelegramChannel(openai_api_key=None)
telegram_channel.agent = hybrid_agent
telegram_channel.register_routes(app)
logging.info("✅ Canal 'telegram' registrado correctamente.")

logging.info("🚀 Todos los canales inicializados correctamente y listos para recibir mensajes.")

# =====================================================
# 🩺 Healthcheck
# =====================================================
@app.get("/health")
async def health():
    """Endpoint para comprobar el estado general del bot."""
    try:
        return {
            "status": "ok",
            "channels": list(manager.channels.keys()) + ["telegram"],
            "pending_escalations": list(pending_escalations.keys()),
        }
    except Exception as e:
        logging.error(f"⚠️ Error en /health: {e}", exc_info=True)
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)

# =====================================================
# 💬 Endpoint genérico de mensajes (API externa)
# =====================================================
@app.post("/api/message")
async def api_message(request: Request):
    """
    Permite enviar mensajes al agente híbrido mediante HTTP.
    Ideal para integraciones o pruebas directas sin canal específico.
    """
    try:
        data = await request.json()
        user_message = data.get("message", "").strip()
        conversation_id = str(data.get("conversation_id", "unknown")).replace("+", "").strip()

        if not user_message:
            return JSONResponse({"error": "Mensaje vacío"}, status_code=400)

        logging.info(f"📨 [API] Mensaje recibido de {conversation_id}: {user_message}")

        # Procesar mensaje con el agente principal
        response = await hybrid_agent.process_message(user_message, conversation_id)

        # ===== Escalación automática si procede =====
        trigger_phrases = [
            "contactar con el encargado",
            "consultarlo con el encargado",
            "voy a consultarlo con el encargado",
            "un momento por favor",
            "permíteme contactar",
            "he contactado con el encargado",
            "error",
        ]
        if any(p in response.lower() for p in trigger_phrases):
            await mark_pending(conversation_id, user_message)
            logging.warning(f"🟡 Escalación detectada para {conversation_id}")
            return JSONResponse({"response": "🕓 Consultando con el encargado..."})

        # ===== Respuesta normal =====
        logging.info(f"💬 [API] Respuesta enviada: {response[:120]}...")
        return JSONResponse({"response": response})

    except Exception as e:
        logging.error(f"❌ Error en /api/message: {e}", exc_info=True)
        return JSONResponse({"error": "Error interno al procesar el mensaje"}, status_code=500)
