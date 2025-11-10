# core/logging.py

import json
import os
from datetime import datetime
from fastapi import Request

AUDIT_DIR = "data"
AUDIT_LOG_FILE = os.path.join(AUDIT_DIR, "audit.log")

# Asegurar que el directorio existe
os.makedirs(AUDIT_DIR, exist_ok=True)

async def audit_middleware(request: Request, call_next):
    """Middleware de auditoría muy simple: logea cada request en data/audit.log."""
    start_time = datetime.now().isoformat()
    response = await call_next(request)

    log_entry = {
        "timestamp": start_time,
        "method": request.method,
        "path": request.url.path,
        "client_ip": request.client.host,
        "status_code": response.status_code,
    }

    try:
        with open(AUDIT_LOG_FILE, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
    except Exception:
        # No romper la API por un fallo al escribir logs
        pass

    return response
