import json
import os
import time
from typing import Dict, Tuple

from dotenv import load_dotenv

from pipeline import run_pipeline
from pipeline.s3_client import S3_BUCKET, get_s3_client

# Carga variables de entorno para que el watcher sea configurable.
load_dotenv()

# Configuración ajustable por entorno.
S3_WATCH_PREFIX = os.getenv("S3_WATCH_PREFIX", "")
S3_POLL_INTERVAL = int(os.getenv("S3_POLL_INTERVAL_SECONDS", "60"))
S3_STATE_FILE = os.getenv(
    "S3_STATE_FILE",
    os.path.join(os.path.dirname(__file__), ".s3_state.json"),
)

s3 = get_s3_client()


def _load_previous_state() -> Dict[str, dict]:
    """Carga la instantánea previa del bucket desde disco."""
    if not os.path.exists(S3_STATE_FILE):
        return {}
    try:
        with open(S3_STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        # Si el estado estuviera corrupto, empezamos de cero.
        return {}


def _save_state(state: Dict[str, dict]) -> None:
    """Guarda la instantánea actual del bucket para futuras comparaciones."""
    state_dir = os.path.dirname(S3_STATE_FILE)
    if state_dir:
        os.makedirs(state_dir, exist_ok=True)
    with open(S3_STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def _fetch_s3_state(prefix: str = "") -> Dict[str, dict]:
    """
    Devuelve un dict con la info básica de cada archivo en S3.
    Usamos etag y last_modified para detectar cambios.
    """
    state: Dict[str, dict] = {}
    paginator = s3.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            state[key] = {
                "etag": obj.get("ETag", "").strip('"'),
                "last_modified": obj.get("LastModified").isoformat()
                if obj.get("LastModified")
                else "",
                "size": obj.get("Size"),
            }

    return state


def _diff_states(
    previous: Dict[str, dict], current: Dict[str, dict]
) -> Tuple[list, list, list]:
    """Devuelve listas de añadidos, modificados y eliminados."""
    added = [k for k in current.keys() if k not in previous]
    removed = [k for k in previous.keys() if k not in current]

    modified = []
    for key, meta in current.items():
        if key in previous and meta.get("etag") != previous[key].get("etag"):
            modified.append(key)

    return added, modified, removed


def watch_for_changes() -> None:
    """
    Bucle infinito que detecta cambios en S3 y lanza la pipeline.
    Usa un snapshot local para evitar relanzar sin nuevos cambios.
    """
    print(f"👀 Vigilando bucket '{S3_BUCKET}' (prefijo: '{S3_WATCH_PREFIX}')...")
    print(f"⏱️ Intervalo de sondeo: {S3_POLL_INTERVAL}s\n")

    previous_state = _load_previous_state()

    while True:
        try:
            current_state = _fetch_s3_state(prefix=S3_WATCH_PREFIX)
            added, modified, removed = _diff_states(previous_state, current_state)

            if added or modified or removed:
                print("📌 Cambios detectados en S3:")
                if added:
                    print(f"   ➕ Nuevos: {', '.join(added)}")
                if modified:
                    print(f"   ✏️  Modificados: {', '.join(modified)}")
                if removed:
                    print(f"   ➖ Eliminados: {', '.join(removed)}")

                # Lanzamos la pipeline principal.
                run_pipeline.main()

                # Guardamos snapshot solo si se procesó la pipeline.
                _save_state(current_state)
                previous_state = current_state
            else:
                print("✅ Sin cambios en S3.")

            time.sleep(S3_POLL_INTERVAL)
        except KeyboardInterrupt:
            print("\n🛑 Watcher detenido por el usuario.")
            break
        except Exception as e:
            print(f"⚠️ Error vigilando S3: {e}")
            # Evita bucle ajustando espera en caso de error transitorio.
            time.sleep(max(10, S3_POLL_INTERVAL // 2))


if __name__ == "__main__":
    watch_for_changes()
