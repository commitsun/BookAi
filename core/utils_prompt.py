from pathlib import Path

def load_prompt(filename: str) -> str:
    """
    Carga un prompt desde la carpeta 'prompts' con manejo seguro de encoding.
    Si encuentra caracteres inválidos, los reemplaza por '�' en lugar de fallar.
    """
    return (Path("prompts") / filename).read_text(
        encoding="utf-8",
        errors="replace"   # 👈 evita crash por caracteres corruptos
    )
