"""Humanized message fragmentation for WhatsApp delivery.

Splits AI responses into natural fragments with realistic typing
delays, simulating how a human would reply in chat.

Public API:
    fragment_message(text) -> list[str]
    compute_typing_delay(text, is_transition) -> float
    is_topic_transition(text) -> bool
"""

from __future__ import annotations

import logging
import random
import re

log = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────────

SOFT_TARGET = 200       # preferred max chars per fragment
HARD_LIMIT = 320        # absolute max chars per fragment
LONG_SPLIT_SOFT = 160   # soft target when splitting long sentences
LONG_SPLIT_HARD = 280   # hard limit when splitting long sentences
MAX_FRAGMENTS = 10
MAX_SENTENCES_PER_GROUP = 3
MIN_FRAGMENT_LENGTH = 15

# Characters where long sentences can be broken
_BREAK_CHARS = ",;:"

# Known abbreviations whose dots should NOT trigger sentence splits
_ABBREVIATIONS = (
    "Sr", "Sra", "Dr", "Dra", "Prof", "Lic", "Ing", "Arq",
    "Ud", "Uds", "Vd", "Vds", "Av", "Avda",
    "Mr", "Mrs", "Ms", "Jr", "Sr",  # English
    "St", "Ave", "Blvd", "Dept", "Corp", "Inc", "Ltd",
    "vs", "etc", "approx", "e.g", "i.e",
)
_ABBR_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(a) for a in _ABBREVIATIONS) + r")\.",
    re.IGNORECASE,
)

# URL pattern
_URL_PATTERN = re.compile(r"https?://\S+")

# Time patterns like "a.m." / "p.m."
_TIME_PATTERN = re.compile(r"\b([ap])\.m\.", re.IGNORECASE)

# List item detection: lines starting with number+dot/paren, dash, asterisk, bullet
_LIST_LINE = re.compile(r"^\s*(?:\d+[.)]\s|[-*•]\s)")

# Specific markers for nested-list detection
_NUMBERED_LINE = re.compile(r"^\s*\d+[.)]\s")
_BULLET_LINE = re.compile(r"^\s*[-*•]\s")

# Sentence boundary regex (applied AFTER protecting abbreviations/URLs)
_SENTENCE_RE = re.compile(r"[^.!?…]+(?:[.!?…]+\s*|$)", re.S)

# Transition phrases for "thoughtful pause" detection
_TRANSITION_PATTERN = re.compile(
    r"^(?:"
    r"Además|Por otro lado|En cuanto a|Por cierto|"
    r"Respecto a|Con respecto|También|Ahora bien|"
    r"Sin embargo|No obstante|De todas formas|"
    r"However|Also|Regarding|By the way|Additionally|"
    r"As for|On the other hand|Meanwhile|Furthermore|"
    r"That said|In addition"
    r")\b",
    re.IGNORECASE,
)

# Placeholder sentinel (unlikely to appear in real text)
_PH_PREFIX = "\x00PH"
_PH_SUFFIX = "\x00"


# ── Public API ──────────────────────────────────────────────────────


def fragment_message(text: str) -> list[str]:
    """Split text into natural fragments for sequential WhatsApp delivery."""
    clean = (text or "").strip()
    if not clean:
        return [text] if text else []
    if len(clean) <= MIN_FRAGMENT_LENGTH * 2:
        return [clean]

    # Phase 1: split by paragraphs
    paragraphs = _split_paragraphs(clean)

    # Phase 2: extract sentences per paragraph
    all_units: list[_FragUnit] = []
    for para in paragraphs:
        all_units.extend(_extract_units(para))

    # Phase 3: group short units into fragments
    fragments = _group_units(all_units)

    # Phase 4: cap and validate
    fragments = _cap_fragments(fragments)
    fragments = _validate_content(clean, fragments)

    return fragments


def compute_typing_delay(text: str, is_transition: bool = False) -> float:
    """Compute a realistic typing delay in seconds for a fragment."""
    base = random.uniform(0.8, 1.5)
    length_factor = min(len(text) / 180, 2.0)
    jitter = random.uniform(0.2, 0.8)
    transition_bonus = random.uniform(0.6, 1.3) if is_transition else 0.0
    return base + length_factor + jitter + transition_bonus


def is_topic_transition(text: str) -> bool:
    """Detect if a fragment starts with a topic-transition phrase."""
    return bool(_TRANSITION_PATTERN.match((text or "").strip()))


# ── Internal: fragmentation unit ────────────────────────────────────


class _FragUnit:
    """A sentence or list-item that came from one paragraph."""

    __slots__ = ("text", "para_id", "is_list_item")

    def __init__(self, text: str, para_id: int, is_list_item: bool = False):
        self.text = text
        self.para_id = para_id
        self.is_list_item = is_list_item


# ── Internal: Phase 1 ──────────────────────────────────────────────


def _split_paragraphs(text: str) -> list[str]:
    """Split on blank lines, preserving non-empty paragraphs."""
    parts = re.split(r"\n\s*\n", text)
    return [p.strip() for p in parts if p and p.strip()]


# ── Internal: Phase 2 ──────────────────────────────────────────────


_para_counter = 0


def _next_para_id() -> int:
    global _para_counter
    _para_counter += 1
    return _para_counter


def _extract_units(paragraph: str) -> list[_FragUnit]:
    """Extract sentence/list-item units from a single paragraph."""
    para_id = _next_para_id()
    lines = paragraph.split("\n")

    list_lines = sum(1 for ln in lines if _LIST_LINE.match(ln))
    is_list = list_lines >= len(lines) * 0.5 and list_lines >= 2

    if is_list:
        return _extract_list_units(lines, para_id)

    sentences = _split_sentences(paragraph)
    units = []
    for s in sentences:
        for sub in _split_long_sentence(s):
            units.append(_FragUnit(sub, para_id))
    return units


def _extract_list_units(lines: list[str], para_id: int) -> list[_FragUnit]:
    """Extract list units, grouping sub-items with their parent.

    When a list mixes numbered (``1.``) and bullet (``-``) markers,
    the first marker type seen defines "top-level".  Lines with the
    other marker are sub-items that stay grouped with the preceding
    top-level item.  When all markers are the same type, every item
    is top-level (no grouping).
    """
    stripped = [ln.strip() for ln in lines if ln.strip()]

    has_numbered = any(_NUMBERED_LINE.match(ln) for ln in stripped)
    has_bullet = any(_BULLET_LINE.match(ln) for ln in stripped)

    if has_numbered and has_bullet:
        # Detect which type came first → that's the top-level marker
        for ln in stripped:
            if _NUMBERED_LINE.match(ln):
                top_re = _NUMBERED_LINE
                break
            if _BULLET_LINE.match(ln):
                top_re = _BULLET_LINE
                break

        units: list[_FragUnit] = []
        group: list[str] = []
        for ln in stripped:
            if top_re.match(ln) and group:
                _flush_group(group, units, para_id)
                group = []
            group.append(ln)
        _flush_group(group, units, para_id)
        return units

    # Same marker everywhere → each line is a top-level item
    units = []
    for ln in stripped:
        for sub in _split_long_sentence(ln):
            units.append(_FragUnit(sub, para_id, is_list_item=True))
    return units


def _flush_group(
    group: list[str], units: list[_FragUnit], para_id: int,
) -> None:
    """Join grouped lines and append as a single list-item unit."""
    text = "\n".join(group)
    for sub in _split_long_sentence(text):
        units.append(_FragUnit(sub, para_id, is_list_item=True))


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences, protecting abbreviations and URLs."""
    placeholders: list[str] = []

    def _store(match: re.Match) -> str:
        idx = len(placeholders)
        placeholders.append(match.group(0))
        return f"{_PH_PREFIX}{idx}{_PH_SUFFIX}"

    # Protect patterns that contain dots but are NOT sentence boundaries
    protected = _URL_PATTERN.sub(_store, text)
    protected = _TIME_PATTERN.sub(_store, protected)
    protected = _ABBR_PATTERN.sub(_store, protected)

    # Extract sentences
    raw = [m.group(0).strip() for m in _SENTENCE_RE.finditer(protected)]
    if not raw:
        raw = [protected.strip()]

    # Restore placeholders
    def _restore(s: str) -> str:
        for idx, orig in enumerate(placeholders):
            s = s.replace(f"{_PH_PREFIX}{idx}{_PH_SUFFIX}", orig)
        return s

    return [_restore(s) for s in raw if s.strip()]


def _split_long_sentence(text: str) -> list[str]:
    """Split a sentence that exceeds LONG_SPLIT_HARD at punctuation."""
    text = text.strip()
    if not text or len(text) <= LONG_SPLIT_HARD:
        return [text] if text else []

    parts: list[str] = []
    remaining = text

    while len(remaining) > LONG_SPLIT_HARD:
        window = remaining[:LONG_SPLIT_HARD]
        cut = -1
        # Try to break at punctuation
        for ch in _BREAK_CHARS:
            idx = window.rfind(ch, LONG_SPLIT_SOFT // 2)
            if idx > cut:
                cut = idx + 1
        # Fall back to space
        if cut <= 0:
            cut = window.rfind(" ")
        # Last resort: hard cut
        if cut <= 0:
            cut = LONG_SPLIT_HARD

        head = remaining[:cut].strip()
        if head:
            parts.append(head)
        remaining = remaining[cut:].strip()

    if remaining:
        parts.append(remaining)

    return [p for p in parts if p]


# ── Internal: Phase 3 ──────────────────────────────────────────────


def _group_units(units: list[_FragUnit]) -> list[str]:
    """Group consecutive units into fragments respecting limits."""
    if not units:
        return []

    fragments: list[str] = []
    current_text = ""
    current_count = 0
    current_para = units[0].para_id

    for unit in units:
        text = unit.text.strip()
        if not text:
            continue

        # Break on paragraph boundary
        same_para = unit.para_id == current_para
        would_exceed = len(current_text) + len(text) + 1 > HARD_LIMIT
        too_many_sentences = current_count >= MAX_SENTENCES_PER_GROUP
        current_is_long = len(current_text) > SOFT_TARGET * 0.75
        unit_is_long = len(text) > SOFT_TARGET * 0.75

        should_break = (
            not same_para
            or would_exceed
            or too_many_sentences
            or (current_text and current_is_long)
            or (current_text and unit_is_long)
            or (current_text and unit.is_list_item)
        )

        if should_break and current_text:
            fragments.append(current_text.strip())
            current_text = ""
            current_count = 0

        if current_text:
            current_text = f"{current_text} {text}"
        else:
            current_text = text
        current_count += 1
        current_para = unit.para_id

    if current_text.strip():
        fragments.append(current_text.strip())

    # Merge tiny fragments with their neighbor
    return _merge_tiny(fragments)


def _merge_tiny(fragments: list[str]) -> list[str]:
    """Merge fragments shorter than MIN_FRAGMENT_LENGTH with neighbors."""
    if len(fragments) <= 1:
        return fragments

    merged: list[str] = []
    for frag in fragments:
        if merged and len(frag) < MIN_FRAGMENT_LENGTH:
            merged[-1] = f"{merged[-1]} {frag}"
        elif merged and len(merged[-1]) < MIN_FRAGMENT_LENGTH:
            merged[-1] = f"{merged[-1]} {frag}"
        else:
            merged.append(frag)
    return merged


# ── Internal: Phase 4 ──────────────────────────────────────────────


def _cap_fragments(fragments: list[str]) -> list[str]:
    """Enforce MAX_FRAGMENTS by merging the tail."""
    if len(fragments) <= MAX_FRAGMENTS:
        return fragments
    head = fragments[: MAX_FRAGMENTS - 1]
    tail = " ".join(f.strip() for f in fragments[MAX_FRAGMENTS - 1:] if f.strip())
    if tail:
        head.append(tail)
    return head


def _normalize(text: str) -> str:
    """Collapse whitespace for comparison."""
    return re.sub(r"\s+", " ", (text or "").strip())


def _validate_content(original: str, fragments: list[str]) -> list[str]:
    """Verify fragments preserve the original content."""
    rebuilt = " ".join(f.strip() for f in fragments if f.strip())
    if _normalize(original) == _normalize(rebuilt):
        return fragments
    log.warning("Fragmentation altered content; falling back to single message")
    return [original.strip()]
