# BookAI — Fase 1

Documento de referencia permanente para la implementación de la Fase 1.
Refleja el estado del código en la rama `main` (último commit: `8c9b9c7`).

---

## Resumen ejecutivo

BookAI es un microservicio Python que actúa como backend de canal conversacional entre:

- **Roomdoo/Odoo** (PMS hotelero del cliente) — fuente de reservas y operadores
- **WhatsApp Business API (Meta Cloud API)** — canal de mensajería con el huésped
- **App cliente** — interfaz de operador con actualizaciones en tiempo real vía WebSocket

En la Fase 1, BookAI funciona exclusivamente como **backend de mensajería transaccional y tiempo real**. No incluye agentes, LLMs ni lógica de IA generativa.

---

## Arquitectura: decisiones no negociables

| Decisión | Detalle |
|---|---|
| Código legacy aislado | `legacy/` solo como referencia, sin imports desde `app/` |
| Sin migración de datos | Tablas nuevas, sin datos del esquema anterior |
| Sin LangChain / LLM | No hay dependencias de IA generativa en Fase 1 |
| Integraciones detrás de adaptadores | WhatsApp aislado en `WhatsAppClient`; Roomdoo en SDK externo |
| Persistencia principal | PostgreSQL vía SQLAlchemy 2.x async + Alembic |
| Sin memoria de proceso | Las conversaciones y sesiones viven solo en BD |
| Sin brokers externos | No hay RabbitMQ ni colas de mensajes |
| SDK PMS separado | El contrato está en `sdk_contract/`; la implementación en otro repo |

---

## Modelo de datos

### Tablas principales

#### `instances`
Instalación de Roomdoo/Odoo. Unidad de autenticación.

| Columna | Tipo | Notas |
|---|---|---|
| `id` | Integer PK | |
| `instance_url` | String | URL base del Odoo del cliente |
| `bearer_token` | String unique | Token de autenticación para la API REST |
| `bookai_enabled` | Boolean | Interruptor global por instancia |
| `active` | Boolean | |
| `created_at` | DateTime | |

#### `properties`
Hotel perteneciente a una instancia.

| Columna | Tipo | Notas |
|---|---|---|
| `id` | Integer PK | |
| `instance_id` | FK → instances | |
| `name` | String | Nombre del hotel |
| `roomdoo_external_code` | String | Código en Roomdoo/Odoo (NOT NULL) |
| `channel_endpoint_id` | FK → channel_endpoints nullable | Canal WhatsApp asignado |

#### `channel_endpoints`
Número de WhatsApp Business (una fila por número de Meta).

| Columna | Tipo | Notas |
|---|---|---|
| `id` | Integer PK | |
| `channel` | String | Tipo de canal (actualmente siempre `"whatsapp"`) |
| `external_code` | String unique | `phone_number_id` de Meta |
| `access_token` | String | Token de acceso de Meta |
| `account_id` | String | WhatsApp Business Account ID |
| `verify_token` | String | Token para la verificación del webhook |
| `mock_mode` | Boolean | Si True, omite llamadas reales a Meta |
| `display_number` | String | Número legible (e.g. `+34 600 000 001`) |

> **Unicidad**: `external_code` es `unique=True` — el mismo `phone_number_id` de Meta no puede existir en dos instancias distintas.

#### `contacts`
Identidad del huésped en el canal (número de teléfono E.164 sin `+`).

| Columna | Tipo | Notas |
|---|---|---|
| `id` | Integer PK | |
| `phone_code` | String unique | E.164 sin `+`, e.g. `34699123456` |
| `display_name` | String nullable | Nombre del perfil de WhatsApp |
| `country_code` | String nullable | ISO 3166-1 alpha-2 |

#### `conversations`
Hilo lógico entre BookAI y un contacto. Uno por contacto.

| Columna | Tipo | Notas |
|---|---|---|
| `id` | Integer PK | |
| `contact_id` | FK → contacts unique | Un contacto = una conversación |
| `created_at` | DateTime | |
| `updated_at` | DateTime | Actualizado por triggers/ORM |

#### `conversation_channel_states`
Estado por canal dentro de una conversación (ventana de 24h de WhatsApp).

| Columna | Tipo | Notas |
|---|---|---|
| `conversation_id` | FK PK | |
| `channel_endpoint_id` | FK PK | |
| `last_inbound_at` | DateTime nullable | Último inbound recibido en este canal |

#### `conversation_reads`
Cursor de lectura por propiedad. Soporta `unread_count`.

| Columna | Tipo | Notas |
|---|---|---|
| `conversation_id` | FK PK | |
| `property_id` | Integer PK | |
| `last_read_at` | DateTime | Momento del último `PATCH /read` |

#### `attention_sessions`
Contexto operativo: une una Conversation con una Property por un período acotado.

| Columna | Tipo | Notas |
|---|---|---|
| `id` | Integer PK | |
| `conversation_id` | FK → conversations | |
| `property_id` | FK → properties | Hotel que atiende esta conversación |
| `status` | Enum `active` / `closed` | |
| `opened_at` | DateTime | |
| `closed_at` | DateTime nullable | |

#### `messages`
Log unificado de mensajes (templates, chat y estado de entrega).

| Columna | Tipo | Notas |
|---|---|---|
| `id` | BigInteger PK autoincrement | |
| `conversation_id` | FK | |
| `channel_endpoint_id` | FK | Siempre presente |
| `attention_session_id` | FK nullable | Sesión activa al procesar el mensaje |
| `direction` | Enum `inbound` / `outbound` | |
| `sender` | Enum `guest` / `agent` / `system` | |
| `content` | Text nullable | Texto en idioma original |
| `content_language` | String nullable | BCP-47 (e.g. `"es"`, `"zh"`) |
| `agent_user_id` | Integer nullable | Solo cuando `sender=agent` |
| `agent_display_name` | String nullable | Solo cuando `sender=agent` |
| `wa_message_id` | String unique nullable | ID de mensaje de Meta |
| `wa_message_type` | String | Default `"text"` |
| `template_code` | String nullable | Solo mensajes de plantilla |
| `template_language` | String nullable | |
| `template_payload` | JSONB nullable | Payload completo enviado a Meta |
| `routing_status` | Enum nullable | `routed` / `unassigned` / `ambiguous` |
| `idempotency_key` | String unique nullable | Para templates de Roomdoo |
| `delivery_status` | Enum | `pending` / `sent` / `delivered` / `read` / `failed` |
| `delivery_error` | Text nullable | Error de Meta si `status=failed` |
| `delivered_at` | DateTime nullable | |
| `created_at` | DateTime | |

#### `message_translations`
Traducciones cacheadas de mensajes.

| Columna | Tipo | Notas |
|---|---|---|
| `message_id` | FK PK | |
| `language` | String PK | BCP-47 |
| `content` | Text | Traducción |

#### `folios`
Reserva del PMS (push desde Roomdoo).

| Columna | Tipo | Notas |
|---|---|---|
| `id` | Integer PK | |
| `odoo_external_code` | String unique | Código en Odoo |
| `odoo_folio_id` | Integer nullable | ID numérico en Odoo |
| `checkin_date` | Date nullable | |
| `checkout_date` | Date nullable | |
| `status` | Enum | `draft` / `confirm` / `onboard` / `done` / `cancel` |
| `pending_payment_amount` | Numeric nullable | |
| `pending_payment_currency` | String nullable | |
| `synced_at` | DateTime nullable | Última sincronización con Roomdoo |

#### `whatsapp_templates` / `whatsapp_template_translations`
Familias de plantillas y sus variantes por idioma, escopadas por property.

---

### Relaciones clave

```
Instance ──< Properties ──> ChannelEndpoint
                │
                └──< AttentionSessions ──> Conversation ──> Contact
                         │                     │
                         └──< SessionFolios    └──< Messages
                                  │            └──< ConversationChannelStates
                                  └──> Folios  └──< ConversationReads
```

---

## Flujos implementados

### Flow 1: Template (Roomdoo → WhatsApp)

```
Roomdoo  →  POST /api/v1/whatsapp/send-template
              │
              ├─ Idempotency check (idempotency_key)
              ├─ Resolve Property (roomdoo_external_code)
              ├─ Resolve Template translation (code + language + property)
              ├─ Resolve ChannelEndpoint
              ├─ Normalize phone (E.164 via phonenumbers lib)
              ├─ get_or_create Contact + Conversation
              ├─ get_or_create_active AttentionSession
              ├─ get_or_create Folio + attach to session (opcional)
              ├─ Persist Message (delivery_status=pending)
              ├─ Send via WhatsAppClient.send_template()
              ├─ Update delivery_status (sent | failed)
              ├─ db.commit()
              └─ Emit Socket.IO: conversation.created (si nueva) + message.created
                    → room: property:{id}
```

### Flow 2: Inbound (WhatsApp → BookAI)

```
Meta  →  POST /webhook/whatsapp
           │
           └─ 200 OK inmediato (satisface timeout de 5s de Meta)
                │
                └─ Background task: process_inbound_webhook()
                     │
                     ├─ Deduplicación por wa_message_id
                     ├─ Resolve ChannelEndpoint (por phone_number_id)
                     ├─ get_or_create Contact + Conversation
                     ├─ Update last_inbound_at (ventana 24h)
                     ├─ Query properties del endpoint
                     ├─ [ROUTING] Determinar routing_status + attention_session_id
                     ├─ Persist Message
                     ├─ db.commit()
                     ├─ mark_read (fire-and-forget)
                     └─ Emit Socket.IO:
                          · message.created → room: chat:{phone_code}
                          · conversation.created/updated → room: property:{id} | property:0

         Meta  →  POST /webhook/whatsapp (delivery status)
                    └─ Background task: _process_status_update()
                         ├─ find message by wa_message_id
                         ├─ update delivery_status
                         ├─ db.commit()
                         └─ Emit: message.delivery_updated → chat:{phone_code}
```

### Flow 3: Agent Reply (App → WhatsApp)

```
App  →  POST /api/v1/chatter/send-message
          │
          ├─ Resolve Conversation (404 si no existe)
          ├─ Resolve ChannelEndpoint (explícito o el más reciente)
          ├─ Verificar ventana de 24h (422 si cerrada)
          ├─ find active AttentionSession (para contexto de routing)
          ├─ Send via WhatsAppClient.send_text()
          ├─ Persist Message (outbound, sender=agent)
          ├─ db.commit()
          └─ Emit Socket.IO:
               · message.created → room: chat:{phone_code}
               · conversation.updated → room: property:{id} (si hay sesión activa)
```

---

## API REST

Todos los endpoints REST requieren `Authorization: Bearer <token>` salvo los webhooks.

| Método | Ruta | Propósito | Auth |
|---|---|---|---|
| `GET` | `/health` | Health check | No |
| `POST` | `/api/v1/whatsapp/send-template` | Flow 1: enviar plantilla a huésped | Bearer |
| `POST` | `/api/v1/chatter/send-message` | Flow 3: operador responde al huésped | Bearer |
| `GET` | `/api/v1/conversations/` | Bandeja de conversaciones para una property | Bearer |
| `GET` | `/api/v1/conversations/search` | Buscar conversaciones por nombre/folio/estado | Bearer |
| `GET` | `/api/v1/conversations/{id}/messages` | Historial de mensajes (paginado por cursor) | Bearer |
| `PATCH` | `/api/v1/conversations/{id}/read` | Marcar conversación como leída | Bearer |
| `PATCH` | `/api/v1/folios/{code}` | Actualizar caché de reserva desde Roomdoo | Bearer |
| `GET` | `/webhook/whatsapp` | Verificación del webhook de Meta | No |
| `POST` | `/webhook/whatsapp` | Eventos inbound de Meta (mensajes + delivery) | No |
| `GET` | `/dev-ui/` | Interfaz de desarrollo (HTML estático) | No |

### Parámetros relevantes

**GET /conversations/**
- `property_id` (requerido): ID de la property. Usar `0` para conversaciones sin sesión activa.
- `limit` (default 50, máx 200)

**GET /conversations/search**
- `property_id` (requerido)
- `q`: texto libre — coincide con nombre del huésped o código de folio (ILIKE + unaccent)
- `status`: estado de reserva (`draft` / `confirm` / `onboard` / `done` / `cancel`)
- Al menos uno de `q` o `status` es obligatorio (400 si ninguno)

**GET /conversations/{id}/messages**
- `language`: BCP-47 opcional. Si hay traducción cacheada, devuelve `is_translated=true`
- `limit` (default 50, máx 200), `before_id` (cursor de paginación)

**PATCH /conversations/{id}/read**
- `property_id` (requerido): la property que marca como leída

---

## Socket.IO

### Autenticación

El cliente debe pasar en el objeto `auth` del handshake:

```json
{ "token": "<bearer_token>", "property_id": 1 }
```

- `token`: mismo Bearer token que la API REST
- `property_id`: ID de la property a suscribir. Usar `0` para el inbox de admin (conversaciones sin sesión)

La conexión es rechazada si el token es inválido o la property no pertenece a la instancia.

### Rooms

| Room | Gestión | Propósito |
|---|---|---|
| `property:{id}` | Automática (al conectar) | Inbox de la property — eventos de conversación |
| `property:0` | Automática (si `property_id=0`) | Inbox admin para conversaciones sin sesión activa |
| `chat:{phone_code}` | Manual (`join_chat` / `leave_chat`) | Vista de conversación individual |

### Eventos Cliente → Servidor

| Evento | Payload | Descripción |
|---|---|---|
| `join_chat` | `{ "phone_code": "34699123456" }` | Entrar al room de una conversación |
| `leave_chat` | `{ "phone_code": "34699123456" }` | Salir del room de una conversación |

### Eventos Servidor → Cliente

**Room `property:{id}` y `property:0`**

| Evento | Cuándo | Payload |
|---|---|---|
| `conversation.created` | Primer mensaje de una conversación nueva | Ver abajo |
| `conversation.updated` | Nuevo mensaje en conversación existente | Ver abajo |

Payload de `conversation.created` / `conversation.updated`:
```json
{
  "id": 42,
  "created_at": "2026-03-15T10:00:00",
  "updated_at": "2026-03-30T14:23:00",
  "unread_count": 3,
  "contact": {
    "id": 7,
    "phone_code": "34699123456",
    "display_name": "María García"
  },
  "last_message": {
    "id": 198,
    "direction": "inbound",
    "sender": "guest",
    "content": "¿A qué hora es el check-in?",
    "created_at": "2026-03-30T14:23:00"
  }
}
```

**Room `chat:{phone_code}`**

| Evento | Cuándo | Payload |
|---|---|---|
| `message.created` | Mensaje persistido (cualquier dirección) | Ver abajo |
| `message.delivery_updated` | Estado de entrega cambiado | Ver abajo |

Payload de `message.created`:
```json
{
  "id": 198,
  "conversation_id": 42,
  "channel_endpoint_id": 1,
  "direction": "inbound",
  "sender": "guest",
  "content": "¿A qué hora es el check-in?",
  "content_language": "es",
  "wa_message_id": "wamid.HBgM...",
  "wa_message_type": "text",
  "delivery_status": "delivered",
  "routing_status": "routed",
  "template_code": null,
  "agent_user_id": null,
  "agent_display_name": null,
  "created_at": "2026-03-30T14:23:00",
  "contact": {
    "id": 7,
    "phone_code": "34699123456",
    "display_name": "María García"
  }
}
```

Payload de `message.delivery_updated`:
```json
{
  "id": 198,
  "conversation_id": 42,
  "wa_message_id": "wamid.HBgM...",
  "delivery_status": "read",
  "delivery_error": null
}
```

---

## Routing de mensajes inbound

Cuando llega un mensaje en el Flow 2, el servicio determina a qué property y Socket.IO room emitir:

| Situación | `routing_status` | `attention_session_id` | Room Socket.IO |
|---|---|---|---|
| 1 sesión activa | `routed` | `session.id` | `property:{session.property_id}` |
| 0 sesiones + 1 property en el endpoint | `routed` | **auto-creada** | `property:{property_id}` |
| 0 sesiones + N > 1 properties en el endpoint | `unassigned` | `null` | `property:0` |
| N > 1 sesiones activas | `ambiguous` | `null` | `property:0` |

**Auto-sesión**: cuando un número de WhatsApp solo tiene una property asociada (`channel_endpoint.properties`), se crea automáticamente una `AttentionSession` al llegar el primer mensaje. No hay ambigüedad.

**`property:0`**: room virtual para el inbox de administración. Recibe las conversaciones que no están asignadas a ninguna property concreta (casos `unassigned` y `ambiguous`).

---

## Unread counts

### Mecanismo

Cada property tiene un cursor de lectura por conversación, almacenado en `conversation_reads`:

```
conversation_reads(conversation_id, property_id) → last_read_at
```

`get_unread_counts(conversation_ids, property_id)` devuelve un `dict[int, int]` con el número de mensajes `inbound` creados **después** de `last_read_at` para cada conversación.

- Si no existe registro en `conversation_reads` (nunca se leyó), **todos** los mensajes inbound cuentan.
- Los mensajes `outbound` nunca se cuentan como unread.

### Actualización

- **PATCH `/conversations/{id}/read?property_id=X`**: hace upsert del cursor a `now()`. Devuelve 204.
- Los eventos `conversation.created` y `conversation.updated` de Socket.IO incluyen el `unread_count` calculado en el momento de la emisión para la property destinataria.

### En el cliente

El dev UI llama a `PATCH /read` automáticamente al abrir una conversación y al recibir un `message.created` en ella.

---

## mock_mode y multi-cuenta WhatsApp

### mock_mode

`channel_endpoints.mock_mode = true` hace que `WhatsAppClient` omita las llamadas reales a Meta:

- `send_template()` y `send_text()` devuelven un ID falso: `wamid.mock.<12-hex>`
- `mark_read()` no hace ninguna llamada
- El resto del flujo (persistencia, Socket.IO) funciona con normalidad

Útil para desarrollo local y tests de integración sin cuenta real de WhatsApp.

### Multi-cuenta

Cada `ChannelEndpoint` tiene sus propias credenciales (`access_token`, `account_id`). `WhatsAppClient` recibe el endpoint en cada llamada y usa sus credenciales directamente. No hay configuración global de cuenta.

---

## Setup local

### Prerrequisitos

- Docker + Docker Compose
- Python 3.11+

### Arrancar

```bash
# 1. Variables de entorno
cp .env.example .env
# Editar DATABASE_URL si es necesario (ya configurada para docker-compose)

# 2. Levantar servicios
docker compose up -d

# 3. Aplicar migraciones
docker compose exec app alembic upgrade head

# 4. (Opcional) Datos demo
docker compose exec app python scripts/seed_demo.py
```

### Migraciones (orden cronológico)

| Revisión | Descripción |
|---|---|
| `0ae7c78a7d91` | Initial schema |
| `1b3f2a9c4d7e` | Multi-channel conversations |
| `2c5d8e1f3a6b` | Message translations |
| `3d4f1e2a8c5b` | Template strategy B |
| `4e5f6a7b8c9d` | Folio cache fields |
| `5f6a7b8c9d0e` | Folio status enum |
| `6a7b8c9d0e1f` | Channel endpoint verify_token |
| `7b8c9d0e1f2a` | Channel endpoint mock_mode |
| `8c9d0e1f2a3b` | Conversation reads (unread counts) |

### Acceso

| Servicio | URL |
|---|---|
| API REST | `http://localhost:8000` |
| Swagger UI | `http://localhost:8000/docs` |
| Dev UI | `http://localhost:8000/dev-ui/` |
| PostgreSQL | `localhost:5432` (user/pass: `bookai`) |

### Stack de Fase 1

| Capa | Tecnología |
|---|---|
| Framework | FastAPI + Uvicorn |
| ORM | SQLAlchemy 2.x async + asyncpg |
| Migraciones | Alembic |
| Validación | Pydantic v2 + pydantic-settings |
| Real-time | python-socketio |
| HTTP client | httpx (async) |
| Phone | phonenumbers |
| Testing | pytest + pytest-asyncio |
