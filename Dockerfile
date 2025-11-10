# Dockerfile - BOOKAI MCP Server

FROM python:3.11-slim

WORKDIR /app

# Instalar dependencias del sistema (por si alguna lib las necesita)
RUN apt-get update && apt-get install -y build-essential && rm -rf /var/lib/apt/lists/*

# Copiar requirements e instalarlos
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el resto del código
COPY . .

# Crear directorio de logs (por si acaso)
RUN mkdir -p data

# Exponer puerto del MCP server
EXPOSE 8001

# Comando por defecto: lanzar el servidor FastAPI con uvicorn
CMD ["uvicorn", "mcp_server:app", "--host", "0.0.0.0", "--port", "8001"]
