from pathlib import Path

def load_prompt(filename: str) -> str:
    """
    Carga un prompt desde la carpeta 'prompts' con manejo seguro de encoding.
    Si encuentra caracteres inválidos, los reemplaza por '�' en lugar de fallar.
    """
    return (Path("prompts") / filename).read_text(
        encoding="utf-8",
        errors="replace"
    )

def sanitize_text(text: str) -> str:
    """
    Normaliza cualquier texto a UTF-8 seguro.
    Reemplaza caracteres inválidos (surrogates) en lugar de romper.
    """
    if text is None:
        return ""
    return str(text).encode("utf-8", errors="replace").decode("utf-8")
