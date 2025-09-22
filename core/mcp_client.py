import os
from langchain_mcp_adapters.client import MultiServerMCPClient

# 👇 Hacemos que cada agente herede el entorno actual del contenedor
shared_env = dict(os.environ)

mcp_connections = {
    "InfoAgent": {
        "command": "python",
        "args": ["-m", "agents.info_agent"],
        "transport": "stdio",
        "env": shared_env,
    },
    "DispoPreciosAgent": {
        "command": "python",
        "args": ["-m", "agents.dispo_precios_agent"],
        "transport": "stdio",
        "env": shared_env,
    },
    "InternoAgent": {
        "command": "python",
        "args": ["-m", "agents.interno_agent"],
        "transport": "stdio",
        "env": shared_env,
    },
}

mcp_client = MultiServerMCPClient(mcp_connections)
