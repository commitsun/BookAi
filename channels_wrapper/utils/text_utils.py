import re
import random
import asyncio
import logging
from langchain_openai import ChatOpenAI

log = logging.getLogger("fragmentation")
_CUT_MARKER = "<<BOOKAI_CUT>>"

# Divide fragmentos largos sin reescribir contenido ni añadir puntuación.
# Se usa en el flujo de fragmentación, typing y envío escalonado de texto para preparar datos, validaciones o decisiones previas.
# Recibe `fragment`, `hard_limit` como entradas relevantes junto con el contexto inyectado en la firma.
# Devuelve un `list[str]` con el resultado de esta operación. Sin efectos secundarios relevantes.
def _split_long_fragment_preserving_text(fragment: str, hard_limit: int = 210) -> list[str]:
    """Divide fragmentos largos sin reescribir contenido ni añadir puntuación."""
    text = (fragment or "").strip()
    if not text:
        return []
    if len(text) <= hard_limit:
        return [text]

    parts: list[str] = []
    remaining = text
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
    return [part for part in parts if part]


# Parte por frases preservando el texto original.
# Se usa en el flujo de fragmentación, typing y envío escalonado de texto para preparar datos, validaciones o decisiones previas.
# Recibe `text` como entrada principal según la firma.
# Devuelve un `list[str]` con el resultado de esta operación. Sin efectos secundarios relevantes.
def _collect_sentence_fragments(text: str) -> list[str]:
    """Parte por frases preservando el texto original."""
    stripped = (text or "").strip().replace("\r", "")
    if not stripped:
        return []

    fragments: list[str] = []
    sentence_re = re.compile(r".+?(?:[.!?…]+(?=\s|$)|$)", re.S)
    for match in sentence_re.finditer(stripped):
        fragment = match.group(0).strip()
        if fragment:
            fragments.append(fragment)

    return fragments or [stripped]


# Normaliza para comparison.
# Se usa en el flujo de fragmentación, typing y envío escalonado de texto para preparar datos, validaciones o decisiones previas.
# Recibe `text` como entrada principal según la firma.
# Devuelve un `str` con el resultado de esta operación. Sin efectos secundarios relevantes.
def _normalize_for_comparison(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


# Resuelve preserve source.
# Se usa en el flujo de fragmentación, typing y envío escalonado de texto para preparar datos, validaciones o decisiones previas.
# Recibe `original`, `fragments` como entradas relevantes junto con el contexto inyectado en la firma.
# Devuelve un `bool` con el resultado de esta operación. Sin efectos secundarios relevantes.
def _fragments_preserve_source(original: str, fragments: list[str]) -> bool:
    rebuilt = " ".join((frag or "").strip() for frag in fragments if (frag or "").strip())
    return _normalize_for_comparison(original) == _normalize_for_comparison(rebuilt)

# Divide el texto en fragmentos naturales preservando el contenido original.
# Se usa en el flujo de fragmentación, typing y envío escalonado de texto para preparar datos, validaciones o decisiones previas.
# Recibe `text`, `max_fragments` como entradas relevantes junto con el contexto inyectado en la firma.
# Devuelve un `list[str]` con el resultado de esta operación. Sin efectos secundarios relevantes.
def fragment_text_intelligently(text: str, max_fragments: int = 12) -> list[str]:
    """Divide el texto en fragmentos naturales preservando el contenido original."""
    if not text or not isinstance(text, str):
        return []

    base_fragments = _collect_sentence_fragments(text)
    fragments: list[str] = []
    for frag in base_fragments:
        fragments.extend(_split_long_fragment_preserving_text(frag))

    if len(fragments) > max_fragments:
        head = fragments[: max_fragments - 1]
        tail = " ".join(part.strip() for part in fragments[max_fragments - 1:] if part.strip())
        fragments = head + ([tail] if tail else [])

    return [f.strip() for f in fragments if f and f.strip()]


# Usa IA solo para decidir puntos de corte.
# Se usa en el flujo de fragmentación, typing y envío escalonado de texto para preparar datos, validaciones o decisiones previas.
# Recibe `text`, `max_fragments` como entradas relevantes junto con el contexto inyectado en la firma.
# Devuelve un `list[str]` con el resultado de esta operación. Puede realizar llamadas externas o a modelos.
async def fragment_text_with_ai(text: str, max_fragments: int = 9) -> list[str]:
    """
    Usa IA solo para decidir puntos de corte.
    Si la IA altera el contenido, se descarta y se usa el fragmentador determinista.
    """
    raw_text = (text or "").strip()
    if not raw_text:
        return []
    if len(raw_text) < 40:
        return [raw_text]

    llm = ChatOpenAI(model="gpt-4.1-mini", temperature=0)
    prompt = f"""
Tu única tarea es insertar el marcador {_CUT_MARKER} en el texto original para indicar cortes naturales.

Reglas obligatorias:
1. No cambies, corrijas, traduzcas, resumas ni reescribas nada.
2. No añadas ni elimines ningún carácter del texto original, salvo el marcador {_CUT_MARKER}.
3. Mantén exactamente el mismo orden, palabras y puntuación.
4. Usa como máximo {max_fragments} fragmentos.
5. Si no hace falta dividir, devuelve el texto original tal cual, sin marcador.
6. Devuelve solo el texto final con los marcadores, sin comillas, sin JSON y sin explicaciones.

Texto original:
---
{raw_text}
---
""".strip()

    try:
        response = await llm.ainvoke(prompt)
        candidate = (getattr(response, "content", None) or str(response or "")).strip()
        if not candidate:
            return fragment_text_intelligently(raw_text, max_fragments=max_fragments)

        # Tolerar code fences accidentales del modelo.
        candidate = re.sub(r"^```(?:text)?\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate)

        if _CUT_MARKER not in candidate:
            return fragment_text_intelligently(raw_text, max_fragments=max_fragments)

        base_fragments = [frag.strip() for frag in candidate.split(_CUT_MARKER) if frag and frag.strip()]
        if not base_fragments:
            return fragment_text_intelligently(raw_text, max_fragments=max_fragments)

        if not _fragments_preserve_source(raw_text, base_fragments):
            log.warning("Fragmentador IA alteró contenido; usando fallback determinista.")
            return fragment_text_intelligently(raw_text, max_fragments=max_fragments)

        fragments: list[str] = []
        for frag in base_fragments:
            fragments.extend(_split_long_fragment_preserving_text(frag))

        if not fragments or not _fragments_preserve_source(raw_text, fragments):
            return fragment_text_intelligently(raw_text, max_fragments=max_fragments)

        if len(fragments) > max_fragments:
            return fragment_text_intelligently(raw_text, max_fragments=max_fragments)

        return fragments
    except Exception as exc:
        log.warning("Error en fragmentador IA; usando fallback determinista: %s", exc)
        return fragment_text_intelligently(raw_text, max_fragments=max_fragments)


# Simula typing retardo seconds.
# Se usa en el flujo de fragmentación, typing y envío escalonado de texto para preparar datos, validaciones o decisiones previas.
# Recibe `text`, `thoughtful` como entradas relevantes junto con el contexto inyectado en la firma.
# Devuelve un `float` con el resultado de esta operación. Sin efectos secundarios relevantes.
def _simulate_typing_delay_seconds(text: str, thoughtful: bool = False) -> float:
    base = random.uniform(1.0, 1.8)
    length_factor = min(len(text) / 150, 2.5)
    variability = random.uniform(0.3, 1.1)
    thoughtful_delay = random.uniform(0.8, 1.5) if thoughtful else 0
    return base + length_factor + variability + thoughtful_delay


# Espera typing asincronía.
# Se usa en el flujo de fragmentación, typing y envío escalonado de texto para preparar datos, validaciones o decisiones previas.
# Recibe `text`, `thoughtful` como entradas relevantes junto con el contexto inyectado en la firma.
# No devuelve un valor relevante; deja preparado el estado o ejecuta la acción necesaria. Sin efectos secundarios relevantes.
async def sleep_typing_async(text: str, thoughtful: bool = False):
    delay = _simulate_typing_delay_seconds(text, thoughtful)
    await asyncio.sleep(delay)


# Envía la respuesta en fragmentos naturales sin reescribir el contenido.
# Se usa en el flujo de fragmentación, typing y envío escalonado de texto para preparar datos, validaciones o decisiones previas.
# Recibe `send_callable` como dependencias o servicios compartidos inyectados desde otras capas, y `user_id`, `reply` como datos de contexto o entrada de la operación.
# Produce la acción solicitada y prioriza el efecto lateral frente a un retorno complejo. Sin efectos secundarios relevantes.
async def send_fragmented_async(send_callable, user_id: str, reply: str):
    """
    Envía la respuesta en fragmentos naturales sin reescribir el contenido.
    - Usa pausas humanas entre mensajes.
    - Conserva el texto tal cual lo generó el agente.
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
