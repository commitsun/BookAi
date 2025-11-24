import logging
from pathlib import Path
from typing import Dict, Tuple, Optional

log = logging.getLogger("PromptLoader")
uvicorn_log = logging.getLogger("uvicorn.error")

# Cache de prompts por filename → (mtime, contenido)
_PROMPT_CACHE: Dict[str, Tuple[float, str]] = {}

def load_prompt(filename: str) -> str:
    """
    Carga un prompt desde la carpeta 'prompts' y devuelve el contenido.
    Se recarga automáticamente si el archivo cambia (mtime distinto).
    Si hay caracteres inválidos, los reemplaza por '�'.
    """
    path = Path("prompts") / filename
    try:
        current_mtime = path.stat().st_mtime
    except FileNotFoundError:
        log.error("❌ Prompt no encontrado: %s", filename)
        raise

    cached: Optional[Tuple[float, str]] = _PROMPT_CACHE.get(filename)
    if cached and cached[0] == current_mtime:
        return cached[1]

    content = path.read_text(encoding="utf-8", errors="replace")
    _PROMPT_CACHE[filename] = (current_mtime, content)

    message = f"📜 Prompt cargado/refrescado: {filename} ({len(content)} chars)"
    log.info(message)
    try:
        uvicorn_log.info(message)
    except Exception:
        pass

    return content

def sanitize_text(text: str) -> str:
    """
    Normaliza cualquier texto a UTF-8 seguro.
    """
    if text is None:
        return ""
    return str(text).encode("utf-8", errors="replace").decode("utf-8")
