import re
import random
import asyncio
import logging
import json
from langchain_openai import ChatOpenAI

log = logging.getLogger("fragmentation")

_END_PUNCTUATION_RE = re.compile(r"[.!?…:;)\]]$")


def _normalize_fragment_punctuation(fragment: str) -> str:
    text = (fragment or "").strip()
    if not text:
        return ""
    if _END_PUNCTUATION_RE.search(text):
        return text
    if re.search(r"https?://|www\.", text, re.IGNORECASE):
        return text
    if "\n" in text:
        return text
    if len(text) < 18:
        return text
    if text.endswith((",", "-", "•")):
        return text.rstrip(", -•") + "."
    return text + "."


def _split_long_unpunctuated_fragment(fragment: str) -> list[str]:
    text = (fragment or "").strip()
    if not text:
        return []
    if len(text) < 220:
        return [text]

    parts: list[str] = []
    remaining = text
    hard_limit = 210
    soft_target = 150
    break_chars = ",;:"

    while len(remaining) > hard_limit:
        window = remaining[:hard_limit]
        cut = -1
        for ch in break_chars:
            idx = window.rfind(ch, soft_target // 2)
            if idx > cut:
                cut = idx + 1
        if cut <= 0:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = hard_limit
        head = remaining[:cut].strip()
        if head:
            parts.append(head)
        remaining = remaining[cut:].strip()

    if remaining:
        parts.append(remaining)
    return [_normalize_fragment_punctuation(part) for part in parts if part]

# ============================================================
# 🔹 Fallback clásico: fragmentación natural tipo “n8n”
# ============================================================
def fragment_text_intelligently(text: str, max_fragments: int = 12) -> list[str]:
    """Divide el texto en frases naturales (por puntos, exclamaciones o interrogaciones)."""
    if not text or not isinstance(text, str):
        return []

    text = text.strip().replace("\r", "")
    text = re.sub(r'\s+', ' ', text)

    raw_fragments = re.split(r'(?<=[.!?])\s+', text)
    fragments = []
    for frag in raw_fragments:
        frag = frag.strip()
        if not frag:
            continue
        if fragments and len(fragments[-1]) < 60:
            fragments[-1] = f"{fragments[-1]} {frag}"
        else:
            fragments.append(frag)

    clean = []
    for frag in fragments:
        if len(frag) > 400:
            sub = re.split(r'(?<=,)\s+', frag)
            clean.extend(sub)
        else:
            clean.append(frag)

    if len(clean) > max_fragments:
        clean = clean[:max_fragments - 1] + [" ".join(clean[max_fragments - 1:])]

    return [f.strip() for f in clean if f.strip()]


# ============================================================
# 🤖 IA Fragmentadora: GPT-4-mini
# ============================================================
async def fragment_text_with_ai(text: str, max_fragments: int = 9) -> list[str]:
    """
    Usa IA (GPT-4-mini) para dividir el texto en fragmentos cortos y naturales.
    Devuelve un JSON con claves "A1", "A2", ... según la estructura del ejemplo.
    """
    if not text or len(text.strip()) < 40:
        return [text.strip()] if text else []

    llm = ChatOpenAI(model="gpt-4.1-mini", temperature=0.3)

    prompt = f"""
Eres un experto en dividir textos largos en mensajes cortos y naturales.

Sigue estas reglas:
1. Divide el texto original en fragmentos consecutivos, sin alterar el orden.
2. Usa un máximo de {max_fragments} fragmentos ("A1", "A2", ..., "A{max_fragments}").
3. Conserva la puntuación original; no quites puntos, comas, dos puntos, interrogaciones ni exclamaciones.
4. Si una frase del texto original termina en punto, interrogación o exclamación, respétalo.
5. No borres los dos puntos ":" si hay listados.
6. Elimina expresiones robóticas o latinoamericanas como "en qué puedo asistirte", "con gusto te ayudo", "estoy aquí para ayudarte".
7. Si el texto es muy largo, resume sin perder lo esencial.
8. Devuelve ÚNICAMENTE un objeto JSON válido, sin texto adicional.

Ejemplo de salida:
{{
  "A1": "Hola, muchas gracias por tus palabras!",
  "A2": "Me alegra que te gusten mis vídeos",
  "A3": "Estás interesado en el mundo de Amazon FBA?"
}}

Texto a fragmentar:
---
{text.strip()}
---
"""

    try:
        response = await llm.ainvoke(prompt)
        raw = response.content.strip()

        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                base = [v.strip() for v in data.values() if isinstance(v, str) and v.strip()]
                fixed: list[str] = []
                for item in base:
                    fixed.extend(_split_long_unpunctuated_fragment(item))
                return [_normalize_fragment_punctuation(v) for v in fixed if v.strip()]
        except json.JSONDecodeError:
            log.warning("⚠️ No se pudo parsear JSON de la IA, usando fallback clásico.")
            return fragment_text_intelligently(text)

    except Exception as e:
        log.error(f"⚠️ Error en fragment_text_with_ai: {e}")
        return fragment_text_intelligently(text)


# ============================================================
# ⏳ Simulación de escritura humana
# ============================================================
def _simulate_typing_delay_seconds(text: str, thoughtful: bool = False) -> float:
    base = random.uniform(1.0, 1.8)
    length_factor = min(len(text) / 150, 2.5)
    variability = random.uniform(0.3, 1.1)
    thoughtful_delay = random.uniform(0.8, 1.5) if thoughtful else 0
    return base + length_factor + variability + thoughtful_delay


async def sleep_typing_async(text: str, thoughtful: bool = False):
    delay = _simulate_typing_delay_seconds(text, thoughtful)
    await asyncio.sleep(delay)


# ============================================================
# 💬 Envío fragmentado con ritmo humano (IA + fallback)
# ============================================================
async def send_fragmented_async(send_callable, user_id: str, reply: str):
    """
    Envía la respuesta en fragmentos naturales (prioriza IA).
    - Usa pausas humanas entre mensajes.
    - Mantiene coherencia y tono cálido.
    """
    if not reply or not isinstance(reply, str):
        return []

    try:
        fragments = await fragment_text_with_ai(reply)
        if not fragments:
            fragments = fragment_text_intelligently(reply)
    except Exception:
        fragments = fragment_text_intelligently(reply)

    normalized: list[str] = []
    for frag in fragments:
        normalized.extend(_split_long_unpunctuated_fragment(frag))
    fragments = [_normalize_fragment_punctuation(frag) for frag in normalized if frag and frag.strip()]

    total = len(fragments)
    results = []

    for idx, frag in enumerate(fragments):
        frag = frag.strip()
        if not frag:
            continue

        # 🧠 Simula pausas pensativas si hay cambio de tema
        thoughtful = bool(re.match(r"^(Además|Por otro|En cuanto|Por cierto)", frag))
        await sleep_typing_async(frag, thoughtful)

        try:
            result = send_callable(user_id, frag)
            if asyncio.iscoroutine(result):
                result = await result
            results.append(result)
            log.info(f"📤 Enviado fragmento {idx+1}/{total} ({len(frag)} chars)")
        except Exception as e:
            log.error(f"⚠️ Error al enviar fragmento {idx+1}: {e}")

    return results
