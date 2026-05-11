FROM python:3.11-slim

RUN apt-get update && apt-get install -y curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]"

# roomdoo-sdk lives vendored at vendor/roomdoo_sdk until it moves to its own repo.
# Editable install so the bind mount in docker-compose.yml picks up local edits.
COPY vendor/roomdoo_sdk ./vendor/roomdoo_sdk
RUN pip install --no-cache-dir -e ./vendor/roomdoo_sdk

ENV PYTHONPATH=/app
COPY . .

EXPOSE 8000
COPY entrypoint.sh .
ENTRYPOINT ["./entrypoint.sh"]
