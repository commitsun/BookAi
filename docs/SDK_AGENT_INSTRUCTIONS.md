# Instrucciones para agente Claude — BookAI Python SDK

> Este documento es la especificación completa para que un agente de Claude desarrolle
> el paquete Python `bookai-sdk`. Léelo íntegro antes de empezar a escribir código.

---

## Contexto

BookAI es un microservicio de mensajería conversacional para hoteles. Expone una API REST
que Roomdoo/Odoo llama para:

1. Iniciar conversaciones WhatsApp enviando plantillas a huéspedes.
2. Recibir respuestas de huéspedes en tiempo real.
3. Mantener sincronizados los datos de folios (reservas) en el caché de BookAI.

El SDK es el cliente Python oficial de esa API. Roomdoo (Odoo) lo importará como:

```python
from bookai_sdk import BookAIClient
```

---

## Repositorio destino

Crear un repositorio nuevo independiente de BookAI. El SDK **no** vive dentro del repo
de BookAI. Estructura sugerida:

```
bookai-sdk/
├── bookai_sdk/
│   ├── __init__.py
│   ├── client.py          # clase principal BookAIClient
│   ├── models.py          # dataclasses / Pydantic models de request/response
│   ├── exceptions.py      # excepciones tipadas
│   └── _http.py           # capa HTTP interna (httpx)
├── tests/
│   └── test_client.py
├── pyproject.toml
└── README.md
```

---

## Stack

| Capa         | Tecnología                              |
|--------------|-----------------------------------------|
| HTTP         | `httpx` (async) — versión >= 0.27       |
| Modelos      | `pydantic` v2                           |
| Python       | >= 3.11                                 |
| Tests        | `pytest` + `pytest-asyncio` + `respx`   |

No usar `requests` (no es async). No usar `aiohttp`.

---

## Interfaz pública del SDK

### Inicialización

```python
client = BookAIClient(
    base_url="https://bookai.myhotel.com",
    bearer_token="roomdoo-instance-token",
    timeout=10.0,          # opcional, segundos
)
```

El cliente debe ser un async context manager:

```python
async with BookAIClient(...) as client:
    result = await client.send_template(...)
```

Y también usable sin `async with` (el usuario cierra manualmente con `await client.close()`).

---

### Métodos del cliente

#### `send_template`

Inicia una conversación WhatsApp enviando una plantilla a un huésped.

```python
result: SendTemplateResult = await client.send_template(
    hotel_external_code="HOTEL_BCN_01",
    phone="+34699323583",
    template_code="welcome_checkin",
    language="es",
    components=[
        {"type": "body", "parameters": [{"type": "text", "text": "María"}]}
    ],
    # opcionales:
    folio_code="206/26/026072",
    folio_id=1042,
    checkin="2026-04-10",
    checkout="2026-04-14",
    guest_display_name="María García",
    country="ES",
    idempotency_key="roomdoo-send-1042-welcome_checkin",
)
```

Mapeo al endpoint: `POST /api/v1/whatsapp/send-template`

Request body que construye internamente:

```json
{
  "source": {
    "hotel": { "external_code": "HOTEL_BCN_01" },
    "origin_folio": {
      "code": "206/26/026072",
      "id": 1042,
      "checkin": "2026-04-10",
      "checkout": "2026-04-14"
    }
  },
  "recipient": {
    "phone": "+34699323583",
    "country": "ES",
    "display_name": "María García"
  },
  "template": {
    "code": "welcome_checkin",
    "language": "es",
    "components": [...]
  },
  "idempotency_key": "roomdoo-send-1042-welcome_checkin"
}
```

Resultado:

```python
@dataclass
class SendTemplateResult:
    status: str           # "ok"
    message_id: int
    wa_message_id: str | None
    conversation_id: int
    idempotent: bool      # True si la clave de idempotencia ya existía
```

---

#### `update_folio`

Actualiza el caché de un folio en BookAI cuando cambian datos en Odoo.

```python
result: FolioResult = await client.update_folio(
    odoo_external_code="206/26/026072",
    status="onboard",         # opcional
    checkin_date="2026-04-10", # opcional
    checkout_date="2026-04-14",# opcional
    pending_payment_amount=150.00,     # opcional
    pending_payment_currency="EUR",    # opcional
)
```

Mapeo al endpoint: `PATCH /api/v1/folios/{odoo_external_code}`

Solo se envían los campos que el usuario pasa explícitamente (no mandar `null` para campos no especificados — usar `exclude_unset=True` o equivalente).

Resultado:

```python
@dataclass
class FolioResult:
    odoo_external_code: str
    status: str | None
    checkin_date: str | None
    checkout_date: str | None
    pending_payment_amount: float | None
    pending_payment_currency: str | None
    synced_at: str | None
```

---

#### `list_conversations`

Obtiene la bandeja de conversaciones de una property.

```python
result: ConversationsResult = await client.list_conversations(
    property_id=1,
    limit=50,        # opcional, default 50
)
```

Mapeo: `GET /api/v1/conversations/?property_id=1&limit=50`

Pasar `property_id=0` para obtener conversaciones sin asignar.

---

#### `search_conversations`

Busca conversaciones por nombre de huésped, código de folio o estado.

```python
result: ConversationsResult = await client.search_conversations(
    property_id=1,
    q="García",        # opcional
    status="onboard",  # opcional; valores: draft/confirm/onboard/done/cancel
    limit=50,
)
```

Mapeo: `GET /api/v1/conversations/search`

Al menos uno de `q` o `status` debe estar presente; el SDK debe validarlo antes de hacer la llamada y lanzar `ValueError` si ambos son `None`.

---

#### `get_messages`

Obtiene el historial de mensajes de una conversación.

```python
result: MessagesResult = await client.get_messages(
    conversation_id=42,
    language="es",       # opcional
    limit=50,
    before_id=1200,      # opcional, para paginación
)
```

Mapeo: `GET /api/v1/conversations/{conversation_id}/messages`

---

#### `mark_read`

Marca una conversación como leída para una property.

```python
await client.mark_read(conversation_id=42, property_id=1)
```

Mapeo: `PATCH /api/v1/conversations/{conversation_id}/read?property_id=1`

Devuelve `None` (204 No Content en la API).

---

#### `send_message`

Envía un mensaje de texto libre al huésped (requiere ventana de 24h abierta).

```python
result: SendMessageResult = await client.send_message(
    conversation_id=42,
    content="Su habitación está lista.",
    agent_user_id=7,                # opcional
    agent_display_name="Carlos",    # opcional
    channel_endpoint_id=None,       # opcional
)
```

Mapeo: `POST /api/v1/chatter/send-message`

---

## Gestión de errores

Definir una jerarquía de excepciones propia:

```python
class BookAIError(Exception):
    """Base para todas las excepciones del SDK."""

class BookAIHTTPError(BookAIError):
    """Respuesta HTTP con status >= 400."""
    status_code: int
    detail: str | None

class BookAINotFoundError(BookAIHTTPError):     # 404
class BookAIUnauthorizedError(BookAIHTTPError): # 401
class BookAIWindowClosedError(BookAIHTTPError): # 422 (ventana de mensajería)
class BookAIServerError(BookAIHTTPError):       # 5xx
```

El código HTTP debe mapearse automáticamente a la subclase correcta.
Exponer `status_code` y `detail` (del body JSON `{"detail": "..."}` de FastAPI).

---

## Autenticación

El bearer token se pasa en todas las requests como:

```
Authorization: Bearer <token>
```

El cliente lo inyecta automáticamente; el usuario no lo gestiona por request.

---

## Retry y timeout

- Timeout configurable en la inicialización (default: 10 segundos).
- NO implementar retry automático en el SDK v1 — dejar esa responsabilidad a la capa de Roomdoo/Celery que llame al SDK. La idempotency_key cubre los reintentos de send_template.

---

## Tests

Usar `respx` para mockear las llamadas HTTP de `httpx` en los tests. No levantar un servidor real.

Tests mínimos a implementar:

| Test                                  | Qué verifica                                                |
|---------------------------------------|-------------------------------------------------------------|
| `test_send_template_ok`               | Construye body correcto, parsea response                    |
| `test_send_template_idempotent`       | `idempotent=True` en response → propagado al resultado      |
| `test_send_template_404`              | `BookAINotFoundError` lanzado                               |
| `test_send_template_422`              | `BookAIHTTPError` con status 422                            |
| `test_update_folio_partial`           | Solo los campos enviados están en el body (sin nulls extra) |
| `test_update_folio_not_found`         | `BookAINotFoundError`                                       |
| `test_mark_read_ok`                   | 204 → devuelve None sin error                               |
| `test_search_requires_q_or_status`    | `ValueError` si ambos son None                              |
| `test_context_manager`                | `async with` abre y cierra el cliente correctamente         |

---

## Packaging

`pyproject.toml` mínimo:

```toml
[project]
name = "bookai-sdk"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "httpx>=0.27.0",
    "pydantic>=2.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "respx>=0.21",
]
```

---

## Consideraciones de diseño

1. **Async only en v1.** No implementar una versión síncrona por ahora.
2. **Modelos inmutables.** Usar `@dataclass(frozen=True)` o `pydantic.BaseModel` con `model_config = ConfigDict(frozen=True)` para los resultados.
3. **No acoplar al modelo interno de BookAI.** El SDK expone su propia interfaz; si BookAI cambia un nombre de campo interno, solo cambia el SDK, no Roomdoo.
4. **Loggear en el nivel correcto.** Usar `logging.getLogger("bookai_sdk")`. No usar `print`.
5. **Documentar todos los métodos públicos** con docstrings que incluyan los posibles errores.

---

## Lo que NO está en scope del SDK v1

- Webhook handler (recibir eventos de WhatsApp) — eso es responsabilidad del servidor.
- Socket.IO (tiempo real) — queda para una futura versión del SDK.
- Gestión de plantillas (crear/listar en Meta) — fuera de scope.
- Rate limiting propio — gestionado por la capa de llamada.
