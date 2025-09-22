from langchain_mcp_adapters.client import MultiServerMCPClient

mcp_connections = {
    "InfoAgent": {
        "command": "python",
        "args": ["-m", "agents.info_agent"],
        "transport": "stdio"
    },
    "DispoPreciosAgent": {
        "command": "python",
        "args": ["-m", "agents.dispo_precios_agent"],
        "transport": "stdio"
    },
    "InternoAgent": {
        "command": "python",
        "args": ["-m", "agents.interno_agent"],
        "transport": "stdio"
    }
}

mcp_client = MultiServerMCPClient(mcp_connections)
