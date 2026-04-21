FROM python:3.11-slim

RUN apt-get update && apt-get install -y curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]"

# roomdoo-sdk: in dev it's mounted as a volume at /roomdoo-sdk;
# for production, copy it into the image before this stage.
RUN if [ -d /roomdoo-sdk ]; then pip install --no-cache-dir /roomdoo-sdk; fi

ENV PYTHONPATH=/app
COPY . .

EXPOSE 8000
CMD ["uvicorn", "main:socket_app", "--host", "0.0.0.0", "--port", "8000"]
