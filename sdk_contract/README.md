# SDK PMS/Roomdoo — Contrato esperado

## Propósito

Este directorio define la interfaz que BookAI espera de una librería Python externa
que abstraiga el acceso al PMS Roomdoo/Odoo. La implementación real de esa librería
vive en un repositorio separado.

BookAI dependerá de este SDK como una dependencia de Python instalable:

```toml
# pyproject.toml (futuro)
dependencies = [
    "roomdoo-sdk>=1.0.0",
]
```

## Responsabilidades del SDK

El SDK debe ser capaz de:

1. **Autenticar** contra una instancia Roomdoo dada una URL base y credenciales.
2. **Resolver propiedades/hoteles** a partir de un código externo o ID.
3. **Obtener credenciales de canal** (ej: token de WhatsApp, phone_id) asociadas a una propiedad.
4. **Obtener plantillas** configuradas para una propiedad y canal.
5. *(Fases futuras)* Obtener agentes disponibles, prompts, tools/skills y su configuración técnica.
6. *(Fases futuras)* Gestionar configuración de IA por instancia/hotel/conversación.

## Principio de abstracción

El SDK debe abstraer el transporte subyacente (HTTP REST a Odoo, webhooks, etc.).
BookAI solo conoce el contrato Python tipado; no conoce URLs, métodos HTTP ni estructuras
internas de Odoo.

## Interfaz pública esperada

Ver `interfaces.py` para los tipos y firmas exactas.

## Cómo BookAI dependerá del SDK

```python
# Ejemplo de uso en un servicio de BookAI (futuro)
from roomdoo_sdk import RoomdooClient, PropertyNotFound

client = RoomdooClient(instance_url="https://alda.host.roomdoo.com", token="...")
property = await client.get_property(external_code="alda_centro_ponferrada")
wa_credentials = await client.get_whatsapp_credentials(property_id=property.id)
```

BookAI inyectará el cliente como dependencia, permitiendo mockear el SDK en tests
sin necesidad de una instancia real de Roomdoo.

---

## Requisitos de seguridad para Fase 2

Los siguientes puntos de seguridad están **diferidos a Fase 2** y dependen directamente
de la disponibilidad del SDK para proporcionar las credenciales desde Odoo.

### B1 — Validación de firma `X-Hub-Signature-256` en el webhook de WhatsApp

Meta firma cada petición `POST /webhook/whatsapp` con HMAC-SHA256 usando el `app_secret`
de la Meta Business App. BookAI debe validar esta firma para garantizar que los mensajes
entrantes provienen realmente de Meta.

**Diseño previsto:**

- Añadir columna `app_secret VARCHAR(255)` a `channel_endpoints` (Alembic migration).
- El SDK proporcionará el `app_secret` al configurar el canal desde Odoo.
- El webhook leerá el body en crudo, extraerá el `phone_number_id` para identificar el
  `channel_endpoint`, y validará `X-Hub-Signature-256` usando ese `app_secret`.
- Si `app_secret` es `NULL` (canal aún no migrado), se permite el paso con warning en log.

```python
# Pseudocódigo del flujo de validación
raw_body = await request.body()          # Starlette cachea — safe to read before Pydantic
phone_number_id = _extract_phone_id(raw_body)
endpoint = await instance_repo.find_channel_endpoint_by_external_code(db, phone_number_id)
if endpoint.app_secret:
    expected = "sha256=" + hmac.new(
        endpoint.app_secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, request.headers.get("X-Hub-Signature-256", "")):
        raise HTTPException(status_code=403, detail="Invalid webhook signature")
```

### B2 — Gestión segura de `access_token` de Meta

El `access_token` de Meta (almacenado en `channel_endpoints.access_token`) da acceso
total para enviar mensajes en nombre del número de WhatsApp.

**Diseño previsto:**

- El SDK gestionará el ciclo de vida del token (obtención, rotación, revocación) desde Odoo.
- En Fase 2, BookAI recibirá el token del SDK en cada operación en lugar de leerlo de la DB,
  o bien el SDK lo cifrará en reposo antes de escribirlo (con `pgcrypto` o equivalent).
- El endpoint de configuración de instancia (que el SDK usará para escribir credenciales en
  BookAI) debe implementarse sobre HTTPS y requerir un token de administración separado del
  bearer token de operación.

**Medida provisional para Fase 1:** el `access_token` vive en texto plano en PostgreSQL,
protegido únicamente por las credenciales de acceso a la BBDD. Mitigar asegurando que
la BBDD no es accesible públicamente y que las credenciales de PostgreSQL están en `.env`
(nunca en el repositorio).
