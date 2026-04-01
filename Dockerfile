FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]"

ENV PYTHONPATH=/app
COPY . .

EXPOSE 8000
CMD ["uvicorn", "main:socket_app", "--host", "0.0.0.0", "--port", "8000"]
