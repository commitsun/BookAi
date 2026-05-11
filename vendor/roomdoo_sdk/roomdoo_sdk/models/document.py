from __future__ import annotations

from dataclasses import dataclass


@dataclass
class KBDocument:
    id: int
    name: str
    source_type: str
    inject_always: bool
    vectorize: bool
    vector_status: str
    doc_type: str | None = None
    content: str | None = None
    source_url: str | None = None
