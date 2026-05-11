from __future__ import annotations

from dataclasses import dataclass, field

from .llm_account import LLMAccount


@dataclass
class AgentToolBinding:
    """A tool bound to an agent, with optional overrides."""
    binding_id: int
    tool_id: int
    tool_name: str
    tool_type: str
    description: str | None = None
    sdk_method: str | None = None
    input_schema: str | None = None
    requires_confirm: bool = False
    action_sensitivity: str = "none"
    active: bool = True
    endpoint_url: str | None = None
    endpoint_headers: dict | None = None


@dataclass
class AgentConfig:
    id: int
    name: str
    technical_name: str
    description: str
    caller_type: str
    active: bool
    llm_account: LLMAccount | None
    system_prompt: str
    context_template: str
    # LLM
    llm_model: str | None = None
    temperature: float = 0.3
    max_tokens: int = 2048
    sensitive_data: bool = False
    # Identity
    identity_mode: str = "technical_user"
    technical_user_id: int | None = None
    god_mode: bool = False
    is_supervisor: bool = False
    # Execution modes
    execution_role: str = "assistant"
    confirmation_policy: str = "sensitive"
    log_level: str = "basic"
    # Relations
    kb_document_ids: list[int] = field(default_factory=list)
    tools: list[AgentToolBinding] = field(default_factory=list)
    # Permissions
    allowed_user_ids: list[int] = field(default_factory=list)
    property_scope_ids: list[int] = field(default_factory=list)
    allowed_agent_ids: list[int] = field(default_factory=list)

    @property
    def effective_model(self) -> str | None:
        if not self.llm_account:
            return self.llm_model
        return self.llm_model or self.llm_account.default_model
