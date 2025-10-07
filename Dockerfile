FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Corrige bug conocido de FastMCP (log)
RUN sed -i 's/logger.info(f"Starting MCP server /# logger.info(f"Starting MCP server /' /usr/local/lib/python3.11/site-packages/fastmcp/server/server.py

ENV PYTHONPATH=/app
COPY . .

ENV DOTENV_PATH=/app/.env

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
