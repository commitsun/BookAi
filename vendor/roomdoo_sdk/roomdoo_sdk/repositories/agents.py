from __future__ import annotations

from ..exceptions import NotFoundError
from ..models.agent import AgentConfig, AgentToolBinding
from ..models.llm_account import LLMAccount
from ..transports.base import Transport

_AGENT_FIELDS = [
    "id",
    "name",
    "technical_name",
    "description",
    "caller_type",
    "active",
    "llm_account_id",
    "llm_model",
    "temperature",
    "max_tokens",
    "sensitive_data",
    "system_prompt",
    "context_template",
    "kb_document_ids",
    "identity_mode",
    "technical_user_id",
    "god_mode",
    "is_supervisor",
    "execution_role",
    "confirmation_policy",
    "log_level",
    "tool_binding_ids",
    "allowed_user_ids",
    "property_scope_ids",
    "allowed_agent_ids",
]

_LLM_ACCOUNT_FIELDS = [
    "id",
    "name",
    "provider",
    "api_key",
    "api_base_url",
    "default_model",
]

_BINDING_FIELDS = [
    "id",
    "agent_id",
    "tool_id",
    "description_override",
    "requires_confirm",
    "action_sensitivity_override",
    "active",
]

_TOOL_FIELDS = [
    "id",
    "name",
    "tool_type",
    "description",
    "sdk_method",
    "input_schema",
    "requires_confirm",
    "action_sensitivity",
    "endpoint_url",
    "endpoint_headers",
]


class AgentRepository:
    def __init__(self, transport: Transport):
        self._transport = transport

    async def get(self, technical_name: str) -> AgentConfig:
        records = await self._transport.search_read(
            "bookai.agent",
            [("technical_name", "=", technical_name)],
            fields=_AGENT_FIELDS,
            limit=1,
        )
        if not records:
            raise NotFoundError(
                f"Agent '{technical_name}' not found"
            )
        data = records[0]
        accounts = await self._read_llm_accounts(data)
        bindings = await self._read_tool_bindings(
            data.get("tool_binding_ids", [])
        )
        return _build_agent_config(data, accounts, bindings)

    async def list(
        self,
        active: bool = True,
        caller_type: str | None = None,
    ) -> list[AgentConfig]:
        domain: list = [("active", "=", active)]
        if caller_type:
            domain.append(
                ("caller_type", "in", [caller_type, "any"])
            )
        records = await self._transport.search_read(
            "bookai.agent", domain, fields=_AGENT_FIELDS
        )
        if not records:
            return []
        # Batch-read LLM accounts
        account_ids = list(
            {
                r["llm_account_id"][0]
                for r in records
                if r.get("llm_account_id")
            }
        )
        accounts: dict[int, LLMAccount] = {}
        if account_ids:
            acct_records = await self._transport.read(
                "bookai.llm.account",
                account_ids,
                fields=_LLM_ACCOUNT_FIELDS,
            )
            accounts = {
                r["id"]: _build_llm_account(r) for r in acct_records
            }
        # Batch-read bindings + tools
        all_binding_ids = []
        for r in records:
            all_binding_ids.extend(
                r.get("tool_binding_ids", [])
            )
        bindings_by_agent: dict[int, list[AgentToolBinding]] = {
            r["id"]: [] for r in records
        }
        if all_binding_ids:
            binding_records = await self._transport.read(
                "bookai.agent.tool.binding",
                all_binding_ids,
                fields=_BINDING_FIELDS,
            )
            tool_ids = list(
                {
                    b["tool_id"][0]
                    for b in binding_records
                    if b.get("tool_id")
                }
            )
            tools_map = {}
            if tool_ids:
                tool_records = await self._transport.read(
                    "bookai.tool", tool_ids, fields=_TOOL_FIELDS
                )
                tools_map = {r["id"]: r for r in tool_records}
            for b in binding_records:
                agent_id = b.get("agent_id")
                if isinstance(agent_id, (list, tuple)):
                    agent_id = agent_id[0]
                tool_ref = b.get("tool_id")
                if not tool_ref:
                    continue
                tid = (
                    tool_ref[0]
                    if isinstance(tool_ref, (list, tuple))
                    else tool_ref
                )
                tool_data = tools_map.get(tid)
                if not tool_data:
                    continue
                binding = _build_binding(b, tool_data)
                if agent_id in bindings_by_agent:
                    bindings_by_agent[agent_id].append(binding)

        result = []
        for r in records:
            result.append(
                _build_agent_config(
                    r,
                    accounts,
                    bindings_by_agent.get(r["id"], []),
                )
            )
        return result

    async def list_for_property(
        self, pms_property_id: int, caller_type: str
    ) -> list[AgentConfig]:
        return await self.list(
            active=True, caller_type=caller_type
        )

    async def create(
        self,
        technical_name: str,
        name: str,
        description: str,
        system_prompt: str,
        caller_type: str = "any",
        **kwargs,
    ) -> int:
        """Create a new agent. Returns agent ID."""
        vals = {
            "technical_name": technical_name,
            "name": name,
            "description": description,
            "system_prompt": system_prompt,
            "caller_type": caller_type,
        }
        vals.update(kwargs)
        return await self._transport.create(
            "bookai.agent", vals
        )

    async def update(
        self, technical_name: str, **vals
    ) -> None:
        """Update agent fields by technical_name."""
        agents = await self._transport.search_read(
            "bookai.agent",
            [("technical_name", "=", technical_name)],
            fields=["id"],
            limit=1,
        )
        if not agents:
            raise NotFoundError(
                f"Agent '{technical_name}' not found"
            )
        await self._transport.write(
            "bookai.agent", [agents[0]["id"]], vals
        )

    async def update_prompt(
        self, technical_name: str, system_prompt: str
    ) -> None:
        """Update agent system prompt."""
        await self.update(
            technical_name, system_prompt=system_prompt
        )

    async def _read_llm_accounts(
        self, data: dict
    ) -> dict[int, LLMAccount]:
        acct = data.get("llm_account_id")
        if not acct:
            return {}
        account_id = acct[0] if isinstance(acct, (list, tuple)) else acct
        records = await self._transport.read(
            "bookai.llm.account",
            [account_id],
            fields=_LLM_ACCOUNT_FIELDS,
        )
        return {r["id"]: _build_llm_account(r) for r in records}

    async def _read_tool_bindings(
        self, binding_ids: list[int]
    ) -> list[AgentToolBinding]:
        if not binding_ids:
            return []
        binding_records = await self._transport.read(
            "bookai.agent.tool.binding",
            binding_ids,
            fields=_BINDING_FIELDS,
        )
        tool_ids = list(
            {
                b["tool_id"][0]
                for b in binding_records
                if b.get("tool_id")
            }
        )
        tools_map = {}
        if tool_ids:
            tool_records = await self._transport.read(
                "bookai.tool", tool_ids, fields=_TOOL_FIELDS
            )
            tools_map = {r["id"]: r for r in tool_records}
        result = []
        for b in binding_records:
            tool_ref = b.get("tool_id")
            if not tool_ref:
                continue
            tid = (
                tool_ref[0]
                if isinstance(tool_ref, (list, tuple))
                else tool_ref
            )
            tool_data = tools_map.get(tid)
            if tool_data:
                result.append(_build_binding(b, tool_data))
        return result


def _build_llm_account(data: dict) -> LLMAccount:
    return LLMAccount(
        id=data["id"],
        name=data["name"],
        provider=data["provider"],
        api_key=data.get("api_key") or None,
        api_base_url=data.get("api_base_url") or None,
        default_model=data.get("default_model") or None,
    )


def _build_binding(
    binding: dict, tool: dict
) -> AgentToolBinding:
    desc = binding.get("description_override") or tool.get(
        "description"
    )
    confirm = binding.get("requires_confirm", False) or tool.get(
        "requires_confirm", False
    )
    # Resolve sensitivity: binding override > tool global
    sensitivity = (
        binding.get("action_sensitivity_override")
        or tool.get("action_sensitivity")
        or "none"
    )
    # Parse endpoint_headers (stored as JSON string or dict in Odoo)
    raw_headers = tool.get("endpoint_headers")
    if isinstance(raw_headers, str):
        import json
        try:
            raw_headers = json.loads(raw_headers)
        except (json.JSONDecodeError, TypeError):
            raw_headers = None
    return AgentToolBinding(
        binding_id=binding["id"],
        tool_id=tool["id"],
        tool_name=tool.get("name", ""),
        tool_type=tool.get("tool_type", "sdk"),
        description=desc or None,
        sdk_method=tool.get("sdk_method") or None,
        input_schema=tool.get("input_schema") or None,
        requires_confirm=confirm,
        action_sensitivity=sensitivity,
        active=binding.get("active", True),
        endpoint_url=tool.get("endpoint_url") or None,
        endpoint_headers=raw_headers if isinstance(raw_headers, dict) else None,
    )


def _build_agent_config(
    data: dict,
    accounts: dict[int, LLMAccount],
    tools: list[AgentToolBinding],
) -> AgentConfig:
    acct = data.get("llm_account_id")
    llm_account = None
    if acct:
        account_id = acct[0] if isinstance(acct, (list, tuple)) else acct
        llm_account = accounts.get(account_id)
    tech_user = data.get("technical_user_id")
    tech_user_id = None
    if tech_user and isinstance(tech_user, (list, tuple)):
        tech_user_id = tech_user[0]
    elif tech_user:
        tech_user_id = tech_user
    return AgentConfig(
        id=data["id"],
        name=data["name"],
        technical_name=data["technical_name"],
        description=data["description"],
        caller_type=data["caller_type"],
        active=data["active"],
        llm_account=llm_account,
        llm_model=data.get("llm_model") or None,
        temperature=data.get("temperature", 0.3),
        max_tokens=data.get("max_tokens", 2048),
        sensitive_data=data.get("sensitive_data", False),
        system_prompt=data["system_prompt"],
        context_template=data.get("context_template", ""),
        kb_document_ids=data.get("kb_document_ids", []),
        identity_mode=data.get("identity_mode", "technical_user"),
        technical_user_id=tech_user_id,
        god_mode=data.get("god_mode", False),
        is_supervisor=data.get("is_supervisor", False),
        execution_role=data.get(
            "execution_role", "assistant"
        ),
        confirmation_policy=data.get(
            "confirmation_policy", "sensitive"
        ),
        log_level=data.get("log_level", "basic"),
        tools=tools,
        allowed_user_ids=data.get("allowed_user_ids", []),
        property_scope_ids=data.get("property_scope_ids", []),
        allowed_agent_ids=data.get("allowed_agent_ids", []),
    )
