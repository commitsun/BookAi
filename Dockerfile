FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 👇 Parche: silenciar logs de arranque de FastMCP
RUN sed -i 's/logger.info(f"Starting MCP server /# logger.info(f"Starting MCP server /' /usr/local/lib/python3.11/site-packages/fastmcp/server/server.py

COPY . .

CMD ["python", "chat_cli.py"]
