from fastapi import FastAPI
from channels_wrapper.manager import ChannelManager

app = FastAPI(title="HotelAI Multi-Channel Bot")

# Inicializamos el manager y registramos los canales
manager = ChannelManager()

for name, channel in manager.channels.items():
    channel.register_routes(app)
    print(f"✅ Canal '{name}' registrado con éxito.")

@app.get("/health")
async def health():
    """Endpoint de comprobación de salud."""
    return {"status": "ok", "channels": list(manager.channels.keys())}

# Esto permite lanzar con: uvicorn main:app --host 0.0.0.0 --port 8000
