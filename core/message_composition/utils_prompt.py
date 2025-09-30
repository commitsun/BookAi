from pathlib import Path

def load_prompt(filename: str) -> str:
    """
    Carga un prompt desde la carpeta 'prompts'.
    Si hay caracteres inválidos, los reemplaza por '�'.
    """
    return (Path("prompts") / filename).read_text(
        encoding="utf-8",
        errors="replace"
    )

def sanitize_text(text: str) -> str:
    """
    Normaliza cualquier texto a UTF-8 seguro.
    """
    if text is None:
        return ""
    return str(text).encode("utf-8", errors="replace").decode("utf-8")
