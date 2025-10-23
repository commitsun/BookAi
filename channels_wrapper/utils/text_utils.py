# channels_wrapper/utils/text_utils.py
import re
import random
import asyncio

def fragment_text_intelligently(text: str, max_fragments: int = 4) -> list[str]:
    """Fragmenta texto largo en partes naturales sin cortar frases."""
    text = re.sub(r'\n{2,}', '\n', text.strip())
    raw_parts = re.split(r'(?:(?<=\n)\d+\.|\n-|\n•|\n(?=[A-Z]))', text)
    fragments, buffer = [], ""

    for part in raw_parts:
        p = part.strip()
        if not p:
            continue
        if re.match(r'^(\d+\.|-|•)\s', p):
            if buffer:
                fragments.append(buffer.strip())
                buffer = ""
            fragments.append(p)
            continue

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


def _simulate_typing_delay_seconds(text: str) -> float:
    base = random.uniform(0.8, 1.6)
    length_factor = min(len(text) / 120, 3)
    return base + length_factor


async def sleep_typing_async(text: str):
    await asyncio.sleep(_simulate_typing_delay_seconds(text))


async def send_fragmented_async(send_callable, user_id: str, reply: str):
    """Envía mensaje largo en fragmentos con delays humanos."""
    frags = fragment_text_intelligently(reply)
    for frag in frags:
        await sleep_typing_async(frag)
        maybe_coro = send_callable(user_id, frag)
        if asyncio.iscoroutine(maybe_coro):
            await maybe_coro
