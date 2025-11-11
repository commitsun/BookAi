import re
import random
import asyncio
import logging
import json
from langchain_openai import ChatOpenAI

log = logging.getLogger("fragmentation")

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
3. Elimina signos de apertura "¿" o "¡", pero deja los de cierre ("?", "!").
4. Elimina comas al final de frase y puntos finales.
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
                return [v.strip() for v in data.values() if isinstance(v, str) and v.strip()]
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
        return

    try:
        fragments = await fragment_text_with_ai(reply)
        if not fragments:
            fragments = fragment_text_intelligently(reply)
    except Exception:
        fragments = fragment_text_intelligently(reply)

    total = len(fragments)

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
                await result
            log.info(f"📤 Enviado fragmento {idx+1}/{total} ({len(frag)} chars)")
        except Exception as e:
            log.error(f"⚠️ Error al enviar fragmento {idx+1}: {e}")
