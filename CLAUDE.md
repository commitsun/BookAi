# CLAUDE.md — BookAI

Contexto y restricciones para el asistente durante el desarrollo de este proyecto.

---

## Idioma

- Comunicación con el usuario: **español**
- Código, nombres de clases, funciones, módulos, variables y tests: **inglés**

---

## Qué es BookAI

Microservicio Python que actúa como backend de canal conversacional entre:
- Roomdoo/Odoo del cliente (PMS hotelero)
- WhatsApp (Meta API)
- App del cliente en tiempo real vía WebSocket

---

## Arquitectura — decisiones no negociables

1. El código anterior al refactoring vive en `legacy/` solo como referencia. No se ejecuta.
2. La nueva estructura vive en `app/`. Son proyectos paralelos; no hay imports cruzados entre ellos.
3. No hay migración de datos entre la base de datos antigua y la nueva. Se parte de tablas vacías.
4. La lógica de negocio no depende de LangChain ni de prompts.
5. Las integraciones externas (WhatsApp, Roomdoo) quedan detrás de adaptadores claros.
6. Persistencia principal: PostgreSQL vía SQLAlchemy 2.x async + Alembic.
7. No usar memoria de proceso como fuente de verdad para conversaciones, mensajes o estados críticos.

---

## Restricciones adicionales para la fase 1

- Bookai no debe comportarse en esta fase como sistema de agentes.
- Bookai debe comportarse como backend de mensajería transaccional y tiempo real.
- Cualquier referencia a agentes, skills, prompts o LLM debe considerarse fuera de alcance salvo que sea estrictamente necesaria para no romper compatibilidad temporal.
- No introducir RabbitMQ ni otros brokers externos en esta fase.
- No usar memoria de proceso como fuente de verdad para conversaciones, mensajes o estados críticos.
- La integración con el PMS/Roomdoo debe prepararse conceptualmente como una futura librería SDK Python importable, no como MCP.
- En este repositorio no debe implementarse el SDK del PMS/Roomdoo.
- En este repositorio solo debe definirse:
  - la necesidad del SDK
  - su interfaz esperada
  - su estructura conceptual
  - cómo Bookai debería integrarse con él
- La implementación real del SDK se hará en otro proyecto con contexto completo del PMS de Odoo.
- El diseño debe priorizar trazabilidad, idempotencia, persistencia y separación de integraciones.

---

## Stack — Fase 1

| Capa | Tecnología |
|---|---|
| Framework | `fastapi` + `uvicorn` |
| ORM | `SQLAlchemy 2.x` async + `asyncpg` |
| Migraciones | `Alembic` |
| Validación | `pydantic v2` + `pydantic-settings` |
| Real-time | `python-socketio` |
| HTTP client | `httpx` (async) |
| Phone | `phonenumbers` |
| Testing | `pytest` + `pytest-asyncio` |

**Excluidos explícitamente:** `langchain`, `langgraph`, `openai`, `fastmcp`, `boto3`, `python-docx`, `nest_asyncio`, `deep-translator`, `langdetect`, RabbitMQ.

> Nota sobre `mcp`: la librería `mcp` (Model Context Protocol SDK) **sí** se usa, pero exclusivamente para conectar con servidores MCP **externos** a Odoo. La integración con Roomdoo/PMS va por el SDK Python (`vendor/roomdoo_sdk/` provisionalmente), no por MCP — eso es lo que prohíbe la restricción de la sección anterior.

---

## Alcance de la Fase 1

### Incluido
1. Roomdoo envía orden → BookAI envía plantilla WhatsApp → crea/localiza conversación → registra mensaje
2. Huésped responde en WhatsApp → BookAI persiste mensaje → notifica app vía WebSocket
3. Usuario interno responde desde la app → BookAI reenvía a WhatsApp → persiste con trazabilidad → notifica vía WebSocket

### Excluido
- Agentes, LLM, RAG, tools dinámicas, skills de Roomdoo
- Implementación del SDK PMS/Roomdoo
- Transcripción de audio (mensajes de audio se ignoran o registran como placeholder)

---

## SDK PMS/Roomdoo

- Contrato definido en `sdk_contract/` (interfaz, estructura, integración esperada)
- La implementación real vive en otro repositorio
- BookAI dependerá del SDK como paquete Python instalable: `roomdoo-sdk`

---

## Disciplina de trabajo

- No hacer reescrituras masivas. Cambios pequeños, verticales y verificables.
- No cambiar contratos públicos sin documentarlos primero.
- Separar siempre hechos observados de recomendaciones.
- Antes de tocar comportamiento: identificar entrypoints, consumidores y side effects.
- No meter lógica de negocio en rutas, webhooks o managers de socket.
- No inventar capas vacías que no vayan a usarse pronto.

## Formato de respuesta

1. Qué observas
2. Qué problema real hay
3. Qué propones
4. Qué archivos tocarías
5. Riesgos
6. Cambio mínimo recomendado
7. Cómo validarlo
