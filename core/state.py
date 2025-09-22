from typing import Literal, TypedDict, List

class GraphState(TypedDict):
    messages: List[dict]
    route: Literal["general_info", "pricing", "other"] | None
    rationale: str | None
    language: str | None   # idioma detectado
