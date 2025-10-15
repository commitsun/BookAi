from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import logging

from channels_wrapper.manager import ChannelManager
from core.main_agent import HotelAIHybrid
from core.escalation_manager import pending_escalations, mark_pending
from core.notification import notify_encargado
from channels_wrapper.telegram.telegram_channel import TelegramChannel

# =====================================================
# 🚀 FastAPI
# =====================================================
app = FastAPI(title="HotelAI - Multi-Channel Hybrid Bot")
logging.basicConfig(level=logging.INFO)

# =====================================================
# 🧠 Agente principal
# =====================================================
hybrid_agent = HotelAIHybrid()

# =====================================================
# 🔌 Registro de canales dinámicos
# =====================================================
manager = ChannelManager()
for name, channel in manager.channels.items():
    channel.agent = hybrid_agent
    channel.register_routes(app)
    logging.info(f"✅ Canal '{name}' registrado correctamente.")

# =====================================================
# 🔌 Canal Telegram (único, no dentro del bucle)
# =====================================================
telegram_channel = TelegramChannel(openai_api_key=None)
telegram_channel.agent = hybrid_agent
telegram_channel.register_routes(app)
logging.info("✅ Canal 'telegram' registrado correctamente.")

logging.info("✅ Todos los canales inicializados correctamente.")

# =====================================================
# 🩺 Healthcheck
# =====================================================
@app.get("/health")
async def health():
    """Comprueba el estado del bot y las conversaciones pendientes."""
    return {
        "status": "ok",
        "channels": list(manager.channels.keys()) + ["telegram"],
        "pending": list(pending_escalations.keys()),
    }

# =====================================================
# 💬 Endpoint API genérico
# =====================================================
@app.post("/api/message")
async def api_message(request: Request):
    """Endpoint HTTP para pruebas o integraciones externas."""
    try:
        data = await request.json()
        user_message = data.get("message", "").strip()
        conversation_id = str(data.get("conversation_id", "unknown")).replace("+", "").strip()

        if not user_message:
            return JSONResponse({"error": "Mensaje vacío"}, status_code=400)

        response = await hybrid_agent.process_message(user_message, conversation_id)

                # Si la IA no puede responder, escalar al encargado
        if any(p in response.lower() for p in [
            "contactar con el encargado",
            "consultarlo con el encargado",
            "voy a consultarlo con el encargado",
            "un momento por favor",
            "permíteme contactar",
            "he contactado con el encargado",
            "error",
        ]):
            await mark_pending(conversation_id, user_message)
            return JSONResponse({
                "response": "🕓 Consultando con el encargado..."
            })


        return JSONResponse({"response": response})

    except Exception as e:
        logging.error(f"⚠️ Error en /api/message: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)
