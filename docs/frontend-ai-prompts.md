# Prompts para asistentes IA — Migracion frontend BookAI

Usa estos prompts con tu agente de IA para guiarlo en la adaptacion del frontend. Cada prompt cubre un area funcional independiente. Ejecutalos en orden.

---

## Prompt 1: Adaptar servicio de API (identificadores y rutas)

```
Necesito migrar el frontend de BookAI de la API legacy a la nueva API. El cambio mas critico es que TODOS los identificadores cambian:

ANTES: chat_id era un string (telefono del huesped, ej: "34699323583")
AHORA: conversation_id es un int (ID numerico de DB, ej: 123)

Rutas que cambian:
- GET /chats → GET /api/v1/conversations/?property_id=X&limit=50
- GET /chats/{chat_id}/messages → GET /api/v1/conversations/{conversation_id}/messages?limit=50&property_id=X
- POST /messages → POST /api/v1/chatter/send-message
- PATCH /chats/{chat_id}/bookai → PATCH /api/v1/conversations/{id}/ai?property_id=X&ai_enabled=true
- PATCH /chats/{chat_id}/read → PATCH /api/v1/conversations/{id}/read?property_id=X

Busca en el codigo todos los usos de chat_id como identificador de conversacion y reemplazalos por conversation_id (int). El phone_code del contacto se sigue usando SOLO para Socket.IO rooms (chat:{phone_code}).

Busca todas las llamadas a la API legacy y actualiza:
1. Las URLs al nuevo formato con prefijo /api/v1/
2. Los parametros de request al nuevo schema
3. Los campos de respuesta al nuevo formato

El conversation_id viene en la respuesta del listado de conversaciones como conversations[].id.
El phone_code viene en conversations[].contact.phone_code.
```

---

## Prompt 2: Adaptar inbox (listado de conversaciones)

```
Migra el componente de inbox/lista de conversaciones. La respuesta de la API cambia de formato:

ANTES (GET /chats):
{
  page, page_size,
  items: [{ conversation_id, content, created_at, client_name, channel, property_id, ... }]
}

AHORA (GET /api/v1/conversations/?property_id=X&limit=50):
{
  property_id: 5,
  conversations: [{
    id: 123,                         // USAR ESTE como identificador
    created_at, updated_at,
    contact: {
      id: 789,
      phone_code: "34699323583",     // para Socket.IO rooms
      display_name: "Maria Garcia"   // nombre del contacto
    },
    last_message: {
      id: 456,
      direction: "inbound",          // "inbound" o "outbound"
      sender: "guest",               // "guest", "agent", "ai", "system"
      content: "...",
      template_code: null,           // si no es null, es un mensaje de template
      created_at: "..."
    },
    unread_count: 2,                 // contador per-property
    needs_attention: false,
    ai_enabled: true,
    has_pending_escalation: false
  }]
}

Cambios a aplicar:
1. client_name → contact.display_name
2. content (ultimo mensaje) → last_message.content
3. Si last_message.content es null y last_message.template_code existe → mostrar "Plantilla: {template_code}"
4. No hay paginacion page/page_size → usar limit
5. property_id es OBLIGATORIO en la query
6. Para inbox sin asignar: property_id=0
7. Busqueda es endpoint separado: GET /api/v1/conversations/search?property_id=X&q=texto

Para actualizaciones en tiempo real, el socket event cambia:
- ANTES: chat.updated
- AHORA: conversation.updated (mismo room property:{id})
- NUEVO: conversation.created (nueva conversacion)
```

---

## Prompt 3: Adaptar historial de mensajes

```
Migra el componente de historial de mensajes. Los campos cambian significativamente:

ANTES: role (user/assistant), read_status, user_id, user_first_name, structured_payload, escalation_reason
AHORA: direction (inbound/outbound), sender (guest/agent/ai/system), delivery_status, agent_user_id, agent_display_name

Endpoint: GET /api/v1/conversations/{conversation_id}/messages?limit=50&property_id=X

Mapping de campos:
- role="user" → direction="inbound", sender="guest"
- role="assistant" (operador) → direction="outbound", sender="agent"
- role="assistant" (IA) → direction="outbound", sender="ai"
- role="assistant" (sistema) → direction="outbound", sender="system"
- read_status → ELIMINADO (usar unread_count a nivel conversacion)
- user_id → agent_user_id
- user_first_name + user_last_name → agent_display_name (nombre completo)
- structured_payload → ELIMINADO
- escalation_reason → ELIMINADO (escalaciones son entidades separadas)

Campos NUEVOS que necesitan renderizado:
- channel: "whatsapp" o "email" → mostrar indicador de canal
- sender="ai" → mostrar etiqueta "IA" en el mensaje
- content_language: idioma detectado del mensaje
- is_translated: si el contenido es una traduccion
- template_code + template_payload: si content es null, mostrar info del template
- email_metadata: { subject, from_address, from_name } → renderizar cabecera de email
- media: array de adjuntos → renderizar segun media_type:
  - image: mostrar imagen con vision_description si existe
  - audio: reproductor con transcription si existe
  - video: reproductor de video
  - document: enlace de descarga con filename

Paginacion: cursor-based con before_id en vez de page/page_size.
Para cargar mensajes anteriores: GET ...?before_id={id_del_mensaje_mas_antiguo}&limit=50

Para mensajes en tiempo real:
- ANTES: escuchar chat.message.created
- AHORA: escuchar message.created (room chat:{phone_code})
- NUEVO: escuchar message.delivery_updated para actualizar ticks de entrega
```

---

## Prompt 4: Adaptar envio de mensajes (operador)

```
Migra el componente de envio de mensajes del operador.

ANTES (POST /messages):
{
  chat_id: "34699323583",        // telefono
  message: "Hola...",
  user_id: 5,
  user_first_name: "Ana",
  user_last_name: "Garcia",
  channel: "whatsapp",
  property_id: 1
}

AHORA (POST /api/v1/chatter/send-message):
{
  conversation_id: 123,           // int, no telefono
  content: "Hola...",             // renombrado de "message"
  agent_user_id: 5,               // renombrado de "user_id"
  agent_display_name: "Ana Garcia", // nombre completo
  channel_endpoint_id: 10         // OPCIONAL: ID del canal especifico
}

Respuesta:
{
  status: "ok",
  message_id: 1205,
  wa_message_id: "wamid.HBg...",
  conversation_id: 123
}

Cambios:
1. chat_id (string telefono) → conversation_id (int)
2. message → content
3. user_id/user_first_name/user_last_name → agent_user_id + agent_display_name
4. channel (string "whatsapp") → channel_endpoint_id (int, opcional)
5. Error 422 = ventana de 24h cerrada, solo templates disponibles

Para multi-canal: GET /api/v1/conversations/{id}/channels?property_id=X
devuelve los canales disponibles:
[{ id: 10, channel: "whatsapp", display: "+34 900 123 456" },
 { id: 11, channel: "email", display: "bookings@hotel.com" }]
Si hay varios canales, mostrar selector. Pasar channel_endpoint_id del seleccionado.
```

---

## Prompt 5: Adaptar Socket.IO

```
Migra la conexion y eventos de Socket.IO.

CONEXION:
// ANTES:
const socket = io("/ws", { auth: { token: "Bearer xxx" } });
socket.emit("join", { rooms: ["chat:34699323583", "property:1"] });
socket.emit("leave", { rooms: ["chat:34699323583"] });

// AHORA:
const socket = io("", {
  auth: { token: "mi-token", property_id: 5 }  // property_id en auth
});
// Auto-join a property:{property_id} al conectar
socket.emit("join_chat", { phone_code: "34699323583" });  // al abrir chat
socket.emit("leave_chat", { phone_code: "34699323583" }); // al cerrar chat

EVENTOS RENOMBRADOS:
- chat.message.created → message.created
- chat.message.new → message.created (consolidado)
- chat.updated → conversation.updated

EVENTOS NUEVOS (añadir listeners):
- conversation.created → nueva conversacion (añadir al inbox)
- message.delivery_updated → actualizar icono de entrega (sent/delivered/read/failed)
- escalation.created → mostrar badge/notificacion de escalacion
- escalation.resolved → quitar badge de escalacion

ROOMS:
- property:{id} → auto-join, recibe: conversation.created/updated, escalation.*
- chat:{phone_code} → manual join/leave, recibe: message.created, message.delivery_updated

PAYLOAD de message.created:
{
  id, conversation_id, channel_endpoint_id, channel,
  direction, sender, content, content_language,
  wa_message_id, wa_message_type, delivery_status,
  template_code, template_payload,
  agent_user_id, agent_display_name, created_at
}
sender puede ser: "guest", "agent", "ai", "system"
Renderizar "ai" con etiqueta visual diferente a "agent".

PAYLOAD de conversation.updated:
{
  id, created_at, updated_at, unread_count, needs_attention,
  contact: { id, phone_code, display_name },
  last_message: { id, direction, sender, content, template_code, created_at }
}
Usar para actualizar el inbox sin recargar.
```

---

## Prompt 6: Adaptar escalaciones

```
Las escalaciones en la nueva API son entidades independientes, no campos dentro de mensajes.

ANTES: escalation_reason era un campo en cada mensaje
AHORA: escalaciones tienen su propio CRUD

Endpoints:
- GET /api/v1/escalations?property_id=X&status=pending → listar pendientes
- GET /api/v1/conversations/{id}/escalations → escalaciones de una conversacion (con timeline)
- PATCH /api/v1/escalations/{escalation_id}/resolve → resolver

Modelo de escalacion:
{
  id: 123,
  conversation_id: 456,
  escalation_type: "manual|info_not_found|bad_response|inappropriate",
  reason: "Descripcion del motivo",
  priority: 1-4,      // 1=manual, 2=info, 3=bad_response, 4=inappropriate
  status: "pending|resolved",
  created_at: "...",
  resolved_at: null,
  messages: [...]      // timeline (solo en endpoint por conversacion)
}

Para resolver:
PATCH /api/v1/escalations/{id}/resolve
{
  resolution_medium: "whatsapp|phone|in_person|ai_supervised|manual_takeover|other",
  resolution_notes: "Texto libre"
}

Socket.IO events nuevos (room property:{id}):
- escalation.created → { conversation_id, escalation_id, type, reason, priority }
- escalation.resolved → { conversation_id, escalation_id, resolved_by, resolution_medium }

El frontend deberia:
1. Mostrar badge/contador de escalaciones pendientes en el inbox
2. Al abrir una conversacion con escalacion, mostrar el bloque de escalacion
3. Boton de "Resolver" que llame al PATCH con el medio de resolucion
4. Actualizar en tiempo real con los socket events
```

---

## Prompt 7: Implementar nuevas funcionalidades

```
Hay funcionalidades nuevas que no existian en legacy. Implementar UI para:

1. MULTI-CANAL (WhatsApp + Email):
- Cada mensaje tiene campo "channel": "whatsapp" o "email"
- Mensajes email incluyen email_metadata: { subject, from_address, from_name, has_attachments }
- Mostrar indicador visual del canal en cada mensaje
- Para email: renderizar subject como cabecera del mensaje
- Selector de canal al enviar: GET /api/v1/conversations/{id}/channels?property_id=X

2. DELIVERY TRACKING:
- Socket event: message.delivery_updated
- Estados: pending → sent → delivered → read (o failed)
- Mostrar iconos de estado (1 tick, 2 ticks, 2 ticks azules, error)
- Actualizar en tiempo real

3. MEDIA ATTACHMENTS:
- Campo media[] en mensajes con: { id, media_type, mime_type, url, transcription, vision_description }
- image: mostrar preview + vision_description como tooltip
- audio: player + transcription debajo
- video: player inline
- document: link de descarga con filename

4. TRANSFERENCIA DE CONVERSACIONES:
- GET /api/v1/conversations/{id}/transfer-targets → lista de properties destino
- POST /api/v1/conversations/{id}/transfer → { destination_property_id, note }
- Boton "Transferir" que muestre selector de property destino

5. ASIGNACION MANUAL:
- POST /api/v1/conversations/{id}/assign → { property_id }
- Para conversaciones en inbox sin asignar (property_id=0)
- Boton "Asignar" que muestre selector de property

6. TOGGLE IA:
- PATCH /api/v1/conversations/{id}/ai?property_id=X&ai_enabled=true/false
- Boton on/off para activar/desactivar respuestas automaticas de IA
- Cuando se desactiva, las escalaciones pendientes se resuelven automaticamente
```

---

## Prompt 8: Verificacion final

```
Revisa que todos los puntos de la migracion estan cubiertos. Checklist:

RUTAS:
[ ] Todas las llamadas usan /api/v1/ como prefijo
[ ] No quedan referencias a /chats o chat_id como identificador REST
[ ] conversation_id (int) se usa en todas las llamadas REST

INBOX:
[ ] Listado usa GET /api/v1/conversations/?property_id=X
[ ] Busqueda usa GET /api/v1/conversations/search
[ ] unread_count se muestra correctamente
[ ] has_pending_escalation muestra indicador

MENSAJES:
[ ] Historial usa GET /api/v1/conversations/{id}/messages
[ ] direction + sender se renderizan correctamente
[ ] sender="ai" tiene etiqueta visual diferenciada
[ ] template_code muestra info del template cuando content es null
[ ] media[] se renderiza (imagenes, audio, video, docs)
[ ] email_metadata se renderiza para mensajes email

ENVIO:
[ ] POST /api/v1/chatter/send-message con nuevo schema
[ ] channel_endpoint_id se pasa si hay multiple canales

SOCKET.IO:
[ ] Conexion con auth: { token, property_id }
[ ] Auto-join property:{id}
[ ] join_chat/leave_chat con phone_code
[ ] message.created (renombrado de chat.message.created)
[ ] conversation.updated (renombrado de chat.updated)
[ ] conversation.created (nuevo)
[ ] message.delivery_updated (nuevo)
[ ] escalation.created (nuevo)
[ ] escalation.resolved (nuevo)

ESCALACIONES:
[ ] CRUD independiente con /api/v1/escalations
[ ] Resolver con PATCH /api/v1/escalations/{id}/resolve
[ ] Socket events actualizan UI en tiempo real

FUNCIONALIDADES NUEVAS:
[ ] Multi-canal (indicador whatsapp/email)
[ ] Delivery tracking (ticks de entrega)
[ ] Media attachments
[ ] Transferencia de conversaciones
[ ] Asignacion manual
[ ] Toggle IA

AUTENTICACION:
[ ] Bearer token en header Authorization
[ ] Mismo token en Socket.IO auth
```
