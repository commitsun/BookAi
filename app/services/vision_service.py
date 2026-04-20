"""
Image description using a vision-capable LLM.
Returns description text and token usage for cost tracking.
"""

import base64
import logging
import os

from litellm import acompletion, cost_per_token

log = logging.getLogger("vision_service")

_VISION_PROMPT = (
    "Describe what you see in this image in 1-2 sentences. "
    "Be factual and objective. Do not refuse — just describe the visual content. "
    "If it's a photo of a place, describe the place. If it's a document, "
    "summarize what it says. If it's a screenshot, describe what it shows. "
    "Respond in Spanish."
)


async def describe_image(
    file_path: str,
    api_key: str,
    model: str = "gpt-4o-mini",
    provider: str = "openai",
) -> tuple[str | None, int, int, float]:
    """Describe an image using a vision-capable model.

    Returns (description, tokens_in, tokens_out, cost_usd).
    """
    if not os.path.exists(file_path):
        log.warning("Image file not found: %s", file_path)
        return None, 0, 0, 0.0

    try:
        with open(file_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")

        ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else "jpeg"
        mime_map = {"jpg": "jpeg", "png": "png", "webp": "webp", "gif": "gif"}
        media_type = f"image/{mime_map.get(ext, 'jpeg')}"

        litellm_model = f"{provider}/{model}" if "/" not in model else model

        response = await acompletion(
            model=litellm_model,
            api_key=api_key,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": _VISION_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media_type};base64,{image_data}",
                        },
                    },
                ],
            }],
            max_tokens=150,
            temperature=0.2,
        )

        text = response.choices[0].message.content.strip()
        usage = response.usage
        tokens_in = usage.prompt_tokens if usage else 0
        tokens_out = usage.completion_tokens if usage else 0

        cost = 0.0
        try:
            p, c = cost_per_token(
                response.model or model,
                prompt_tokens=tokens_in,
                completion_tokens=tokens_out,
            )
            cost = p + c
        except Exception:
            pass

        log.info(
            "Image described: %d chars, %d/%d tokens, $%.6f from %s",
            len(text), tokens_in, tokens_out, cost, file_path,
        )
        return (text if text else None), tokens_in, tokens_out, cost

    except Exception as exc:
        log.error("Vision failed for %s: %s", file_path, exc)
        return None, 0, 0, 0.0
