"""Socket.IO manager para notificaciones en tiempo real."""

from __future__ import annotations

import logging
from typing import Any, Iterable

log = logging.getLogger("SocketManager")
_GLOBAL_SOCKET_MANAGER = None


class SocketManager:
    """Administra Socket.IO y expone helpers de emisión."""

    def __init__(self, app, *, cors_origins: list[str] | None, bearer_token: str | None):
        self.enabled = False
        self.sio = None
        self._bearer_token = (bearer_token or "").strip()

        try:
            import socketio  # type: ignore
        except Exception as exc:  # pragma: no cover - dependencia opcional
            log.warning("Socket.IO no disponible: %s", exc)
            return

        self.sio = socketio.AsyncServer(
            async_mode="asgi",
            cors_allowed_origins=cors_origins or "*",
            logger=False,
            engineio_logger=False,
        )
        self._register_handlers()
        # Acepta conexiones en /ws (sin /socket.io) para alinearse con el frontend.
        app.mount("/ws", socketio.ASGIApp(self.sio, socketio_path=""))
        self.enabled = True
        log.info("Socket.IO montado en /ws")

    def _extract_auth_token(self, environ: dict, auth: Any | None) -> str | None:
        if isinstance(auth, dict):
            token = auth.get("token") or auth.get("bearer")
            if token:
                return str(token)

        scope = environ.get("asgi.scope", {}) or {}
        headers = scope.get("headers") or environ.get("headers") or []
        for key, val in headers:
            if key == b"authorization":
                try:
                    return val.decode("utf-8")
                except Exception:
                    return None
        return None

    def _is_token_valid(self, token: str | None) -> bool:
        if not self._bearer_token:
            return False
        if not token:
            return False
        raw = str(token).strip()
        if raw.lower().startswith("bearer "):
            raw = raw.split(" ", 1)[1].strip()
        return raw == self._bearer_token

    def _register_handlers(self) -> None:
        if not self.sio:
            return

        @self.sio.event
        async def connect(sid, environ, auth):
            token = self._extract_auth_token(environ, auth)
            if not self._is_token_valid(token):
                log.warning("Socket.IO connect rechazado (sid=%s)", sid)
                return False
            log.info("Socket.IO conectado: %s", sid)
            return True

        @self.sio.event
        async def disconnect(sid):
            log.info("Socket.IO desconectado: %s", sid)

        @self.sio.event
        async def join(sid, data):
            rooms = (data or {}).get("rooms") or []
            for room in rooms:
                await self.sio.enter_room(sid, str(room))

        @self.sio.event
        async def leave(sid, data):
            rooms = (data or {}).get("rooms") or []
            for room in rooms:
                await self.sio.leave_room(sid, str(room))

    async def emit(self, event: str, data: dict[str, Any], rooms: str | Iterable[str] | None = None) -> None:
        if not self.enabled or not self.sio:
            return
        if rooms is None:
            await self.sio.emit(event, data)
            return
        if isinstance(rooms, str):
            await self.sio.emit(event, data, room=rooms)
            return
        for room in rooms:
            await self.sio.emit(event, data, room=str(room))


def set_global_socket_manager(manager: SocketManager | None) -> None:
    global _GLOBAL_SOCKET_MANAGER
    _GLOBAL_SOCKET_MANAGER = manager


def get_global_socket_manager() -> SocketManager | None:
    return _GLOBAL_SOCKET_MANAGER


async def emit_event(event: str, data: dict[str, Any], rooms: str | Iterable[str] | None = None) -> None:
    manager = get_global_socket_manager()
    if not manager:
        return
    await manager.emit(event, data, rooms=rooms)
