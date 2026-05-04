# Plan de pruebas exhaustivo pre-producción — 65 tests

## A. Aislamiento multi-instancia (6)
- A1: Token instancia A → GET conversations property de B → 404
- A2: Token instancia A → GET messages conversación de B → 404
- A3: Token instancia A → PATCH read property de B → 404
- A4: Token instancia A → GET escalations property de B → 404
- A5: Token instancia A → POST send-template property de B → 404
- A6: Token inválido → 401

## B. Ruteo de mensajes por canal (5)
- B1: Mensaje al phone_number_id_A → property A
- B2: Mensaje al phone_number_id_B → property B
- B3: phone_number_id inexistente → ignora
- B4: Mismo contacto, 2 properties → sesiones separadas
- B5: Property sin canal → no recibe

## C. Templates y folios (7)
- C1: Template con folio → contacto + conversación + sesión + folio
- C2: 2ª template mismo contacto, otro folio → misma conv, 2 folios
- C3: Idempotency key → idempotent=true
- C4: Template a property sin canal → 422
- C5: odoo_id inexistente → 404
- C6: Language fallback en→en_US
- C7: Folio sin fechas → null dates

## D. Conversación IA básica (6)
- D1: Mensaje → supervisor delega → worker responde
- D2: Folio context en prompt
- D3: Property context (IDs, pricelists, room_types)
- D4: Guest context (nombre, teléfono)
- D5: Fecha actual en prompt
- D6: Sale_channel_id en property context

## E. Conversación multi-turno (5)
- E1: 5 turnos coherentes
- E2: Worker context entre agentes
- E3: Historial 20+ mensajes
- E4: Cambio de tema → reasignación
- E5: Mensaje corto ("ok") → no pierde contexto

## F. Seguridad de datos (7)
- F1: Busca folios otro nombre → phone forzado
- F2: Busca folios teléfono ajeno → phone forzado
- F3: God_mode tool → bloqueado para external
- F4: Advisor → sin tools escritura
- F5: Worker fuera de allowed_agents → no delegado
- F6: God_mode no en workers external
- F7: Internal → acceso completo

## G. Confirmation policy (5)
- G1: sensitive×sensitive → confirma
- G2: sensitive×none → ejecuta
- G3: irreversible×irreversible → confirma
- G4: Pending invalidado al crear nuevo
- G5: Matriz completa 12 combinaciones

## H. Operador (5)
- H1: Enviar dentro ventana → 200
- H2: Enviar sin inbound → 422
- H3: IA off → no responde
- H4: IA on → responde
- H5: Mark read → unread=0

## I. Escalaciones (5)
- I1: "hablar con persona" → escalación
- I2: "besoin humain" (francés) → escalación
- I3: Resolver → IA restaurada
- I4: manual_takeover → IA no restaurada
- I5: No duplica escalación

## J. Dedup y buffer (4)
- J1: Mismo wa_message_id → 1 mensaje
- J2: 3 mensajes rápidos → buffer
- J3: Mismo texto, distinto ID → 2 mensajes
- J4: Status update → delivery_status

## K. Execution modes (5)
- K1: Execution created → running
- K2: Completed tras éxito
- K3: Error tras escalación
- K4: Effective role resolution
- K5: Log level filtering

## L. Edge cases (5)
- L1: Mensaje vacío
- L2: Mensaje 5000 chars
- L3: Emoji/unicode/RTL
- L4: Property disabled
- L5: Sin sesión activa → auto-crea
