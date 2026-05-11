from __future__ import annotations

from ..exceptions import NotFoundError
from ..models.document import KBDocument
from ..transports.base import Transport

_DOCUMENT_FIELDS = [
    "id",
    "name",
    "source_type",
    "doc_type",
    "content",
    "source_url",
    "inject_always",
    "vectorize",
    "vector_status",
]


class KBRepository:
    def __init__(self, transport: Transport):
        self._transport = transport

    async def list_by_agent(self, technical_name: str) -> list[KBDocument]:
        agents = await self._transport.search_read(
            "bookai.agent",
            [("technical_name", "=", technical_name)],
            fields=["kb_document_ids"],
            limit=1,
        )
        if not agents:
            raise NotFoundError(f"Agent '{technical_name}' not found")
        doc_ids = agents[0].get("kb_document_ids", [])
        if not doc_ids:
            return []
        records = await self._transport.search_read(
            "bookai.kb.document",
            [("id", "in", doc_ids), ("active", "=", True)],
            fields=_DOCUMENT_FIELDS,
        )
        return [_build_document(r) for r in records]

    async def get_document(self, doc_id: int) -> KBDocument:
        records = await self._transport.read(
            "bookai.kb.document", [doc_id], fields=_DOCUMENT_FIELDS
        )
        if not records:
            raise NotFoundError(f"KB Document {doc_id} not found")
        return _build_document(records[0])

    async def update_vector_status(
        self, doc_id: int, status: str
    ) -> None:
        await self._transport.write(
            "bookai.kb.document",
            [doc_id],
            {"vector_status": status},
        )

    async def create_document(
        self,
        name: str,
        source_type: str = "markdown",
        content: str | None = None,
        doc_type: str | None = None,
        inject_always: bool = True,
        agent_ids: list[int] | None = None,
    ) -> int:
        """Create a KB document. Returns document ID."""
        vals: dict = {
            "name": name,
            "source_type": source_type,
            "inject_always": inject_always,
        }
        if content:
            vals["content"] = content
        if doc_type:
            vals["doc_type"] = doc_type
        if agent_ids:
            vals["agent_ids"] = [(6, 0, agent_ids)]
        return await self._transport.create(
            "bookai.kb.document", vals
        )

    async def update_document(
        self, doc_id: int, **vals
    ) -> None:
        """Update KB document fields."""
        if "agent_ids" in vals and isinstance(
            vals["agent_ids"], list
        ):
            vals["agent_ids"] = [(6, 0, vals["agent_ids"])]
        await self._transport.write(
            "bookai.kb.document", [doc_id], vals
        )


def _build_document(data: dict) -> KBDocument:
    return KBDocument(
        id=data["id"],
        name=data["name"],
        source_type=data["source_type"],
        doc_type=data.get("doc_type") or None,
        content=data.get("content") or None,
        source_url=data.get("source_url") or None,
        inject_always=data.get("inject_always", False),
        vectorize=data.get("vectorize", False),
        vector_status=data.get("vector_status", "not_needed"),
    )
