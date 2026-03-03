"""Socket.IO manager para notificaciones en tiempo real."""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

from core.config import Settings

log = logging.getLogger("SocketManager")
_GLOBAL_SOCKET_MANAGER = None


class SocketManager:
    """Administra Socket.IO y expone helpers de emisión."""

    def __init__(self, app, *, cors_origins: list[str] | None, bearer_token: str | None):
        self.enabled = False
        self.sio = None
        self._bearer_token = (bearer_token or "").strip()
        self._sid_instances: dict[str, str] = {}
        self._token_instances: dict[str, str] = {}
        raw_map = str(Settings.ROOMDOO_TOKEN_INSTANCE_MAP or "").strip()
        if raw_map.startswith("{"):
            try:
                payload = json.loads(raw_map)
                if isinstance(payload, dict):
                    for token, instance_id in payload.items():
                        token_text = str(token or "").strip()
                        instance_text = str(instance_id or "").strip()
                        if token_text and instance_text:
                            self._token_instances[token_text] = instance_text
            except Exception:
                log.warning("ROOMDOO_TOKEN_INSTANCE_MAP inválido para socket auth (JSON).")
        elif raw_map:
            for chunk in raw_map.split(","):
                part = str(chunk or "").strip()
                if not part or "=" not in part:
                    continue
                instance_id, token = part.split("=", 1)
                token_text = str(token or "").strip()
                instance_text = str(instance_id or "").strip()
                if token_text and instance_text:
                    self._token_instances[token_text] = instance_text
        legacy_test_token = str(Settings.ROOMDOO_BOOKAI_TOKEN_TEST or "").strip()
        legacy_test_instance = str(Settings.ROOMDOO_INSTANCE_ID_TEST or "").strip()
        if legacy_test_token and legacy_test_instance:
            self._token_instances.setdefault(legacy_test_token, legacy_test_instance)
        legacy_alda_token = str(Settings.ROOMDOO_BOOKAI_TOKEN_ALDA or "").strip()
        legacy_alda_instance = str(Settings.ROOMDOO_INSTANCE_ID_ALDA or "").strip()
        if legacy_alda_token and legacy_alda_instance:
            self._token_instances.setdefault(legacy_alda_token, legacy_alda_instance)
        self._valid_tokens = self._parse_valid_tokens(self._bearer_token)

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
        log.info("Socket.IO montado en /ws (tokens_validos=%s)", len(self._valid_tokens))

    @staticmethod
    def _parse_valid_tokens(primary_token: str) -> set[str]:
        tokens: set[str] = set()
        if primary_token:
            tokens.add(primary_token.strip())

        # Compatibilidad con los dos tokens legacy (test/alda).
        for value in (
            Settings.ROOMDOO_BOOKAI_TOKEN_TEST,
            Settings.ROOMDOO_BOOKAI_TOKEN_ALDA,
        ):
            token = str(value or "").strip()
            if token:
                tokens.add(token)

        raw_map = str(Settings.ROOMDOO_TOKEN_INSTANCE_MAP or "").strip()
        if not raw_map:
            return tokens

        if raw_map.startswith("{"):
            try:
                payload = json.loads(raw_map)
                if isinstance(payload, dict):
                    for token in payload.keys():
                        token_text = str(token or "").strip()
                        if token_text:
                            tokens.add(token_text)
            except Exception:
                log.warning("ROOMDOO_TOKEN_INSTANCE_MAP inválido para socket auth (JSON).")
            return tokens

        # Formato CSV "instA=tokenA,instB=tokenB"
        for chunk in raw_map.split(","):
            part = str(chunk or "").strip()
            if not part or "=" not in part:
                continue
            _, token = part.split("=", 1)
            token_text = str(token or "").strip()
            if token_text:
                tokens.add(token_text)
        return tokens

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
        if not self._valid_tokens:
            return False
        if not token:
            return False
        raw = str(token).strip()
        if raw.lower().startswith("bearer "):
            raw = raw.split(" ", 1)[1].strip()
        return raw in self._valid_tokens

    def _register_handlers(self) -> None:
        if not self.sio:
            return

        @self.sio.event
        async def connect(sid, environ, auth):
            token = self._extract_auth_token(environ, auth)
            if not self._is_token_valid(token):
                log.warning("Socket.IO connect rechazado (sid=%s)", sid)
                return False
            raw = str(token or "").strip()
            if raw.lower().startswith("bearer "):
                raw = raw.split(" ", 1)[1].strip()
            instance_id = self._token_instances.get(raw)
            if instance_id:
                self._sid_instances[str(sid)] = instance_id
            log.info("Socket.IO conectado: %s", sid)
            return True

        @self.sio.event
        async def disconnect(sid):
            self._sid_instances.pop(str(sid), None)
            log.info("Socket.IO desconectado: %s", sid)

        @self.sio.event
        async def join(sid, data):
            rooms = (data or {}).get("rooms") or []
            log.debug("Socket join sid=%s rooms=%s", sid, rooms)
            for room in rooms:
                room_text = str(room)
                await self.sio.enter_room(sid, room_text)
                instance_id = self._sid_instances.get(str(sid))
                if instance_id and room_text.startswith("property:"):
                    await self.sio.enter_room(sid, f"{room_text}:{instance_id}")

        @self.sio.event
        async def room_join(sid, data):
            rooms = (data or {}).get("rooms") or []
            log.debug("Socket room_join sid=%s rooms=%s", sid, rooms)
            for room in rooms:
                room_text = str(room)
                await self.sio.enter_room(sid, room_text)
                instance_id = self._sid_instances.get(str(sid))
                if instance_id and room_text.startswith("property:"):
                    await self.sio.enter_room(sid, f"{room_text}:{instance_id}")

        @self.sio.event
        async def leave(sid, data):
            rooms = (data or {}).get("rooms") or []
            for room in rooms:
                room_text = str(room)
                await self.sio.leave_room(sid, room_text)
                instance_id = self._sid_instances.get(str(sid))
                if instance_id and room_text.startswith("property:"):
                    await self.sio.leave_room(sid, f"{room_text}:{instance_id}")

        @self.sio.event
        async def room_leave(sid, data):
            rooms = (data or {}).get("rooms") or []
            for room in rooms:
                room_text = str(room)
                await self.sio.leave_room(sid, room_text)
                instance_id = self._sid_instances.get(str(sid))
                if instance_id and room_text.startswith("property:"):
                    await self.sio.leave_room(sid, f"{room_text}:{instance_id}")

    async def emit(self, event: str, data: dict[str, Any], rooms: str | Iterable[str] | None = None) -> None:
        if not self.enabled or not self.sio:
            return
        log.debug("Socket emit event=%s rooms=%s", event, rooms)
        if rooms is None:
            await self.sio.emit(event, data)
            return
        if isinstance(rooms, str):
            await self.sio.emit(event, data, room=rooms)
            return
        unique_rooms = []
        seen = set()
        for room in rooms:
            r = str(room)
            if r in seen:
                continue
            seen.add(r)
            unique_rooms.append(r)
        if not unique_rooms:
            await self.sio.emit(event, data)
            return
        try:
            await self.sio.emit(event, data, room=unique_rooms)
        except Exception:
            for room in unique_rooms:
                await self.sio.emit(event, data, room=room)


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
