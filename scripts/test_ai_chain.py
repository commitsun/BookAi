"""
End-to-end test of the AI chain: SDK → AgentLoader → ContextBuilder → LLM.

Usage:
    python scripts/test_ai_chain.py

Requires:
    - Odoo running at http://devel.odoo:16069 with agent "test" configured
    - LLM account in Odoo with a valid API key and model
"""

import asyncio
import sys

from roomdoo_sdk import RoomdooClient
from roomdoo_sdk.transports.jsonrpc import JsonRpcTransport

from app.services.agent_loader import AgentLoader
from app.services.context_builder import build_prompt
from app.services.llm_litellm import LiteLLMProvider
from app.services.llm_client import LLMClientError

ODOO_URL = "http://devel.odoo:16069"
ODOO_DB = "devel"
ODOO_USER = "admin"
ODOO_PASS = "admin"

TEST_MESSAGE = "¿A qué hora es el check-in?"
AGENT_NAME = "test"


async def main():
    print("=" * 60)
    print("BookAI AI Chain — End-to-End Test")
    print("=" * 60)

    # 1. Connect to Odoo via SDK
    print("\n[1/5] Connecting to Odoo...")
    transport = JsonRpcTransport(
        url=ODOO_URL, db=ODOO_DB,
        username=ODOO_USER, password=ODOO_PASS,
    )
    client = RoomdooClient(transport=transport)
    print(f"  OK — connected to {ODOO_URL}")

    # 2. Load agents
    print("\n[2/5] Loading agents via AgentLoader...")
    loader = AgentLoader(client)
    await loader.load_all()
    cached = loader.get(AGENT_NAME)
    if not cached:
        print(f"  FAIL — agent '{AGENT_NAME}' not found in cache")
        await transport.close()
        sys.exit(1)

    agent = cached.config
    docs = cached.documents
    print(f"  OK — agent: {agent.name} ({agent.technical_name})")
    print(f"       provider: {agent.llm_account.provider}")
    print(f"       model: {agent.effective_model or '(not set)'}")
    print(f"       KB docs: {len(docs)}")
    print(f"       api_key: {'***' + agent.llm_account.api_key[-4:] if agent.llm_account.api_key else 'EMPTY'}")

    if not agent.llm_account.api_key:
        print("\n  ⚠ No API key in LLM account!")
        print("  Go to Odoo → BooKAI → Cuentas LLM → edit 'test'")
        print("  Set: API Key = sk-..., Default Model = gpt-4o-mini")
        await transport.close()
        sys.exit(1)

    if not agent.effective_model:
        print("\n  ⚠ No model configured!")
        print("  Set Default Model in the LLM account (e.g. gpt-4o-mini)")
        await transport.close()
        sys.exit(1)

    # 3. Build prompt
    print(f"\n[3/5] Building prompt for: \"{TEST_MESSAGE}\"")
    messages = build_prompt(
        agent=agent,
        docs=docs,
        conversation_history=[],
        current_message=TEST_MESSAGE,
        property_name="Hotel Costa Brava",
    )
    print(f"  OK — {len(messages)} messages in prompt:")
    for m in messages:
        preview = m.content[:80].replace('\n', ' ')
        print(f"       [{m.role}] {preview}...")

    # 4. Call LLM
    print(f"\n[4/5] Calling LLM ({agent.llm_account.provider}/{agent.effective_model})...")
    llm = LiteLLMProvider()
    llm_messages = [{"role": m.role, "content": m.content} for m in messages]

    try:
        response = await llm.chat(
            messages=llm_messages,
            provider=agent.llm_account.provider,
            api_key=agent.llm_account.api_key,
            model=agent.effective_model,
            api_base_url=agent.llm_account.api_base_url,
            temperature=agent.temperature,
            max_tokens=agent.max_tokens,
        )
    except LLMClientError as e:
        print(f"  FAIL — {e}")
        await transport.close()
        sys.exit(1)

    print(f"  OK — model: {response.model}")
    print(f"       tokens: {response.tokens_in} in / {response.tokens_out} out")

    # 5. Show response
    print(f"\n[5/5] AI Response:")
    print("-" * 60)
    print(response.content)
    print("-" * 60)

    await transport.close()
    print("\n✅ Full chain works: Odoo → SDK → AgentLoader → ContextBuilder → LLM")


if __name__ == "__main__":
    asyncio.run(main())
