import os
from contextlib import contextmanager
from typing import Dict, List, Optional
from langsmith import tracing_context
from langsmith.run_helpers import get_current_run_tree

PROJECT = os.getenv("LANGCHAIN_PROJECT", "BookAI")
SAMPLING = float(os.getenv("LANGSMITH_SAMPLING_RATE", "1.0"))

@contextmanager
def ls_context(
    name: Optional[str] = None,
    metadata: Optional[Dict] = None,
    tags: Optional[List[str]] = None,
    parent: Optional[Dict] = None,
):
    """
    Contexto corto para anidar metadatos/tags en las trazas.
    """
    if SAMPLING <= 0:
        yield
        return
    kwargs = {"project_name": PROJECT}
    if name:
        kwargs["name"] = name
    if metadata:
        kwargs["metadata"] = metadata
    if tags:
        kwargs["tags"] = tags
    if parent:
        kwargs["parent"] = parent
    with tracing_context(**kwargs):
        yield

def current_headers_for_propagation() -> Dict:
    """
    Propaga el contexto actual a sub-agentes/servicios (si aplica).
    """
    run_tree = get_current_run_tree()
    return run_tree.to_headers() if run_tree else {}