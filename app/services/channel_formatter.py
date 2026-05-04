"""
Channel-aware text formatting.

Converts standard Markdown produced by LLMs into the native formatting
syntax of each delivery channel before sending.

Public API:
    format_for_channel(text, channel) -> str
"""

from __future__ import annotations

import re

# ── WhatsApp conversion rules ─────────────────────────────────────
#
# WhatsApp rich text uses single-symbol wrappers:
#   *bold*   _italic_   ~strikethrough~   ```code```   `code`
#
# LLMs typically produce standard Markdown:
#   **bold**  __italic__  ~~strikethrough~~  ### heading
#
# The rules below convert Markdown → WhatsApp, preserving anything
# that is already WhatsApp-compatible.

# Protected zones: code blocks and inline code should NOT be touched.
_CODE_BLOCK = re.compile(r"```[\s\S]*?```")
_INLINE_CODE = re.compile(r"`[^`]+`")

# Markdown → WhatsApp substitutions (order matters)
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)        # **text** → *text*
_MD_ITALIC = re.compile(r"__(.+?)__", re.S)           # __text__ → _text_
_MD_STRIKE = re.compile(r"~~(.+?)~~", re.S)           # ~~text~~ → ~text~
_MD_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.M)    # ### heading → *heading*

# Placeholder sentinel for protected zones
_PH = "\x00CB"


def format_for_channel(text: str, channel: str) -> str:
    """Format *text* for the target *channel*.

    Currently supported channels:
    - ``whatsapp``: converts Markdown → WhatsApp rich text.
    - Any other value: returns text unchanged.
    """
    if not text:
        return text
    if channel == "whatsapp":
        return _format_whatsapp(text)
    return text


def _format_whatsapp(text: str) -> str:
    """Convert standard Markdown to WhatsApp rich-text syntax."""
    # 1. Protect code blocks / inline code from transformation
    protected: list[str] = []

    def _protect(m: re.Match) -> str:
        idx = len(protected)
        protected.append(m.group(0))
        return f"{_PH}{idx}{_PH}"

    result = _CODE_BLOCK.sub(_protect, text)
    result = _INLINE_CODE.sub(_protect, result)

    # 2. Apply conversions (order: bold before italic to avoid conflicts)
    result = _MD_BOLD.sub(r"*\1*", result)
    result = _MD_ITALIC.sub(r"_\1_", result)
    result = _MD_STRIKE.sub(r"~\1~", result)
    result = _MD_HEADING.sub(r"*\1*", result)

    # 3. Restore protected zones
    for idx, original in enumerate(protected):
        result = result.replace(f"{_PH}{idx}{_PH}", original)

    return result
