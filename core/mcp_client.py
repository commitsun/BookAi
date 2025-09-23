import os
from langchain_mcp_adapters.client import MultiServerMCPClient

# =========
# Recuperar API Key
# =========
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise RuntimeError("❌ Falta la variable OPENAI_API_KEY en el entorno.")

# =========
# Definición de conexiones MCP
# =========
mcp_connections = {
    "InfoAgent": {
        "command": "python",
        "args": ["-m", "agents.info_agent"],
        "transport": "stdio",
        "env": {"OPENAI_API_KEY": api_key},
    },
    "DispoPreciosAgent": {
        "transport": "streamable_http",
        "url": "https://n8n-n8n.d6aq21.easypanel.host/mcp/cbc40f16-8756-40b5-ab72-32912227282f",
        
    },
    "InternoAgent": {
        "command": "python",
        "args": ["-m", "agents.interno_agent"],
        "transport": "stdio",
        "env": {"OPENAI_API_KEY": api_key},
    },
    "KnowledgeBase": {  # 👈 integrado desde tools
        "command": "python",
        "args": ["-m", "tools.knowledge_base"],
        "transport": "stdio",
        "env": {"OPENAI_API_KEY": api_key},
    },
}

# =========
# Cliente MCP multi-servidor
# =========
mcp_client = MultiServerMCPClient(mcp_connections)
