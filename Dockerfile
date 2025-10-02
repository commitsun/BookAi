FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN sed -i 's/logger.info(f"Starting MCP server /# logger.info(f"Starting MCP server /' /usr/local/lib/python3.11/site-packages/fastmcp/server/server.py

ENV PYTHONPATH=/app

COPY . .

RUN apt-get update && apt-get install -y python3-dotenv && rm -rf /var/lib/apt/lists/*
ENV DOTENV_PATH=/app/.env

CMD ["python", "chat_cli.py"]
