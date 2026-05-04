# BookAI — Guia de Migracion API para Frontend

**Version**: Refactorizacion v2 (abril 2026)
**Audiencia**: Desarrollador frontend
**Proyecto**: BookAI — Backend conversacional para hoteles

---

## Resumen ejecutivo

BookAI ha sido refactorizado completamente. Los cambios principales que afectan al frontend son:

1. **Todas las rutas han cambiado** — nuevo prefijo `/api/v1/` y estructura RESTful
2. **Los identificadores cambian** — de `chat_id` (telefono) a `conversation_id` (int)
3. **Socket.IO se simplifica** — eventos renombrados, auto-join de rooms
4. **Nuevas funcionalidades** — multi-canal, transferencias, media, delivery tracking
5. **Escalaciones son entidades independientes** — ya no estan embebidas en mensajes

---

## 1. Cambios de rutas

| Legacy | Nuevo | Notas |
|--------|-------|-------|
| `GET /chats` | `GET /api/v1/conversations/?property_id=X` | Params cambian |
| `GET /chats/{chat_id}/messages` | `GET /api/v1/conversations/{id}/messages` | chat_id -> conversation_id |
| `POST /messages` | `POST /api/v1/chatter/send-message` | Body nuevo |
| `PATCH /chats/{chat_id}/bookai` | `PATCH /api/v1/conversations/{id}/ai?property_id=X&ai_enabled=true` | Query params |
| `PATCH /chats/{chat_id}/read` | `PATCH /api/v1/conversations/{id}/read?property_id=X` | Necesita property_id |
| `POST /chats/{id}/resolve-escalation` | `PATCH /api/v1/escalations/{escalation_id}/resolve` | Por escalation ID |
| `POST /templates/send` | `POST /api/v1/whatsapp/send-template` | Compatible con Odoo |

---

## 2. Identificadores - CAMBIO CRITICO

**Antes**: Todo se identifica por `chat_id` (string, telefono del huesped, ej: `"34699323583"`)

**Ahora**: Todo se identifica por `conversation_id` (int, ID de base de datos, ej: `123`)

- REST API: usar `conversation_id` para todas las llamadas
- Socket.IO rooms: usar `phone_code` del contacto (ej: `chat:34699323583`)
- El `phone_code` viene en `conversation.contact.phone_code` al listar conversaciones

---

## 3. Listado de conversaciones (Inbox)

### `GET /api/v1/conversations/?property_id=X&limit=50`

**Query params**:
- `property_id` (int, obligatorio) — ID de la property del hotel. Usar `0` para inbox sin asignar
- `limit` (int, default 50, max 200)

**Respuesta**:
```json
{
  "property_id": 5,
  "conversations": [
    {
      "id": 123,
      "created_at": "2026-04-22T10:30:00",
      "updated_at": "2026-04-22T12:45:00",
      "contact": {
        "id": 789,
        "phone_code": "34699323583",
        "display_name": "Maria Garcia"
      },
      "last_message": {
        "id": 456,
        "direction": "inbound",
        "sender": "guest",
        "content": "Hola, necesito ayuda...",
        "template_code": null,
        "created_at": "2026-04-22T12:45:00"
      },
      "unread_count": 2,
      "needs_attention": false,
      "ai_enabled": true,
      "has_pending_escalation": false
    }
  ]
}
```

**Diferencias con legacy**:
- No hay paginacion page/page_size, solo `limit`
- `property_id` es obligatorio (antes era opcional)
- No hay filtro `channel` (ahora todas las conversaciones son multi-canal)
- Busqueda es endpoint separado: `GET /api/v1/conversations/search?property_id=X&q=texto`

### Busqueda: `GET /api/v1/conversations/search`

**Query params**:
- `property_id` (int, obligatorio)
- `q` (string) — busca en nombre de contacto y codigo de folio
- `status` (string, opcional) — estado del folio: draft, confirm, onboard, done, cancel
- `limit` (int, default 50)

---

## 4. Historial de mensajes

### `GET /api/v1/conversations/{conversation_id}/messages`

**Query params**:
- `limit` (int, default 50, max 200)
- `before_id` (int, opcional) — cursor para paginacion (mensajes anteriores a este ID)
- `language` (string, opcional) — codigo BCP-47 (ej: "es", "en") para traducciones
- `property_id` (int, opcional) — filtra notas internas por property

**Respuesta**:
```json
{
  "conversation_id": 123,
  "language": "es",
  "messages": [
    {
      "id": 456,
      "conversation_id": 123,
      "channel_endpoint_id": 10,
      "channel": "whatsapp",
      "kind": "message",
      "direction": "inbound",
      "sender": "guest",
      "content": "A que hora es el check-in?",
      "content_language": "es",
      "is_translated": false,
      "agent_user_id": null,
      "agent_display_name": null,
      "wa_message_id": "wamid.HBg...",
      "wa_message_type": "text",
      "delivery_status": "delivered",
      "routing_status": null,
      "template_code": null,
      "template_payload": null,
      "created_at": "2026-04-22T10:30:00",
      "email_metadata": null,
      "media": null
    }
  ]
}
```

**Mapping de campos legacy -> nuevo**:

| Legacy | Nuevo | Notas |
|--------|-------|-------|
| `role` ("user"/"assistant") | `direction` + `sender` | direction=inbound/outbound, sender=guest/agent/ai/system |
| `read_status` | (eliminado) | Gestionado con `unread_count` a nivel conversacion |
| `user_id` | `agent_user_id` | Solo para mensajes de operador |
| `user_first_name` + `user_last_name` | `agent_display_name` | Nombre completo |
| `structured_payload` | (eliminado) | No aplica |
| `escalation_reason` | (eliminado) | Escalaciones son entidades separadas |
| (no existia) | `channel` | "whatsapp" o "email" |
| (no existia) | `content_language` | Idioma detectado del mensaje |
| (no existia) | `is_translated` | Si content es una traduccion cached |
| (no existia) | `template_code` | Codigo del template (si es mensaje de template) |
| (no existia) | `template_payload` | Datos del template |
| (no existia) | `email_metadata` | `{ subject, from_address, from_name, has_attachments }` |
| (no existia) | `media` | `[{ id, media_type, url, transcription, vision_description }]` |

**Renderizar templates**: Si `content` es null pero `template_code` tiene valor, mostrar algo como "Plantilla: {template_code}".

**Renderizar media**: El array `media` puede contener imagenes, audio, video o documentos con URLs relativas.

---

## 5. Envio de mensaje (operador)

### `POST /api/v1/chatter/send-message`

**Request body**:
```json
{
  "conversation_id": 123,
  "content": "Hola, le confirmo la reserva",
  "agent_user_id": 5,
  "agent_display_name": "Ana Garcia",
  "channel_endpoint_id": 10
}
```

- `conversation_id` (int, obligatorio) — reemplaza `chat_id`
- `content` (string, obligatorio) — reemplaza `message`
- `agent_user_id` (int, opcional) — ID del usuario de Odoo
- `agent_display_name` (string, opcional) — nombre visible del operador
- `channel_endpoint_id` (int, opcional) — si no se pasa, auto-detecta el canal mas reciente

**Respuesta**:
```json
{
  "status": "ok",
  "message_id": 1205,
  "wa_message_id": "wamid.HBg...",
  "conversation_id": 123
}
```

**Error 422**: Ventana de 24h cerrada. Solo se pueden enviar templates fuera de la ventana.

### Canales disponibles: `GET /api/v1/conversations/{id}/channels?property_id=X`

```json
{
  "channels": [
    { "id": 10, "channel": "whatsapp", "display": "+34 900 123 456" },
    { "id": 11, "channel": "email", "display": "bookings@hotel.com" }
  ]
}
```

---

## 6. Toggle IA

### `PATCH /api/v1/conversations/{id}/ai?property_id=X&ai_enabled=true`

Sin body. Todo en query params.

**Respuesta**:
```json
{
  "conversation_id": 123,
  "session_id": 456,
  "ai_enabled": true
}
```

---

## 7. Marcar como leido

### `PATCH /api/v1/conversations/{id}/read?property_id=X`

`property_id` obligatorio porque el conteo de no leidos es per-property.

Respuesta: `204 No Content`

---

## 8. Escalaciones

Las escalaciones son ahora entidades independientes con su propio ciclo de vida, no campos dentro de mensajes.

### Listar: `GET /api/v1/escalations?property_id=X&status=pending`

```json
{
  "property_id": 5,
  "escalations": [
    {
      "id": 123,
      "conversation_id": 456,
      "session_id": 789,
      "escalation_type": "manual",
      "reason": "Guest requested human agent",
      "priority": 2,
      "status": "pending",
      "created_at": "2026-04-22T10:30:00",
      "resolved_at": null
    }
  ]
}
```

### Por conversacion: `GET /api/v1/conversations/{id}/escalations`

Incluye `messages` — timeline de la escalacion.

### Resolver: `PATCH /api/v1/escalations/{escalation_id}/resolve`

```json
{
  "resolution_medium": "manual_takeover",
  "resolution_notes": "Operador tomo el control"
}
```

Medios de resolucion: `whatsapp`, `phone`, `in_person`, `ai_supervised`, `manual_takeover`, `other`

---

## 9. Socket.IO

### Conexion

```javascript
const socket = io("", {
  auth: {
    token: "mi-bearer-token",
    property_id: 5
  }
});

// Auto-join a property:5 al conectar
// Para abrir un chat especifico:
socket.emit("join_chat", { phone_code: "34699323583" });

// Al salir del chat:
socket.emit("leave_chat", { phone_code: "34699323583" });
```

### Eventos renombrados

| Legacy | Nuevo |
|--------|-------|
| `chat.message.created` | `message.created` |
| `chat.message.new` | `message.created` |
| `chat.updated` | `conversation.updated` |

### Eventos nuevos

| Evento | Room | Descripcion |
|--------|------|-------------|
| `conversation.created` | `property:{id}` | Primera conversacion de un contacto |
| `message.delivery_updated` | `chat:{phone}` | Cambio de estado de entrega |
| `escalation.created` | `property:{id}` | Nueva escalacion |
| `escalation.resolved` | `property:{id}` | Escalacion resuelta |

### message.created (Room: `chat:{phone_code}`)

```json
{
  "id": 456,
  "conversation_id": 123,
  "channel_endpoint_id": 10,
  "channel": "whatsapp",
  "direction": "inbound",
  "sender": "guest",
  "content": "...",
  "content_language": "es",
  "wa_message_id": "wamid.HBg...",
  "delivery_status": "delivered",
  "template_code": null,
  "template_payload": null,
  "agent_user_id": null,
  "agent_display_name": null,
  "created_at": "2026-04-22T10:30:00"
}
```

Valores de `sender`: `guest`, `agent`, `ai`, `system`

### conversation.created / conversation.updated (Room: `property:{id}`)

```json
{
  "id": 123,
  "created_at": "...",
  "updated_at": "...",
  "unread_count": 2,
  "needs_attention": false,
  "contact": {
    "id": 789,
    "phone_code": "34699323583",
    "display_name": "Maria Garcia"
  },
  "last_message": {
    "id": 456,
    "direction": "inbound",
    "sender": "guest",
    "content": "...",
    "template_code": null,
    "created_at": "..."
  }
}
```

### message.delivery_updated (Room: `chat:{phone_code}`)

```json
{
  "id": 456,
  "conversation_id": 123,
  "wa_message_id": "wamid.HBg...",
  "delivery_status": "read",
  "delivery_error": null
}
```

Estados: `pending` -> `sent` -> `delivered` -> `read` (o `failed`)

### escalation.created (Room: `property:{id}`)

```json
{
  "conversation_id": 123,
  "escalation_id": 456,
  "type": "manual",
  "reason": "...",
  "priority": 2
}
```

### escalation.resolved (Room: `property:{id}`)

```json
{
  "conversation_id": 123,
  "escalation_id": 456,
  "resolved_by": null,
  "resolution_medium": "manual_takeover"
}
```

### Rooms

| Room | Auto-join | Eventos recibidos |
|------|-----------|-------------------|
| `property:{id}` | Si (al conectar) | conversation.created/updated, escalation.created/resolved |
| `property:0` | Si (si property_id=0) | Conversaciones sin asignar |
| `chat:{phone_code}` | Manual (join_chat) | message.created, message.delivery_updated |

---

## 10. Nuevas funcionalidades

### Multi-canal (WhatsApp + Email)
- Cada mensaje indica su `channel`: "whatsapp" o "email"
- Mensajes email incluyen `email_metadata`: subject, from_address, from_name
- `GET /api/v1/conversations/{id}/channels` lista canales disponibles para enviar

### Transferencia de conversaciones
- `GET /api/v1/conversations/{id}/transfer-targets` — properties destino disponibles
- `POST /api/v1/conversations/{id}/transfer` — transferir con nota opcional

### Asignacion manual
- `POST /api/v1/conversations/{id}/assign` — asignar conversacion del inbox sin property

### Media attachments
- Campo `media` en mensajes: imagenes, audio, video, documentos
- Audio con `transcription` (transcripcion automatica)
- Imagenes con `vision_description` (descripcion IA)

### Templates
- Si `content` es null y `template_code` tiene valor, es un mensaje de template
- `template_payload` contiene datos tecnicos del template

### Delivery tracking en tiempo real
- Evento `message.delivery_updated` con estados: pending -> sent -> delivered -> read -> failed

---

## 11. Funcionalidades pendientes

| Funcionalidad | Endpoint legacy | Estado |
|---|---|---|
| Respuesta sugerida IA | `POST /chats/{id}/proposed-response` | Por implementar |
| Archivar conversacion | `POST /chats/{id}/archive` | Por implementar |
| Ocultar conversacion | `POST /chats/{id}/hide` | Por implementar |
| Estado ventana 24h | `GET /chats/{id}/window` | Por implementar |

---

## 12. Autenticacion

Sin cambios respecto a legacy:

- **REST API**: Header `Authorization: Bearer <token>`
- **Socket.IO**: `auth: { token: "<token>", property_id: X }`
- Tokens por instancia (un token por hotel/cadena)

---

## 13. Codigos de error

| Codigo | Significado |
|--------|-------------|
| 200 | OK |
| 201 | Creado |
| 204 | Sin contenido (exito) |
| 400 | Request invalido |
| 401 | Token invalido o ausente |
| 404 | No encontrado |
| 409 | Conflicto (duplicado, ya asignado) |
| 422 | Error de logica (ventana cerrada, property sin canal) |
| 502 | Error del proveedor de canal (Meta/Mailgun) |

Formato de error:
```json
{ "detail": "Mensaje legible del error" }
```
