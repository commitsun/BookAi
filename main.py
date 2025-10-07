from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import asyncio
from channels_wrapper.manager import ChannelManager
from core.main_agent import HotelAIHybrid

app = FastAPI(title="HotelAI - Multi-Channel Hybrid Bot")

# ------------------------------------------------------------------
# 🧠 Instancia única del sistema híbrido
# ------------------------------------------------------------------
hybrid_agent = HotelAIHybrid()

# ------------------------------------------------------------------
# 🔌 Inicialización de canales (WhatsApp, Telegram, etc.)
# ------------------------------------------------------------------
manager = ChannelManager()

# Asignar el mismo agente híbrido a todos los canales
for name, channel in manager.channels.items():
    channel.agent = hybrid_agent
    channel.register_routes(app)
    print(f"✅ Canal '{name}' registrado con éxito y conectado al agente híbrido.")

# ------------------------------------------------------------------
# 🩺 Endpoint de salud
# ------------------------------------------------------------------
@app.get("/health")
async def health():
    """Endpoint de comprobación de salud."""
    return {"status": "ok", "channels": list(manager.channels.keys())}

# ------------------------------------------------------------------
# 💬 Endpoint genérico de mensajes (por API directa)
# ------------------------------------------------------------------
@app.post("/api/message")
async def api_message(request: Request):
    """
    Endpoint genérico para recibir mensajes desde cualquier interfaz.
    Estructura esperada:
    {
      "message": "texto del usuario",
      "conversation_id": "opcional"
    }
    """
    try:
        data = await request.json()
        user_message = data.get("message", "").strip()
        conversation_id = data.get("conversation_id")

        if not user_message:
            return JSONResponse({"error": "Mensaje vacío"}, status_code=400)

        # Usar la instancia global del sistema híbrido
        response = await hybrid_agent.process_message(user_message, conversation_id)

        return JSONResponse({"response": response})

    except Exception as e:
        print(f"⚠️ Error en /api/message: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
