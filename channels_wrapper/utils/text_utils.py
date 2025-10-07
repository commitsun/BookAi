import re
import random
import time

# ------------------------------------------------------------------
# 🧠 Fragmentación de texto
# ------------------------------------------------------------------
def fragment_text_intelligently(text: str, max_fragments: int = 4) -> list[str]:
    """
    Fragmenta texto largo en partes más pequeñas sin cortar frases a mitad.
    Ideal para enviar respuestas por partes en canales como WhatsApp o Telegram.
    """
    text = re.sub(r'\n{2,}', '\n', text.strip())
    raw_parts = re.split(r'(?:(?<=\n)\d+\.|\n-|\n•|\n(?=[A-Z]))', text)
    fragments, buffer = [], ""

    for part in raw_parts:
        p = part.strip()
        if not p:
            continue

        # Listas o ítems
        if re.match(r'^(\d+\.|-|•)\s', p):
            if buffer:
                fragments.append(buffer.strip())
                buffer = ""
            fragments.append(p)
            continue

        # Fragmentación de párrafos largos
        if len(p) > 500:
            subparts = re.split(r'(?<=[.!?])\s+', p)
            temp_chunk = ""
            for s in subparts:
                if len(temp_chunk) + len(s) < 300:
                    temp_chunk += (" " if temp_chunk else "") + s
                else:
                    fragments.append(temp_chunk.strip())
                    temp_chunk = s
            if temp_chunk:
                fragments.append(temp_chunk.strip())
        else:
            if len(buffer) + len(p) < 400:
                buffer += ("\n\n" if buffer else "") + p
            else:
                fragments.append(buffer.strip())
                buffer = p

    if buffer:
        fragments.append(buffer.strip())

    # Limitar cantidad de fragmentos
    if len(fragments) > max_fragments:
        merged, temp = [], ""
        for f in fragments:
            if len(temp) + len(f) < 500:
                temp += ("\n\n" if temp else "") + f
            else:
                merged.append(temp)
                temp = f
        if temp:
            merged.append(temp)
        fragments = merged[:max_fragments]

    return fragments


# ------------------------------------------------------------------
# ⏳ Simulación de escritura / delay humano
# ------------------------------------------------------------------
def simulate_typing_delay(text: str):
    """
    Devuelve un tiempo de espera "humano" según la longitud del texto.
    Útil para hacer más natural el envío de mensajes.
    """
    base = random.uniform(1.0, 2.0)
    length_factor = min(len(text) / 100, 3)
    return base + length_factor


def sleep_typing(text: str):
    """Bloquea el hilo simulando un tiempo de escritura humano."""
    delay = simulate_typing_delay(text)
    time.sleep(delay)
