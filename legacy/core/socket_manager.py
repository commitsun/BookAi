"""Socket.IO manager para notificaciones en tiempo real."""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

from core.config import Settings

log = logging.getLogger("SocketManager")
_GLOBAL_SOCKET_MANAGER = None


# Administra Socket.IO y expone helpers de emisión.
# Se usa en el flujo de Socket.IO, autenticación y eventos en tiempo real como pieza de organización, contrato de datos o punto de extensión.
# Se instancia con configuración, managers, clients o callbacks externos y luego delega el trabajo en sus métodos.
# Los efectos reales ocurren cuando sus métodos se invocan; la definición de clase solo organiza estado y responsabilidades.
class SocketManager:
    """Administra Socket.IO y expone helpers de emisión."""

    # Inicializa el estado interno y las dependencias de `SocketManager`.
    # Se usa dentro de `SocketManager` en el flujo de Socket.IO, autenticación y eventos en tiempo real.
    # Recibe `app` como dependencias o servicios compartidos inyectados desde otras capas, y `cors_origins`, `bearer_token` como datos de contexto o entrada de la operación.
    # No devuelve valor; deja la instancia preparada con sus dependencias y estado inicial. Sin efectos secundarios relevantes.
    def __init__(self, app, *, cors_origins: list[str] | None, bearer_token: str | None):
        self.enabled = False
        self.sio = None
        self._bearer_token = (bearer_token or "").strip()
        self._valid_tokens = self._parse_valid_tokens(self._bearer_token)
        self._token_instances = self._parse_token_instances()
        self._sid_instances: dict[str, str] = {}

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

    # Parsea el conjunto de tokens válidos.
    # Se usa dentro de `SocketManager` en el flujo de Socket.IO, autenticación y eventos en tiempo real.
    # Recibe `primary_token` como entrada principal según la firma.
    # Devuelve un `set[str]` con el resultado de esta operación. Sin efectos secundarios relevantes.
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

    # Parsea el mapa token->instancia.
    # Se usa dentro de `SocketManager` en el flujo de Socket.IO, autenticación y eventos en tiempo real.
    # No recibe parámetros externos; trabaja con estado capturado por el cierre o atributos de instancia.
    # Devuelve un `dict[str, str]` con el resultado de esta operación. Sin efectos secundarios relevantes.
    @staticmethod
    def _parse_token_instances() -> dict[str, str]:
        mapping: dict[str, str] = {}

        token_test = str(Settings.ROOMDOO_BOOKAI_TOKEN_TEST or "").strip()
        token_alda = str(Settings.ROOMDOO_BOOKAI_TOKEN_ALDA or "").strip()
        instance_test = str(Settings.ROOMDOO_INSTANCE_ID_TEST or "").strip()
        instance_alda = str(Settings.ROOMDOO_INSTANCE_ID_ALDA or "").strip()
        if token_test and instance_test:
            mapping[token_test] = instance_test
        if token_alda and instance_alda:
            mapping[token_alda] = instance_alda

        raw_map = str(Settings.ROOMDOO_TOKEN_INSTANCE_MAP or "").strip()
        if not raw_map:
            return mapping

        if raw_map.startswith("{"):
            try:
                payload = json.loads(raw_map)
                if isinstance(payload, dict):
                    for token, instance in payload.items():
                        token_text = str(token or "").strip()
                        instance_text = str(instance or "").strip()
                        if token_text and instance_text:
                            mapping[token_text] = instance_text
            except Exception:
                log.warning("ROOMDOO_TOKEN_INSTANCE_MAP inválido para socket instancias (JSON).")
            return mapping

        for chunk in raw_map.split(","):
            part = str(chunk or "").strip()
            if not part or "=" not in part:
                continue
            instance, token = part.split("=", 1)
            token_text = str(token or "").strip()
            instance_text = str(instance or "").strip()
            if token_text and instance_text:
                mapping[token_text] = instance_text
        return mapping

    # Normaliza el token.
    # Se usa dentro de `SocketManager` en el flujo de Socket.IO, autenticación y eventos en tiempo real.
    # Recibe `token` como entrada principal según la firma.
    # Devuelve un `str` con el resultado de esta operación. Sin efectos secundarios relevantes.
    @staticmethod
    def _normalize_token(token: str | None) -> str:
        raw = str(token or "").strip()
        if raw.lower().startswith("bearer "):
            raw = raw.split(" ", 1)[1].strip()
        return raw

    # Resuelve el participantes.
    # Se usa dentro de `SocketManager` en el flujo de Socket.IO, autenticación y eventos en tiempo real.
    # Recibe `room` como entrada principal según la firma.
    # Devuelve un `list[str]` con el resultado de esta operación. Sin efectos secundarios relevantes.
    def _room_participants(self, room: str) -> list[str]:
        if not self.sio:
            return []
        try:
            rooms = getattr(self.sio.manager, "rooms", {}) or {}
            namespace_rooms = rooms.get("/", {}) if hasattr(rooms, "get") else {}
            participants = namespace_rooms.get(str(room), {}) if hasattr(namespace_rooms, "get") else {}
            if hasattr(participants, "keys"):
                return [str(sid) for sid in participants.keys()]
            return [str(sid) for sid in participants]
        except Exception:
            return []

    # Expande compat sala nombres.
    # Se usa dentro de `SocketManager` en el flujo de Socket.IO, autenticación y eventos en tiempo real.
    # Recibe `room` como entrada principal según la firma.
    # Devuelve un `list[str]` con el resultado de esta operación. Sin efectos secundarios relevantes.
    @staticmethod
    def _expand_compat_room_names(room: str) -> list[str]:
        raw_room = str(room or "").strip()
        if not raw_room:
            return []
        expanded = [raw_room]
        if raw_room.startswith("chat:"):
            alias = raw_room.split(":", 1)[1].strip()
            if alias and alias not in expanded:
                expanded.append(alias)
        return expanded

    # Resuelve sids para salas.
    # Se usa dentro de `SocketManager` en el flujo de Socket.IO, autenticación y eventos en tiempo real.
    # Recibe `rooms` como entrada principal según la firma.
    # Devuelve un `list[str]` con el resultado de esta operación. Sin efectos secundarios relevantes.
    def _target_sids_for_rooms(self, rooms: Iterable[str]) -> list[str]:
        target_sids: list[str] = []
        seen: set[str] = set()
        for room in rooms:
            for compatible_room in self._expand_compat_room_names(str(room)):
                for sid in self._room_participants(compatible_room):
                    if sid in seen:
                        continue
                    seen.add(sid)
                    target_sids.append(sid)
        return target_sids

    # Normaliza el payload de chat mensaje.
    # Se usa dentro de `SocketManager` en el flujo de Socket.IO, autenticación y eventos en tiempo real.
    # Recibe `event`, `data` como entradas relevantes junto con el contexto inyectado en la firma.
    # Devuelve un `dict[str, Any]` con el resultado de esta operación. Sin efectos secundarios relevantes.
    @staticmethod
    def _normalize_chat_message_payload(event: str, data: dict[str, Any]) -> dict[str, Any]:
        if event not in {"chat.message.created", "chat.message.new"}:
            return data
        payload = dict(data or {})
        message_text = payload.get("message")
        if message_text is None:
            message_text = payload.get("content")
        if message_text is not None:
            payload.setdefault("message", message_text)
            payload.setdefault("content", message_text)
        sender = str(payload.get("sender") or "").strip() or "bookai"
        payload.setdefault("sender", sender)
        payload.setdefault("read_status", False)

        original_chat_id = str(
            payload.get("original_chat_id")
            or payload.get("context_id")
            or ""
        ).strip()
        if original_chat_id:
            payload.setdefault("original_chat_id", original_chat_id)

        chat_id = str(payload.get("chat_id") or "").strip() or None
        created_at = str(payload.get("created_at") or "").strip() or None
        property_id = payload.get("property_id")
        if not payload.get("message_id"):
            parts = [
                str(chat_id or "chat").strip(),
                sender,
                str(property_id or "").strip(),
                str(created_at or "").strip(),
            ]
            token = ":".join(part.replace(":", "_") for part in parts if part)
            payload["message_id"] = f"socket:{token}" if token else "socket:message"

        payload.setdefault(
            "item",
            {
                "message_id": payload.get("message_id"),
                "chat_id": chat_id,
                "created_at": created_at,
                "read_status": payload.get("read_status"),
                "content": payload.get("content"),
                "message": payload.get("message"),
                "sender": sender,
                "original_chat_id": payload.get("original_chat_id"),
                "property_id": property_id,
                "user_id": payload.get("user_id"),
                "user_first_name": payload.get("user_first_name"),
                "user_last_name": payload.get("user_last_name"),
                "user_last_name2": payload.get("user_last_name2"),
                "structured_payload": payload.get("structured_payload"),
                "structured_csv": payload.get("structured_csv"),
                "ai_request_type": payload.get("ai_request_type"),
                "escalation_reason": payload.get("escalation_reason"),
            },
        )
        return payload

    # Extrae auth token.
    # Se usa dentro de `SocketManager` en el flujo de Socket.IO, autenticación y eventos en tiempo real.
    # Recibe `environ`, `auth` como entradas relevantes junto con el contexto inyectado en la firma.
    # Devuelve un `str | None` con el resultado de esta operación. Sin efectos secundarios relevantes.
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

    # Determina si token valid cumple la condición necesaria en este punto del flujo.
    # Se usa dentro de `SocketManager` en el flujo de Socket.IO, autenticación y eventos en tiempo real.
    # Recibe `token` como entrada principal según la firma.
    # Devuelve un booleano que gobierna la rama de ejecución siguiente. Sin efectos secundarios relevantes.
    def _is_token_valid(self, token: str | None) -> bool:
        if not self._valid_tokens:
            return False
        if not token:
            return False
        raw = self._normalize_token(token)
        return raw in self._valid_tokens

    # Registra el handlers.
    # Se usa dentro de `SocketManager` en el flujo de Socket.IO, autenticación y eventos en tiempo real.
    # No recibe parámetros externos; trabaja con estado capturado por el cierre o atributos de instancia.
    # Devuelve un `None` con el resultado de esta operación. Puede emitir eventos socket.
    def _register_handlers(self) -> None:
        if not self.sio:
            return

        # Resuelve el conexión.
        # Se invoca dentro de `_register_handlers` para encapsular una parte local de Socket.IO, autenticación y eventos en tiempo real.
        # Recibe `sid`, `environ`, `auth` como entradas relevantes junto con el contexto inyectado en la firma.
        # Devuelve el resultado calculado para que el siguiente paso lo consuma. Sin efectos secundarios relevantes.
        @self.sio.event
        async def connect(sid, environ, auth):
            token = self._extract_auth_token(environ, auth)
            if not self._is_token_valid(token):
                log.warning("Socket.IO connect rechazado (sid=%s)", sid)
                return False
            normalized = self._normalize_token(token)
            instance_id = self._token_instances.get(normalized)
            if instance_id:
                self._sid_instances[str(sid)] = str(instance_id)
            log.info("Socket.IO conectado: %s", sid)
            return True

        # Resuelve el desconexión.
        # Se invoca dentro de `_register_handlers` para encapsular una parte local de Socket.IO, autenticación y eventos en tiempo real.
        # Recibe `sid` como entrada principal según la firma.
        # No devuelve un valor relevante; deja preparado el estado o ejecuta la acción necesaria. Sin efectos secundarios relevantes.
        @self.sio.event
        async def disconnect(sid):
            self._sid_instances.pop(str(sid), None)
            log.info("Socket.IO desconectado: %s", sid)

        # Resuelve el join.
        # Se invoca dentro de `_register_handlers` para encapsular una parte local de Socket.IO, autenticación y eventos en tiempo real.
        # Recibe `sid`, `data` como entradas relevantes junto con el contexto inyectado en la firma.
        # No devuelve un valor relevante; deja preparado el estado o ejecuta la acción necesaria. Puede emitir eventos socket.
        @self.sio.event
        async def join(sid, data):
            rooms = (data or {}).get("rooms") or []
            log.debug("Socket join sid=%s rooms=%s", sid, rooms)
            for room in rooms:
                await self.sio.enter_room(sid, str(room))

        # Resuelve el join.
        # Se invoca dentro de `_register_handlers` para encapsular una parte local de Socket.IO, autenticación y eventos en tiempo real.
        # Recibe `sid`, `data` como entradas relevantes junto con el contexto inyectado en la firma.
        # No devuelve un valor relevante; deja preparado el estado o ejecuta la acción necesaria. Puede emitir eventos socket.
        @self.sio.event
        async def room_join(sid, data):
            rooms = (data or {}).get("rooms") or []
            log.debug("Socket room_join sid=%s rooms=%s", sid, rooms)
            for room in rooms:
                await self.sio.enter_room(sid, str(room))

        # Resuelve el leave.
        # Se invoca dentro de `_register_handlers` para encapsular una parte local de Socket.IO, autenticación y eventos en tiempo real.
        # Recibe `sid`, `data` como entradas relevantes junto con el contexto inyectado en la firma.
        # No devuelve un valor relevante; deja preparado el estado o ejecuta la acción necesaria. Puede emitir eventos socket.
        @self.sio.event
        async def leave(sid, data):
            rooms = (data or {}).get("rooms") or []
            for room in rooms:
                await self.sio.leave_room(sid, str(room))

        # Resuelve el leave.
        # Se invoca dentro de `_register_handlers` para encapsular una parte local de Socket.IO, autenticación y eventos en tiempo real.
        # Recibe `sid`, `data` como entradas relevantes junto con el contexto inyectado en la firma.
        # No devuelve un valor relevante; deja preparado el estado o ejecuta la acción necesaria. Puede emitir eventos socket.
        @self.sio.event
        async def room_leave(sid, data):
            rooms = (data or {}).get("rooms") or []
            for room in rooms:
                await self.sio.leave_room(sid, str(room))

    # Resuelve el emisión.
    # Se usa dentro de `SocketManager` en el flujo de Socket.IO, autenticación y eventos en tiempo real.
    # Recibe `event`, `data`, `rooms`, `instance_id` como entradas relevantes junto con el contexto inyectado en la firma.
    # No devuelve un valor relevante; deja preparado el estado o ejecuta la acción necesaria. Puede emitir eventos socket.
    async def emit(
        self,
        event: str,
        data: dict[str, Any],
        rooms: str | Iterable[str] | None = None,
        instance_id: str | None = None,
    ) -> None:
        if not self.enabled or not self.sio:
            return
        data = self._normalize_chat_message_payload(event, data)
        log.debug("Socket emit event=%s rooms=%s", event, rooms)
        if rooms is None:
            await self.sio.emit(event, data)
            return
        if isinstance(rooms, str):
            normalized_instance = str(instance_id or "").strip()
            if normalized_instance and rooms.startswith("property:"):
                target_sids = [
                    sid
                    for sid in self._room_participants(rooms)
                    if self._sid_instances.get(str(sid)) == normalized_instance
                ]
                if target_sids:
                    for sid in target_sids:
                        await self.sio.emit(event, data, room=sid)
                    return
            target_sids = self._target_sids_for_rooms([rooms])
            if target_sids:
                for sid in target_sids:
                    await self.sio.emit(event, data, room=sid)
                return
            await self.sio.emit(event, data, room=rooms)
            return
        unique_rooms = []
        seen = set()
        for room in rooms:
            for compatible_room in self._expand_compat_room_names(str(room)):
                if compatible_room in seen:
                    continue
                seen.add(compatible_room)
                unique_rooms.append(compatible_room)
        if not unique_rooms:
            await self.sio.emit(event, data)
            return
        target_sids = self._target_sids_for_rooms(unique_rooms)
        if target_sids:
            for sid in target_sids:
                await self.sio.emit(event, data, room=sid)
            return
        try:
            await self.sio.emit(event, data, room=unique_rooms)
        except Exception:
            for room in unique_rooms:
                await self.sio.emit(event, data, room=room)


# Fija global socket manager.
# Se usa en el flujo de Socket.IO, autenticación y eventos en tiempo real para preparar datos, validaciones o decisiones previas.
# Recibe `manager` como entrada principal según la firma.
# No devuelve un valor de negocio; deja aplicado el cambio de estado o registro correspondiente. Sin efectos secundarios relevantes.
def set_global_socket_manager(manager: SocketManager | None) -> None:
    global _GLOBAL_SOCKET_MANAGER
    _GLOBAL_SOCKET_MANAGER = manager


# Recupera global socket manager.
# Se usa en el flujo de Socket.IO, autenticación y eventos en tiempo real para preparar datos, validaciones o decisiones previas.
# No recibe parámetros externos; trabaja con estado capturado por el cierre o atributos de instancia.
# Devuelve un `SocketManager | None` con el resultado de esta operación. Sin efectos secundarios relevantes.
def get_global_socket_manager() -> SocketManager | None:
    return _GLOBAL_SOCKET_MANAGER


# Resuelve el evento.
# Se usa en el flujo de Socket.IO, autenticación y eventos en tiempo real para preparar datos, validaciones o decisiones previas.
# Recibe `event`, `data`, `rooms` como entradas relevantes junto con el contexto inyectado en la firma.
# No devuelve un valor relevante; deja preparado el estado o ejecuta la acción necesaria. Puede emitir eventos socket.
async def emit_event(event: str, data: dict[str, Any], rooms: str | Iterable[str] | None = None) -> None:
    manager = get_global_socket_manager()
    if not manager:
        return
    await manager.emit(event, data, rooms=rooms)
