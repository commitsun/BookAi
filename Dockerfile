# =====================================================
# Dockerfile - BookAI MCP Server (HTTP)
# =====================================================

FROM python:3.11-slim

WORKDIR /app

# Dependencias del sistema
RUN apt-get update && apt-get install -y build-essential && rm -rf /var/lib/apt/lists/*

# Instalar dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código fuente
COPY . .

# Crear carpeta de logs
RUN mkdir -p data

# Exponer puerto HTTP
EXPOSE 8001

# Comando de inicio
CMD ["python", "mcp_server.py"]
