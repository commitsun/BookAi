"""
Pure policy resolution for agent execution modes.

No I/O, no DB, no network. All functions are deterministic
and can be unit-tested in isolation.
"""

# ── Ordering constants ────────────────────────────────────────────────

ROLE_ORDER = {"advisor": 0, "assistant": 1, "operator": 2}
CONFIRM_ORDER = {"always": 3, "sensitive": 2, "irreversible": 1, "never": 0}
LOG_ORDER = {"basic": 0, "full": 1, "debug": 2}


# ── Confirmation matrix ───────────────────────────────────────────────
#
#              none    sensitive  irreversible
# always       ✓       ✓          ✓
# sensitive    ✗       ✓          ✓
# irreversible ✗       ✗          ✓
# never        ✗       ✗          ✗

_CONFIRM_MATRIX = {
    ("always", "none"): True,
    ("always", "sensitive"): True,
    ("always", "irreversible"): True,
    ("sensitive", "none"): False,
    ("sensitive", "sensitive"): True,
    ("sensitive", "irreversible"): True,
    ("irreversible", "none"): False,
    ("irreversible", "sensitive"): False,
    ("irreversible", "irreversible"): True,
    ("never", "none"): False,
    ("never", "sensitive"): False,
    ("never", "irreversible"): False,
}


def needs_confirmation(
    policy: str,
    sensitivity: str,
    requires_confirm_legacy: bool = False,
) -> bool:
    """Determine if a tool call needs guest confirmation.

    Backward compat: if requires_confirm=True and sensitivity="none",
    treat as sensitivity="sensitive".
    """
    if requires_confirm_legacy and sensitivity == "none":
        sensitivity = "sensitive"
    return _CONFIRM_MATRIX.get((policy, sensitivity), False)


def should_include_tool(role: str, sensitivity: str) -> bool:
    """Determine if a tool should be visible to an agent with this role.

    advisor: only read tools (sensitivity="none")
    assistant/operator: all tools
    """
    if role == "advisor" and sensitivity in ("sensitive", "irreversible"):
        return False
    return True


# ── Effective policy resolution (supervisor → worker) ─────────────────

def resolve_effective_role(
    supervisor_role: str, worker_role: str,
) -> str:
    """More restrictive role wins."""
    sup = ROLE_ORDER.get(supervisor_role, 1)
    wrk = ROLE_ORDER.get(worker_role, 1)
    idx = min(sup, wrk)
    for name, order in ROLE_ORDER.items():
        if order == idx:
            return name
    return "assistant"


def resolve_effective_confirmation(
    supervisor_policy: str, worker_policy: str,
) -> str:
    """More demanding confirmation policy wins."""
    sup = CONFIRM_ORDER.get(supervisor_policy, 2)
    wrk = CONFIRM_ORDER.get(worker_policy, 2)
    idx = max(sup, wrk)
    for name, order in CONFIRM_ORDER.items():
        if order == idx:
            return name
    return "sensitive"


def resolve_effective_log_level(
    supervisor_log: str, worker_log: str,
) -> str:
    """More detailed log level wins."""
    sup = LOG_ORDER.get(supervisor_log, 0)
    wrk = LOG_ORDER.get(worker_log, 0)
    idx = max(sup, wrk)
    for name, order in LOG_ORDER.items():
        if order == idx:
            return name
    return "basic"


# ── Log level filtering ───────────────────────────────────────────────

# step_types logged at each level
_LOG_STEP_TYPES = {
    "basic": {"tool_call", "error"},
    "full": {"tool_call", "error", "delegation", "confirmation", "decision"},
    "debug": {"tool_call", "error", "delegation", "confirmation", "decision", "escalation"},
}


def should_log_step(
    log_level: str, step_type: str,
) -> tuple[bool, bool, bool]:
    """Determine what to log for a step.

    Returns (should_log, include_args, include_result).
    """
    allowed = _LOG_STEP_TYPES.get(log_level, _LOG_STEP_TYPES["basic"])
    if step_type not in allowed:
        return False, False, False

    include_args = log_level in ("full", "debug")
    include_result = log_level == "debug"
    return True, include_args, include_result
