# Nuevos endpoints de escalacion — Guia para frontend

## Auth

Todos los endpoints requieren `Authorization: Bearer <token>` (igual que el resto de la API).

---

## 1. Chat con IA durante una escalacion

El operador envia una instruccion y la IA genera un borrador de respuesta para el huesped.

```
POST /api/v1/escalations/{escalation_id}/chat
```

**Request body:**

```json
{
  "instruction": "Dile que mañana a las 10 le arreglamos el aire acondicionado",
  "agent_user_id": 5,
  "agent_display_name": "María García"
}
```

| Campo | Tipo | Requerido | Descripcion |
|-------|------|-----------|-------------|
| `instruction` | string | Si | Lo que el operador quiere que la IA transmita al huesped |
| `agent_user_id` | int \| null | No | Odoo user ID del operador (trazabilidad) |
| `agent_display_name` | string \| null | No | Nombre visible del operador |

**Response 200:**

```json
{
  "escalation_id": 42,
  "draft_response": "Hemos coordinado la reparación de su aire acondicionado para mañana a las 10:00. Disculpe las molestias.",
  "messages": [
    {
      "id": 101,
      "sender": "agent",
      "content": "Dile que mañana a las 10...",
      "created_at": "2026-05-01T14:30:00+00:00"
    },
    {
      "id": 102,
      "sender": "ai",
      "content": "Hemos coordinado la reparación...",
      "created_at": "2026-05-01T14:30:02+00:00"
    }
  ]
}
```

`messages` incluye todo el hilo de la escalacion (instrucciones previas + respuestas AI), ordenado cronologicamente.

---

## 2. Refinar borrador

El operador pide ajustar el borrador actual sin generar un hilo nuevo.

```
POST /api/v1/escalations/{escalation_id}/refine-draft
```

**Request body:**

```json
{
  "instruction": "Hazlo más corto y en inglés",
  "current_draft": "Hemos coordinado la reparación..."
}
```

| Campo | Tipo | Requerido | Descripcion |
|-------|------|-----------|-------------|
| `instruction` | string | Si | Que ajustar |
| `current_draft` | string \| null | No | Borrador base. Si no se envia, usa el ultimo `draft_response` guardado |

**Response 200:**

```json
{
  "escalation_id": 42,
  "draft_response": "We've scheduled the AC repair for tomorrow at 10 AM. Sorry for the inconvenience."
}
```

---

## 3. Evento Socket.IO en tiempo real

Cada vez que se genera o refina un borrador, se emite al room `property:{property_id}`:

**Evento:** `escalation.draft_updated`

```json
{
  "escalation_id": 42,
  "conversation_id": 15,
  "draft_response": "We've scheduled the AC repair..."
}
```

Sirve para que otros operadores viendo la misma escalacion reciban el borrador actualizado sin hacer polling.

---

## Errores posibles

| HTTP | Cuando |
|------|--------|
| 404 | `escalation_id` no existe |
| 409 | La escalacion ya esta resuelta o cancelada |
| 502 | Fallo en la generacion AI (timeout, error del LLM) |
| 503 | La instancia no tiene IA configurada (sin supervisor o sin credenciales LLM) |

---

## Flujo tipico

1. Se recibe `escalation.created` por socket → mostrar escalacion pendiente
2. Operador abre la escalacion → `GET /api/v1/conversations/{id}/escalations` (trae el hilo con mensajes)
3. Operador escribe instruccion → `POST /escalations/{id}/chat` → mostrar `draft_response`
4. Operador quiere ajustar → `POST /escalations/{id}/refine-draft` → actualizar `draft_response`
5. Operador satisfecho → envia el borrador al huesped con el endpoint existente de chatter (`POST /api/v1/chatter/send-message`)
6. Operador resuelve → `PATCH /escalations/{id}/resolve`
