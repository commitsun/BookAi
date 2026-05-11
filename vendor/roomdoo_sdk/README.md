# roomdoo_sdk (vendored)

Copia provisional del SDK Python para hablar con el PMS Roomdoo/Odoo vía JSON-RPC.

## Origen

- Repositorio fuente: `/home/dario/roomdoo-sdk` (local, sin remoto público todavía)
- Snapshot: commit `1133e66` — copiado el 2026-05-11
- Excluidos al copiar: `.git/`, `build/`, `*.egg-info/`, `__pycache__/`, `.pytest_cache/`

## Por qué está aquí

CLAUDE.md establece que el SDK debe vivir en un repo separado y BookAI debería
consumirlo como paquete instalable (`roomdoo-sdk`). Mientras ese repo no exista
como dependencia publicada/instalable, lo mantenemos vendorizado para que el
build de Docker sea autocontenido y dev no dependa de un volumen externo.

## Cómo se carga

El [Dockerfile](../../Dockerfile) hace `pip install --no-cache-dir -e ./vendor/roomdoo_sdk`
(editable). El bind mount `.:/app` del [docker-compose.yml](../../docker-compose.yml)
permite que los cambios al SDK desde el host se reflejen automáticamente con
`uvicorn --reload`.

## Cuándo quitar esto

Cuando el SDK se publique como paquete instalable (PyPI o índice privado):

1. Sustituir el `COPY` + `pip install -e` del Dockerfile por una dependencia normal en `pyproject.toml`.
2. Borrar `vendor/roomdoo_sdk/`.
3. Eliminar este README.
4. Actualizar el comentario de `pyproject.toml` que apunta a esta ubicación.

## No editar aquí en serio

Cualquier cambio funcional al SDK debe hacerse en su repo fuente para no
perderlo cuando se actualice este snapshot. Lo editable solo está pensado para
poder depurar/iterar rápido en dev.
