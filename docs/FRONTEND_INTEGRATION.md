# BookAI — Guía de integración para frontend

> Audiencia: desarrollador de la app de hotel (Roomdoo / app interna).
> Versión de la API documentada: **Fase 1**

---

## Índice

1. [Autenticación](#1-autenticación)
2. [REST API](#2-rest-api)
   - [Listar bandeja](#21-get-apiv1conversations)
   - [Buscar conversaciones](#22-get-apiv1conversationssearch)
   - [Historial de mensajes](#23-get-apiv1conversationsidmessages)
   - [Marcar como leído](#24-patch-apiv1conversationsidread)
   - [Enviar mensaje (chatter)](#25-post-apiv1chattersend-message)
   - [Actualizar caché de folio](#26-patch-apiv1foliosodoo_external_code)
   - [Destinos de traspaso](#27-get-apiv1conversationsidtransfer-targets)
   - [Traspasar conversación](#28-post-apiv1conversationsidtransfer)
   - [Asignar conversación](#29-post-apiv1conversationsidassign)
   - [Notificar evento de folio](#210-post-apiv1folioscodeeventos)
3. [Socket.IO — tiempo real](#3-socketio--tiempo-real)
   - [Conexión y autenticación](#31-conexión-y-autenticación)
   - [Rooms](#32-rooms)
   - [Eventos C→S](#33-eventos-c→s-cliente-a-servidor)
   - [Eventos S→C (property room)](#34-eventos-s→c-en-la-property-room)
   - [Eventos S→C (chat room)](#35-eventos-s→c-en-la-chat-room)
   - [Payloads de referencia](#36-payloads-de-referencia)
4. [Flujos completos](#4-flujos-completos)
   - [Abrir la bandeja](#41-abrir-la-bandeja)
   - [Abrir una conversación](#42-abrir-una-conversación)
   - [Enviar un mensaje](#43-enviar-un-mensaje)
   - [Actualizar la bandeja en tiempo real](#44-actualizar-la-bandeja-en-tiempo-real)
5. [Conteo de no leídos](#5-conteo-de-no-leídos)
6. [Códigos de error comunes](#6-códigos-de-error-comunes)
7. [Roadmap de fases](#7-roadmap-de-fases)

---

## 1. Autenticación

Todas las llamadas REST llevan el header:

```
Authorization: Bearer <token>
```

El token es el `bearer_token` de la instancia Roomdoo.
Requests sin token → **401**.
Token válido pero instancia con BookAI deshabilitado → **403**.

---

## 2. REST API

Base URL: `https://<host>/api/v1`

### 2.1 `GET /api/v1/conversations/`

Bandeja de conversaciones para una property, ordenada por último mensaje (más reciente primero).

**Query params:**

| Parámetro    | Tipo    | Requerido | Descripción                                                            |
|--------------|---------|-----------|------------------------------------------------------------------------|
| `property_id`| integer | sí        | ID de la property. Usar `0` para conversaciones sin sesión asignada.   |
| `limit`      | integer | no        | Nº de resultados (default 50, máx 200).                                |

**Response 200:**

```json
{
  "property_id": 1,
  "conversations": [
    {
      "id": 42,
      "created_at": "2026-04-01T10:00:00+00:00",
      "updated_at": "2026-04-01T14:32:00+00:00",
      "contact": {
        "id": 7,
        "phone_code": "34699323583",
        "display_name": "María García"
      },
      "last_message": {
        "id": 1205,
        "direction": "inbound",
        "sender": "guest",
        "content": "¿A qué hora es el check-in?",
        "created_at": "2026-04-01T14:32:00+00:00"
      },
      "unread_count": 3
    }
  ]
}
```

**Campos:**

| Campo                    | Tipo          | Descripción                                              |
|--------------------------|---------------|----------------------------------------------------------|
| `id`                     | integer       | ID de la conversación                                    |
| `contact.phone_code`     | string        | Número de teléfono sin `+` (E.164 sin prefijo)           |
| `last_message.direction` | `inbound` / `outbound` | Sentido del último mensaje                    |
| `last_message.sender`    | `guest` / `agent` / `system` | Quién envió el último mensaje          |
| `unread_count`           | integer       | Mensajes inbound no leídos para esta property            |

---

### 2.2 `GET /api/v1/conversations/search`

Búsqueda dentro de la bandeja de una property.

**Query params:**

| Parámetro    | Tipo    | Requerido | Descripción                                                                     |
|--------------|---------|-----------|---------------------------------------------------------------------------------|
| `property_id`| integer | sí        | ID de la property                                                               |
| `q`          | string  | condicional | Texto libre contra nombre del huésped o código de folio (accent-insensitive). |
| `status`     | string  | condicional | Estado exacto del folio: `draft`, `confirm`, `onboard`, `done`, `cancel`.     |
| `limit`      | integer | no        | Default 50, máx 200                                                             |

Al menos uno de `q` o `status` es obligatorio → **400** si ambos ausentes.

**Response 200:** igual que `/conversations/`.

---

### 2.3 `GET /api/v1/conversations/{conversation_id}/messages`

Historial de mensajes de una conversación, paginado por cursor inverso.

**Path param:** `conversation_id` (integer)

**Query params:**

| Parámetro     | Tipo    | Descripción                                                                                           |
|---------------|---------|-------------------------------------------------------------------------------------------------------|
| `property_id` | integer | Si se indica, filtra **todos** los mensajes (inbound, outbound y notas) a los que pertenecen a sesiones de esta property. Usar `0` para la bandeja central (sesiones sin property). Sin este parámetro se devuelven todos los mensajes de la conversación. |
| `language`    | string  | BCP-47 (`es`, `gl`, `pt`, `en`, `fr`). Si existe traducción cacheada se devuelve; si la nota tiene `template_code` y el idioma está soportado, se genera y cachea al vuelo. |
| `limit`       | integer | Default 50, máx 200                                                                                   |
| `before_id`   | integer | Cursor: devuelve mensajes con `id < before_id` (para cargar páginas anteriores)                       |

**Response 200:**

```json
{
  "conversation_id": 42,
  "language": "es",
  "messages": [
    {
      "id": 1201,
      "conversation_id": 42,
      "channel_endpoint_id": 3,
      "direction": "outbound",
      "sender": "system",
      "content": "Hola María, su reserva está confirmada.",
      "content_language": "es",
      "is_translated": false,
      "agent_user_id": null,
      "agent_display_name": null,
      "wa_message_id": "wamid.HBgLMzQ2OTkz",
      "wa_message_type": "template",
      "delivery_status": "delivered",
      "routing_status": "routed",
      "template_code": "welcome_checkin",
      "created_at": "2026-04-01T10:00:00+00:00"
    },
    {
      "id": 1205,
      "conversation_id": 42,
      "direction": "inbound",
      "sender": "guest",
      "content": "¿A qué hora es el check-in?",
      "content_language": "es",
      "is_translated": false,
      "wa_message_type": "text",
      "delivery_status": "delivered",
      "routing_status": "routed",
      "created_at": "2026-04-01T14:32:00+00:00"
    }
  ]
}
```

**Campos clave:**

| Campo                | Valores posibles                                                   | Notas                                                    |
|----------------------|--------------------------------------------------------------------|---------------------------------------------------------|
| `kind`               | `message` / `note`                                                 | `note` = anotación interna, nunca enviada al canal       |
| `direction`          | `inbound` / `outbound`                                             | Sentido respecto a BookAI                                |
| `sender`             | `guest` / `agent` / `system`                                       | `system` = automático (plantilla o nota interna)         |
| `delivery_status`    | `pending` / `sent` / `delivered` / `read` / `failed` / `skipped`  | `skipped` = aplica solo a notas                          |
| `routing_status`     | `routed` / `unassigned` / `ambiguous` / `null`                     | Solo en mensajes inbound                                 |
| `is_translated`      | boolean                                                            | `true` = `content` es traducción (cacheada o generada)   |
| `agent_display_name` | string / null                                                      | Presente cuando `sender=agent`                           |
| `template_code`      | string / null                                                      | En notas automáticas: clave de la plantilla usada        |

**Filtrado por property (`?property_id=X`):**

Cada hotel solo debe ver los mensajes de sus propias sesiones. El parámetro `property_id` filtra **todos** los mensajes (no solo notas) a aquellos cuyo `attention_session_id` pertenece a una sesión de esa property. Esto implementa el modelo de chatter por hotel: cuando una conversación se traspasa de A→B, el hotel A sigue viendo solo sus mensajes previos y su nota de salida; el hotel B ve solo los mensajes de su sesión y su nota de entrada.

**Notas internas (`kind=note`):**

- Aparecen mezcladas en la timeline con los mensajes normales.
- `wa_message_id` siempre es `null`.
- `delivery_status` siempre es `skipped`.
- Si se pasa `?language=X` y el idioma está soportado (`es`, `gl`, `pt`, `en`, `fr`), la nota se devuelve traducida usando la plantilla original. Para idiomas no soportados se devuelve el texto original.

**Paginación:** para cargar mensajes anteriores, pasar `before_id` = el `id` del mensaje más antiguo visible.

---

### 2.4 `PATCH /api/v1/conversations/{conversation_id}/read`

Actualiza el cursor de lectura para esta property. Después de esta llamada, `unread_count` vuelve a 0.

**Query params:**

| Parámetro    | Tipo    | Requerido |
|--------------|---------|-----------|
| `property_id`| integer | sí        |

**Response:** `204 No Content`

---

### 2.5 `POST /api/v1/chatter/send-message`

Envía un mensaje de texto desde el operador del hotel al huésped.

**Cuándo usarlo:** cuando hay conversación activa y el huésped ha enviado un mensaje en las últimas 24 horas (ventana de mensajería abierta). Fuera de ventana → usar send-template.

**Body:**

```json
{
  "conversation_id": 42,
  "content": "Buenos días María, su habitación está lista.",
  "channel_endpoint_id": null,
  "agent_user_id": 7,
  "agent_display_name": "Carlos Recepción"
}
```

| Campo                  | Tipo    | Requerido | Descripción                                                        |
|------------------------|---------|-----------|--------------------------------------------------------------------|
| `conversation_id`      | integer | sí        | ID de la conversación destino                                      |
| `content`              | string  | sí        | Texto del mensaje (mínimo 1 carácter)                              |
| `channel_endpoint_id`  | integer | no        | Canal explícito. Si no se indica, se usa el canal más reciente.    |
| `agent_user_id`        | integer | no        | ID del usuario Roomdoo que envía                                   |
| `agent_display_name`   | string  | no        | Nombre visible en el chat                                          |

**Response 200:**

```json
{
  "status": "ok",
  "message_id": 1210,
  "wa_message_id": "wamid.HBgLMzQ2OTkz",
  "conversation_id": 42
}
```

**Errores:**

| Código | Causa                                              |
|--------|----------------------------------------------------|
| 404    | Conversación o canal no encontrado                 |
| 422    | Ventana de mensajería cerrada (huésped no ha respondido) |
| 502    | Error de la API del proveedor de canal               |

---

### 2.7 `GET /api/v1/conversations/{conversation_id}/transfer-targets`

Devuelve las properties de la instancia con canal asignado. Son los destinos válidos para un traspaso.

**Response 200:**

```json
{
  "conversation_id": 42,
  "properties": [
    { "id": 1, "name": "Hotel Costa Brava", "roomdoo_external_code": "HOTEL-CB-001" },
    { "id": 2, "name": "Hotel Barcelona Centro", "roomdoo_external_code": "HOTEL-BCN-001" }
  ]
}
```

| Código | Causa                         |
|--------|-------------------------------|
| 404    | Conversación no encontrada    |

---

### 2.8 `POST /api/v1/conversations/{conversation_id}/transfer`

Traspasa la conversación a otra property. Genera una nota interna en la sesión origen (si existe) y una nota en la sesión destino. La sesión origen se cierra o permanece abierta según el canal:

| Caso | Comportamiento |
|------|---------------|
| Origen y destino comparten el mismo canal | Sesión origen se **cierra**. El canal es uno solo: solo el destino gestiona la conversación. |
| Origen y destino tienen canales distintos | Sesión origen **permanece activa**. Ambas properties pueden operar en su propio canal. |
| Origen es la bandeja central (sin property) | Sesión origen se **cierra**. No tiene canal propio que mantener. |

La sesión destino siempre se crea (o reutiliza si ya existía activa).

**Body:**

```json
{
  "destination_property_id": 2,
  "note": "El huésped prefiere el hotel del centro por proximidad a reuniones."
}
```

| Campo                      | Tipo    | Requerido | Descripción                                   |
|----------------------------|---------|-----------|-----------------------------------------------|
| `destination_property_id`  | integer | sí        | ID de la property destino                     |
| `note`                     | string  | sí        | Texto explicativo (1–1000 caracteres)         |

**Response 200:**

```json
{
  "conversation_id": 42,
  "from_session_id": 12,
  "to_session_id": 15,
  "destination_property_id": 2
}
```

`from_session_id` es `null` si no había sesión activa antes del traspaso.

| Código | Causa                                                     |
|--------|-----------------------------------------------------------|
| 404    | Conversación o property destino no encontradas            |
| 422    | La conversación ya está asignada a la property destino    |

---

### 2.9 `POST /api/v1/conversations/{conversation_id}/assign`

Asigna (o devuelve) una sesión activa para una property sin crear notas de traspaso. Útil para asignación inicial desde la bandeja central.

**Body:**

```json
{ "property_id": 1 }
```

**Response 200:**

```json
{
  "conversation_id": 42,
  "property_id": 1,
  "attention_session_id": 8,
  "created": true
}
```

`created=false` si la sesión ya existía (idempotente).

---

### 2.10 `POST /api/v1/folios/{odoo_external_code}/events`

Notifica un evento del ciclo de vida de una reserva. BookAI genera una nota interna en todas las sesiones activas vinculadas al folio.

**Normalización del código externo (`odoo_external_code`):**

Los códigos de folio de Odoo pueden contener caracteres conflictivos en URLs (p.ej. `206/26/003`). BookAI normaliza automáticamente el código reemplazando los caracteres problemáticos por `_`:

| Caracteres reemplazados | Ejemplo original | Resultado normalizado |
|-------------------------|------------------|-----------------------|
| `/` `?` `#` `%` `&` `=` ` ` | `206/26/003` | `206_26_003` |

- La normalización se aplica tanto al guardar como al buscar, por lo que es transparente para el llamador.
- El SDK de Roomdoo debe usar el código normalizado al construir URLs (p.ej. `PATCH /api/v1/folios/206_26_003`). Si se envía el código original con barras, la API lo acepta igualmente y lo normaliza internamente.
- El campo `folio_code` en la respuesta siempre devuelve el código ya normalizado.

**Body según `event_type`:**

```json
// folio_created / folio_cancelled  (sin campos adicionales)
{ "event_type": "folio_created", "data": {} }

// folio_modified — modification_type requerido
{ "event_type": "folio_modified", "data": { "modification_type": "dates_changed", "checkin_date": "2026-05-10", "checkout_date": "2026-05-14" } }

// payment_registered
{ "event_type": "payment_registered", "data": { "amount": "500.00", "currency": "EUR" } }

// precheckin_completed
{ "event_type": "precheckin_completed", "data": { "guest_name": "James Smith", "room_number": "302" } }

// status_changed
{ "event_type": "status_changed", "data": { "new_status": "onboard" } }
```

`modification_type` posibles: `room_added`, `room_cancelled`, `dates_changed`, `service_added`, `room_changed`.

**Response 200:**

```json
{ "folio_code": "206_26_001", "event_type": "folio_created", "notes_created": 1 }
```

`notes_created=0` (no es error) si el folio no tiene sesiones activas vinculadas.

| Código | Causa                              |
|--------|------------------------------------|
| 404    | Folio no encontrado                |
| 422    | Payload inválido para el event_type|

---

### 2.6 `PATCH /api/v1/folios/{odoo_external_code}`

Actualiza el caché de un folio (reserva). Llamado por Roomdoo al detectar cambios en Odoo; el frontend no necesita llamarlo directamente.

> **Formato del código:** ver nota de normalización en [2.10](#210-post-apiv1foliosodoo_external_codeevents).

**Body (todos los campos opcionales):**

```json
{
  "status": "onboard",
  "checkin_date": "2026-04-10",
  "checkout_date": "2026-04-14",
  "pending_payment_amount": 150.00,
  "pending_payment_currency": "EUR"
}
```

Estados de folio posibles: `draft`, `confirm`, `onboard`, `done`, `cancel`.

---

## 3. Socket.IO — tiempo real

Librería recomendada: `socket.io-client` (v4).

### 3.1 Conexión y autenticación

```javascript
import { io } from "socket.io-client";

const socket = io("https://<host>", {
  path: "/socket.io",
  auth: {
    token: "<bearer_token>",
    property_id: 1           // integer; usar 0 para conversaciones sin asignar
  },
  transports: ["websocket"]
});

socket.on("connect", () => console.log("connected:", socket.id));
socket.on("connect_error", (err) => console.error("auth failed:", err.message));
```

Si el token no es válido o `property_id` no pertenece a la instancia, el servidor rechaza la conexión (el evento `connect_error` se dispara).

### 3.2 Rooms

| Room                 | Gestión       | Cuándo se entra                                   | Qué se recibe                    |
|----------------------|---------------|---------------------------------------------------|----------------------------------|
| `property:{id}`      | Automática    | Al conectar (según `property_id` del auth)        | Eventos de bandeja               |
| `property:0`         | Automática    | Al conectar con `property_id=0`                   | Conversaciones sin asignar       |
| `chat:{phone_code}`  | Manual        | Al abrir una conversación (`join_chat`)           | Mensajes de esa conversación     |

### 3.3 Eventos C→S (cliente a servidor)

```javascript
// Entrar al chat de un huésped
socket.emit("join_chat", { phone_code: "34699323583" });

// Salir del chat al navegar a otra conversación
socket.emit("leave_chat", { phone_code: "34699323583" });
```

### 3.4 Eventos S→C en la property room

#### `conversation.created`
Nueva conversación detectada (primer mensaje).

```json
{
  "id": 42,
  "created_at": "2026-04-01T10:00:00+00:00",
  "updated_at": null,
  "unread_count": 1,
  "contact": {
    "id": 7,
    "phone_code": "34699323583",
    "display_name": "María García"
  },
  "last_message": {
    "id": 1205,
    "direction": "inbound",
    "sender": "guest",
    "content": "Hola, ¿cuándo es el check-in?",
    "created_at": "2026-04-01T10:00:00+00:00"
  }
}
```

#### `conversation.updated`
Nuevo mensaje en una conversación existente. Mismo payload que `conversation.created`.

### 3.5 Eventos S→C en la chat room

#### `message.created`
Un mensaje fue persistido (cualquier dirección).

```json
{
  "id": 1210,
  "conversation_id": 42,
  "channel_endpoint_id": 3,
  "direction": "outbound",
  "sender": "agent",
  "content": "Su habitación está lista.",
  "content_language": "es",
  "wa_message_id": "wamid.HBgLMzQ2OTkz",
  "wa_message_type": "text",
  "delivery_status": "sent",
  "routing_status": "routed",
  "template_code": null,
  "agent_user_id": 7,
  "agent_display_name": "Carlos Recepción",
  "created_at": "2026-04-01T15:00:00+00:00"
}
```

#### `message.delivery_updated`
Cambio de estado de entrega (Meta notifica delivered/read/failed).

```json
{
  "id": 1210,
  "conversation_id": 42,
  "wa_message_id": "wamid.HBgLMzQ2OTkz",
  "delivery_status": "delivered",
  "delivery_error": null
}
```

### 3.6 Payloads de referencia

**`direction`:** `"inbound"` (huésped → BookAI) | `"outbound"` (BookAI → huésped)

**`sender`:** `"guest"` | `"agent"` | `"system"` (envío automático desde Roomdoo)

**`delivery_status`:** `"pending"` → `"sent"` → `"delivered"` → `"read"` | `"failed"`

**`routing_status`** (solo mensajes inbound):
- `"routed"` — asignado a una property
- `"unassigned"` — múltiples properties candidatas, requiere asignación manual
- `"ambiguous"` — múltiples sesiones activas simultáneas

---

## 4. Flujos completos

### 4.1 Abrir la bandeja

```javascript
// 1. Conectar Socket.IO
const socket = io(host, { auth: { token, property_id } });

// 2. Suscribirse a eventos de bandeja
socket.on("conversation.created", (conv) => prependToInbox(conv));
socket.on("conversation.updated", (conv) => updateInboxItem(conv));

// 3. Cargar estado inicial
const response = await fetch(`/api/v1/conversations/?property_id=${propertyId}`, {
  headers: { Authorization: `Bearer ${token}` }
});
const { conversations } = await response.json();
renderInbox(conversations);
```

### 4.2 Abrir una conversación

```javascript
async function openConversation(conversation) {
  // 1. Entrar al chat room
  socket.emit("join_chat", { phone_code: conversation.contact.phone_code });

  // 2. Suscribirse a mensajes
  socket.on("message.created", (msg) => {
    if (msg.conversation_id === conversation.id) appendMessage(msg);
  });
  socket.on("message.delivery_updated", (update) => {
    updateDeliveryStatus(update);
  });

  // 3. Cargar historial
  const response = await fetch(
    `/api/v1/conversations/${conversation.id}/messages`,
    { headers: { Authorization: `Bearer ${token}` } }
  );
  const { messages } = await response.json();
  renderMessages(messages);

  // 4. Marcar como leído
  await fetch(
    `/api/v1/conversations/${conversation.id}/read?property_id=${propertyId}`,
    { method: "PATCH", headers: { Authorization: `Bearer ${token}` } }
  );
}

async function closeConversation(conversation) {
  socket.emit("leave_chat", { phone_code: conversation.contact.phone_code });
}
```

### 4.3 Enviar un mensaje

```javascript
async function sendMessage(conversationId, content) {
  const response = await fetch("/api/v1/chatter/send-message", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`
    },
    body: JSON.stringify({
      conversation_id: conversationId,
      content,
      agent_user_id: currentUser.id,
      agent_display_name: currentUser.name
    })
  });

  if (!response.ok) {
    if (response.status === 422) {
      // Ventana cerrada: ofrecer envío de plantilla en su lugar
      showWindowClosedWarning();
    }
    return;
  }
  // El mensaje aparecerá en el chat vía evento message.created (no renderizar aquí)
}
```

### 4.4 Actualizar la bandeja en tiempo real

Los eventos `conversation.created` y `conversation.updated` llevan el campo `unread_count` actualizado. Úsalos directamente para refrescar la bandeja sin necesidad de recargar:

```javascript
socket.on("conversation.updated", (conv) => {
  const existing = inbox.find(c => c.id === conv.id);
  if (existing) {
    Object.assign(existing, conv);   // actualizar in-place
    sortInboxByLastMessage();
  } else {
    inbox.unshift(conv);             // nueva conversación al principio
  }
  renderInbox();
});
```

---

## 5. Conteo de no leídos

`unread_count` en cada `ConversationListItem` refleja los mensajes `inbound` recibidos **después** del último `PATCH /read` de esa property.

- Si nunca se ha llamado a `/read`: todos los mensajes inbound cuentan como no leídos.
- La llamada a `/read` establece el cursor al momento actual.
- Los mensajes `outbound` nunca cuentan.
- El conteo es por property: cada property tiene su propio cursor independiente.

**Patrón recomendado:**
1. Al abrir una conversación → llamar `PATCH /read` inmediatamente.
2. Mientras la conversación está abierta → los nuevos `message.created` inbound no incrementan el contador (el usuario los está viendo en tiempo real).
3. Al cerrar la conversación → opcionalmente llamar `PATCH /read` de nuevo para garantizar que quede a 0.

---

## 6. Códigos de error comunes

| Código | Situación                                             |
|--------|-------------------------------------------------------|
| 400    | Parámetros requeridos ausentes (ej. `q` en search)    |
| 401    | Token ausente o inválido                              |
| 403    | BookAI deshabilitado para esta instancia              |
| 404    | Conversación, property o template no encontrados      |
| 422    | Ventana de mensajería cerrada / teléfono inválido     |
| 502    | Error en la API del proveedor de canal                |

---

## 7. Roadmap de fases

### Fase 1 (actual)

Mensajería transaccional básica:
- Envío de plantillas desde Roomdoo → canal configurado
- Recepción de mensajes de huéspedes y enrutamiento a properties
- Respuesta del agente desde la app
- Conteo de no leídos y búsqueda en bandeja
- Tiempo real vía Socket.IO

### Fase 2 (próxima)

Agentes y escalaciones:
- Respuesta automática configurable por property y conversación
- Escalación a agente humano con notificación
- Nuevos eventos Socket.IO: `session.escalated`, ...
- Nuevos endpoints: configuración de agentes por property

### Fase 3 (planificada)

Agente interno Roomdoo:
- Integración del agente IA con acceso a datos del PMS (folios, disponibilidad, precios)
- Sugerencias de respuesta en la interfaz del operador
- Historial de acciones del agente como mensajes `system` en el hilo
