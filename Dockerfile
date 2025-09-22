FROM python:3.11-slim

# Establecer directorio de trabajo
WORKDIR /app

# Copiar requirements e instalar dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 👇 Parche: silenciar logs de arranque de FastMCP
RUN sed -i 's/logger.info(f"Starting MCP server /# logger.info(f"Starting MCP server /' \
    /usr/local/lib/python3.11/site-packages/fastmcp/server/server.py

# 👇 Asegurar que Python vea /app como raíz de imports
ENV PYTHONPATH=/app

# Copiar todo el proyecto
COPY . .

# Comando por defecto
CMD ["python", "chat_cli.py"]
