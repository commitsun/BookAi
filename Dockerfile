FROM python:3.11-slim

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
