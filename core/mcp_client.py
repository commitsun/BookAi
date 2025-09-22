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
        "command": "python",
        "args": ["-m", "agents.dispo_precios_agent"],
        "transport": "stdio",
        "env": {"OPENAI_API_KEY": api_key},
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
