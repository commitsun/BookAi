# Instrucciones para agente Claude — Módulo Odoo BookAI

> Este documento es la especificación completa para que un agente de Claude desarrolle
> el módulo Odoo `bookai_connector`. Léelo íntegro antes de escribir código.

---

## Contexto

BookAI es un microservicio Python de mensajería conversacional para hoteles.
Roomdoo es una distribución de Odoo (ERP hotelero). El módulo que hay que construir
conecta Roomdoo con BookAI: cuando pasan cosas en Odoo (nueva reserva, cambio de estado,
check-in, check-out), Roomdoo llama a la API de BookAI.

El módulo es **Fase 1**: integración básica con la API actual.
En fases futuras se añadirá configuración de agentes IA desde Odoo, pero eso NO está
en scope aquí.

---

## Stack y versión de Odoo

| Elemento      | Versión / tech         |
|---------------|------------------------|
| Odoo          | 17.0 (Community o Enterprise) |
| Python        | 3.11+                  |
| HTTP client   | `requests` (ya disponible en Odoo) |
| Tests         | `unittest` estándar de Odoo |

El módulo debe ser compatible con Odoo 17.0. No usar funcionalidades exclusivas de versiones anteriores o posteriores.

---

## Nombre del módulo

`bookai_connector`

Ruta: `<addons_path>/bookai_connector/`

---

## Estructura de archivos

```
bookai_connector/
├── __manifest__.py
├── __init__.py
├── models/
│   ├── __init__.py
│   ├── res_config_settings.py   # configuración del módulo
│   └── bookai_mixin.py          # mixin reutilizable para llamadas a BookAI
├── views/
│   ├── res_config_settings_views.xml
│   └── bookai_log_views.xml     # vista de logs de llamadas
├── data/
│   └── ir_config_parameter_data.xml  # parámetros de sistema vacíos
├── security/
│   └── ir_model_access.csv
└── tests/
    └── test_bookai_connector.py
```

---

## `__manifest__.py`

```python
{
    "name": "BookAI Connector",
    "version": "17.0.1.0.0",
    "summary": "Integración de Roomdoo con BookAI (mensajería WhatsApp para huéspedes)",
    "author": "Roomdoo",
    "category": "Hotel",
    "depends": ["base", "hotel"],   # ajustar al nombre exacto del módulo hotel de Roomdoo
    "data": [
        "security/ir_model_access.csv",
        "data/ir_config_parameter_data.xml",
        "views/res_config_settings_views.xml",
        "views/bookai_log_views.xml",
    ],
    "installable": True,
    "auto_install": False,
    "license": "LGPL-3",
}
```

---

## Configuración (`res_config_settings.py`)

Añadir tres campos a `res.config.settings` y guardarlos como `ir.config.parameter`:

| Campo en settings          | Parámetro en ir.config.parameter  | Descripción                          |
|----------------------------|-------------------------------------|--------------------------------------|
| `bookai_url`               | `bookai_connector.url`              | URL base de BookAI (sin trailing /)  |
| `bookai_token`             | `bookai_connector.token`            | Bearer token de la instancia         |
| `bookai_enabled`           | `bookai_connector.enabled`          | Boolean, activa/desactiva el módulo  |

```python
class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    bookai_url = fields.Char(
        string="BookAI URL",
        config_parameter="bookai_connector.url",
        help="Base URL del servidor BookAI (ej. https://bookai.mihotel.com)",
    )
    bookai_token = fields.Char(
        string="BookAI Bearer Token",
        config_parameter="bookai_connector.token",
    )
    bookai_enabled = fields.Boolean(
        string="Activar integración BookAI",
        config_parameter="bookai_connector.enabled",
        default=False,
    )
```

---

## Mixin (`bookai_mixin.py`)

Clase reutilizable que encapsula todas las llamadas HTTP a BookAI.
Todos los modelos que necesiten llamar a BookAI heredarán de este mixin.

```python
class BookAIMixin(models.AbstractModel):
    _name = "bookai.mixin"
    _description = "BookAI API mixin"

    def _bookai_enabled(self) -> bool:
        """Returns True if BookAI integration is enabled and configured."""

    def _bookai_post(self, path: str, payload: dict) -> dict:
        """POST to BookAI API. Raises BookAIError on failure."""

    def _bookai_patch(self, path: str, payload: dict) -> dict:
        """PATCH to BookAI API. Raises BookAIError on failure."""
```

Comportamiento de `_bookai_post` / `_bookai_patch`:
- Lee URL y token de `ir.config.parameter`.
- Si `bookai_enabled=False`, hace log a nivel DEBUG y retorna `{}` sin llamar.
- Lanza `UserError` (o excepción personalizada) si falla con status >= 400.
- Registra cada llamada en el modelo de log (ver abajo).
- Timeout: 15 segundos.

---

## Modelo de log (`bookai.api.log`)

Para auditoría y debugging. Cada llamada HTTP queda registrada.

```python
class BookAIApiLog(models.Model):
    _name = "bookai.api.log"
    _description = "BookAI API Call Log"
    _order = "create_date desc"

    method = fields.Char(readonly=True)          # POST, PATCH
    endpoint = fields.Char(readonly=True)        # /api/v1/whatsapp/send-template
    request_payload = fields.Text(readonly=True) # JSON serializado
    response_status = fields.Integer(readonly=True)
    response_body = fields.Text(readonly=True)
    success = fields.Boolean(readonly=True)
    error_message = fields.Text(readonly=True)
    duration_ms = fields.Integer(readonly=True)
    create_date = fields.Datetime(readonly=True)
```

Vista de lista mínima: method, endpoint, response_status, success, duration_ms, create_date.
Vista de formulario: todos los campos.

Añadir una acción de menú en Ajustes → Técnico → BookAI → Logs.

---

## Triggers de integración

Estos son los puntos de Odoo donde el módulo debe actuar. Implementar usando `_inherit` sobre los modelos existentes de Roomdoo.

### Trigger 1: Envío de plantilla al crear/confirmar reserva

**Cuándo:** al cambiar `hotel.folio` (o equivalente en Roomdoo) a estado `confirm`.

**Qué llama:**

```
POST /api/v1/whatsapp/send-template
```

Body:

```python
{
    "source": {
        "hotel": {"external_code": folio.hotel_id.bookai_external_code},
        "origin_folio": {
            "code": folio.name,          # código del folio en Odoo
            "id": folio.id,
            "checkin": str(folio.checkin_date),
            "checkout": str(folio.checkout_date),
        }
    },
    "recipient": {
        "phone": folio.partner_id.mobile or folio.partner_id.phone,
        "country": folio.partner_id.country_id.code,
        "display_name": folio.partner_id.name,
    },
    "template": {
        "code": "welcome_checkin",       # configurable por hotel en el futuro
        "language": folio.partner_id.lang or "es",
        "components": [],
    },
    "idempotency_key": f"folio-confirm-{folio.id}-{folio.name}",
}
```

**Campo necesario en `hotel.hotel` (o el modelo de property de Roomdoo):**

```python
bookai_external_code = fields.Char(
    string="BookAI External Code",
    help="roomdoo_external_code configurado en BookAI para esta property",
)
```

---

### Trigger 2: Actualización de caché de folio

**Cuándo:** al cambiar el estado de `hotel.folio` a cualquier valor, o al cambiar `checkin_date`, `checkout_date`, o cualquier campo de pago pendiente.

**Qué llama:**

```
PATCH /api/v1/folios/{folio.name}
```

Body (solo los campos que cambiaron — usar `_changed_fields`):

```python
# Mapeo de campos Odoo → BookAI
FOLIO_STATUS_MAP = {
    "draft": "draft",
    "confirm": "confirm",
    "open": "onboard",    # ajustar según el código de estado real de Roomdoo
    "done": "done",
    "cancel": "cancel",
}

payload = {}
if "state" in changed_fields:
    payload["status"] = FOLIO_STATUS_MAP.get(folio.state)
if "checkin_date" in changed_fields:
    payload["checkin_date"] = str(folio.checkin_date)
if "checkout_date" in changed_fields:
    payload["checkout_date"] = str(folio.checkout_date)
# ... pending_payment si existe en el modelo
```

---

### Trigger 3: Check-in (estado `onboard`)

Igual que Trigger 2, pero siempre incluye `status: "onboard"` aunque sea repetido.
Puede haber lógica adicional en el futuro (ej. enviar plantilla de bienvenida de check-in).

---

## Implementación técnica de los triggers

Usar `write()` override o `_onchange` según el patrón de Roomdoo.
Preferir `write()` override para garantizar persistencia:

```python
class HotelFolio(models.Model):
    _inherit = "hotel.folio"

    def write(self, vals):
        result = super().write(vals)
        if self._bookai_enabled():
            if "state" in vals or "checkin_date" in vals or "checkout_date" in vals:
                for folio in self:
                    folio._sync_to_bookai(vals)
        return result

    def _sync_to_bookai(self, changed_vals):
        """Build and send the folio update payload to BookAI."""
        ...
```

---

## Manejo de errores

- Si la llamada a BookAI falla (timeout, 5xx, red), **no interrumpir la operación de Odoo**. Registrar el error en `bookai.api.log` y loggear con `_logger.error(...)`.
- Si la llamada falla con 404 (folio no existe en BookAI) en el trigger de actualización, intentar una vez el send-template para crearlo, luego reintentar el PATCH.
- En ningún caso propagar excepciones al usuario final a menos que sea una mala configuración (URL o token vacíos).

---

## Tests

Usar `unittest.mock.patch` para mockear las llamadas HTTP. No hacer llamadas reales a BookAI.

Tests mínimos:

| Test                                        | Qué verifica                                                          |
|---------------------------------------------|-----------------------------------------------------------------------|
| `test_send_template_on_confirm`             | Al confirmar folio → se llama `_bookai_post` con el payload correcto  |
| `test_update_folio_on_state_change`         | Al cambiar estado → PATCH con solo los campos modificados             |
| `test_no_call_when_disabled`               | Con `bookai_enabled=False` → no se hace ninguna llamada HTTP          |
| `test_error_does_not_rollback_folio`        | Si BookAI falla → el `write()` del folio se completa igualmente       |
| `test_log_created_on_success`               | Llamada exitosa → 1 registro en `bookai.api.log` con `success=True`   |
| `test_log_created_on_error`                 | Llamada fallida → registro con `success=False` y `error_message`      |
| `test_idempotency_key_format`               | La clave de idempotencia sigue el patrón `folio-confirm-{id}-{name}`  |

---

## Lo que NO está en scope (Fase 1)

- Configuración de agentes IA por property desde Odoo.
- Plantillas configurables por hotel desde la interfaz (el código de plantilla está hardcodeado).
- Recepción de webhooks desde BookAI hacia Odoo (dirección inversa).
- Sincronización de mensajes del chat en el folio de Odoo.
- Multi-idioma para plantillas seleccionado dinámicamente por huésped.
- Integración con `mail.thread` de Odoo.

Estas funcionalidades están previstas para Fase 2 y Fase 3 respectivamente.

---

## Preguntas que debes responder antes de codificar

Antes de empezar, identifica en el código de Roomdoo existente:

1. ¿Cuál es el nombre exacto del modelo de folio/reserva? (¿`hotel.folio`, `pms.folio`, otro?)
2. ¿Cuál es el nombre exacto del modelo de property/hotel? (¿`hotel.hotel`, `pms.property`, otro?)
3. ¿Cuáles son los valores exactos del campo `state` del folio?
4. ¿Tiene el partner (`res.partner`) campo `mobile` y `phone` por separado?
5. ¿Existe ya algún campo de código externo en el modelo de hotel/property?

Usa las respuestas para ajustar los nombres de campos y modelos antes de escribir código.
Si no tienes acceso al código de Roomdoo, implementa con los nombres estándar de Odoo
(`hotel.folio`, `hotel.hotel`) y añade una nota en el README indicando qué hay que ajustar.
