import json
import os
import re
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


def _build_client() -> Optional[OpenAI]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        return OpenAI(api_key=api_key)
    except Exception as exc:
        print(f"⚠️ No se pudo inicializar OpenAI: {exc}")
        return None


_client = _build_client()


def is_variable_doc(file_name: str) -> bool:
    """Detecta si el archivo corresponde al documento Variable (no toca Fijo)."""
    return "variable" in file_name.lower()


def _split_sections(text: str) -> List[str]:
    """
    Divide el documento en bloques independientes.
    - Preferimos encabezados tipo "[2025-12-01 11:41 UTC]" para evitar que un bloque con fecha vencida arrastre a los demás.
    - Si no hay encabezados, se usan párrafos separados por doble salto de línea.
    """
    header = re.compile(r"^\s*\[\d{4}-\d{2}-\d{2}.*?\]")
    lines = text.splitlines()

    sections: List[List[str]] = []
    current: List[str] = []

    for line in lines:
        if header.match(line) and current:
            sections.append(current)
            current = [line]
        else:
            current.append(line)

    if current:
        sections.append(current)

    # Si no detectamos encabezados y solo hay un bloque, caemos al split por párrafos.
    if len(sections) <= 1:
        return [s.strip() for s in text.split("\n\n") if s.strip()]

    return ["\n".join(block).strip() for block in sections if "\n".join(block).strip()]


def _should_keep_section(section: str, now_utc: datetime) -> Tuple[bool, Optional[str], str]:
    """
    Usa un modelo IA para decidir si un bloque sigue vigente.
    Devuelve: keep, fecha_fin_detectada_iso (o None), razon.
    """
    if not _client:
        return True, None, "OpenAI no configurado"

    now_iso = now_utc.isoformat()
    prompt = (
        "Eres un asistente que limpia avisos caducados de un hotel. "
        f"La fecha y hora actuales en UTC son: {now_iso}.\n\n"
        "Analiza el siguiente bloque de texto en español. "
        "Si el contenido tiene un plazo/fecha límite o un rango de fechas y dicho plazo ya terminó "
        "(la fecha final es anterior a 'ahora'), responde keep:false. "
        "Si el contenido no tiene una fecha límite clara o todavía está vigente, responde keep:true.\n\n"
        "Ten en cuenta:\n"
        "- Puede haber rangos como 'del 26 al 27 de este mes', '23 y 24 de diciembre', 'hasta el 15 de enero'.\n"
        "- Si no se menciona el año, asume el año actual; si el mes ya pasó este año, asume que fue en el año actual y ya venció.\n"
        "- Frases como 'este mes', 'la próxima semana', 'mañana' o 'hoy' son relativas a la fecha actual.\n"
        "- Si no hay señales de fecha/validez, conserva el bloque (keep:true).\n\n"
        "Devuelve estrictamente JSON con el formato: "
        '{\"keep\": <bool>, \"deadline_end_iso\": <string|null>, \"reason\": <string>}. '
        "Si no puedes determinar una fecha final, usa null en deadline_end_iso."
    )

    try:
        completion = _client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": section},
            ],
        )
        content = completion.choices[0].message.content or "{}"
        data = json.loads(content)
        keep = bool(data.get("keep", True))
        deadline_end = data.get("deadline_end_iso")
        reason = data.get("reason") or ""
        return keep, deadline_end, reason
    except Exception as exc:
        print(f"⚠️ No se pudo evaluar caducidad con IA: {exc}")
        return True, None, "Fallo IA: se conserva por seguridad"


def filter_expired_sections(
    text: str, *, now_utc: Optional[datetime] = None
) -> Tuple[str, List[str]]:
    """
    Elimina bloques caducados del documento Variable.
    Devuelve el texto filtrado y un resumen de los bloques eliminados.
    """
    if not text.strip():
        return text, []

    now = now_utc or datetime.now(timezone.utc)
    sections = _split_sections(text)
    kept_sections: List[str] = []
    removed_summaries: List[str] = []

    for section in sections:
        keep, deadline_end, reason = _should_keep_section(section, now)
        if keep:
            kept_sections.append(section)
            continue

        deadline_info = f"deadline={deadline_end}" if deadline_end else "deadline=desconocido"
        snippet = section.replace("\n", " ")[:120]
        removed_summaries.append(f"{deadline_info} | {reason} | '{snippet}...'")

    filtered_text = "\n\n".join(kept_sections)
    return filtered_text, removed_summaries
