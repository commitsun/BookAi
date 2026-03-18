"""
🚀 Main Entry Point - Sistema de Agentes con Orquestación + Idioma + Buffer
Mantiene el comportamiento previo moviendo la lógica pesada a módulos dedicados.
"""

import logging
import warnings

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.app_state import AppState
from core.pipeline import process_user_message  # re-export para compatibilidad
from channels_wrapper.telegram.webhook_telegram import register_telegram_routes
from channels_wrapper.whatsapp.webhook_whatsapp import register_whatsapp_routes
from api.template_routes import register_template_routes
from api.chatter_routes import register_chatter_routes
from api.superintendente_routes import register_superintendente_routes
from core.config import Settings
from core.socket_manager import SocketManager, set_global_socket_manager

# =============================================================
# CONFIG GLOBAL / LOGGING
# =============================================================

warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=UserWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    force=True,
)

for noisy_logger in ("langsmith", "langsmith.client"):
    logging.getLogger(noisy_logger).setLevel(logging.ERROR)

log = logging.getLogger("Main")

# =============================================================
# FASTAPI APP
# =============================================================

app = FastAPI(title="HotelAI - Sistema de Agentes ReAct v4")

# =============================================================
# CORS
# =============================================================

# Parsea los orígenes CORS configurados.
# Se usa en el flujo de arranque FastAPI, CORS, sockets y registro de rutas para preparar datos, validaciones o decisiones previas.
# Recibe `raw` como entrada principal según la firma.
# Devuelve un `list[str]` con el resultado de esta operación. Sin efectos secundarios relevantes.
def _parse_cors_origins(raw: str) -> list[str]:
    raw = (raw or "").strip()
    if not raw:
        return []
    if raw == "*" or raw.lower() == "all":
        return ["*"]
    parts = [item.strip().rstrip("/") for item in raw.split(",")]
    return [item for item in parts if item]


cors_origins = _parse_cors_origins(Settings.CORS_ORIGINS)
if not cors_origins:
    log.warning("CORS_ORIGINS vacío. Se permite cualquier origen por defecto.")
    cors_origins = ["*"]

class ConditionalCORSMiddleware:
    """Evita duplicar headers CORS en rutas que ya los manejan (p.ej. Socket.IO)."""

    # Inicializa el estado interno y las dependencias de `ConditionalCORSMiddleware`.
    # Se usa dentro de `ConditionalCORSMiddleware` en el flujo de arranque FastAPI, CORS, sockets y registro de rutas.
    # Recibe `app` como dependencias o servicios compartidos inyectados desde otras capas, y `skip_paths`, `cors_kwargs` como datos de contexto o entrada de la operación.
    # No devuelve valor; deja la instancia preparada con sus dependencias y estado inicial. Sin efectos secundarios relevantes.
    def __init__(self, app, *, skip_paths: list[str], **cors_kwargs):
        self.app = app
        self.cors = CORSMiddleware(app, **cors_kwargs)
        self.skip_paths = tuple(skip_paths)

    # Gestiona la invocación directa del objeto cuando actúa como middleware o callback.
    # Se usa dentro de `ConditionalCORSMiddleware` en el flujo de arranque FastAPI, CORS, sockets y registro de rutas.
    # Recibe `scope`, `receive`, `send` como entradas relevantes junto con el contexto inyectado en la firma.
    # Devuelve el resultado calculado para que el siguiente paso lo consuma. Sin efectos secundarios relevantes.
    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path") or ""
            for prefix in self.skip_paths:
                if path.startswith(prefix):
                    return await self.app(scope, receive, send)
        return await self.cors(scope, receive, send)


app.add_middleware(
    ConditionalCORSMiddleware,
    skip_paths=["/ws"],
    allow_origins=cors_origins,
    allow_credentials="*" not in cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =============================================================
# ESTADO COMPARTIDO
# =============================================================

state = AppState()
log.info("✅ Sistema inicializado con agentes y estado compartido")

# =============================================================
# SOCKET.IO (tiempo real)
# =============================================================

socket_manager = SocketManager(
    app,
    cors_origins=cors_origins,
    bearer_token=Settings.ROOMDOO_BEARER_TOKEN,
)
setattr(state, "socket_manager", socket_manager if socket_manager.enabled else None)
set_global_socket_manager(socket_manager if socket_manager.enabled else None)

# =============================================================
# WEBHOOKS REGISTRADOS
# =============================================================

register_whatsapp_routes(app, state)
register_telegram_routes(app, state)
register_template_routes(app, state)
register_chatter_routes(app, state)
register_superintendente_routes(app, state)

# =============================================================
# HEALTHCHECK
# =============================================================


# Atiende el endpoint `GET /health` y coordina la operación pública de este módulo.
# Se usa como punto de entrada HTTP dentro de arranque FastAPI, CORS, sockets y registro de rutas.
# No recibe parámetros de negocio adicionales; consume el request y dependencias resueltas por FastAPI.
# Devuelve la respuesta HTTP del endpoint o lanza errores de validación cuando corresponde. Sin efectos secundarios relevantes.
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
