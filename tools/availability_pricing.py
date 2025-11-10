# =====================================================
# tools/availability_pricing.py
# =====================================================
"""
Tool: Disponibilidad y Precios
------------------------------
Consulta la disponibilidad de habitaciones y precios
para un hotel en Roomdoo, igual que en n8n.

1️⃣ Obtiene el token actual desde Supabase
2️⃣ Llama a la API de Roomdoo con los parámetros recibidos
3️⃣ Devuelve los resultados en el mismo formato que n8n
"""

import logging
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from core.roomdoo_token import get_roomdoo_token
from core.config import os

router = APIRouter()
log = logging.getLogger("availability_pricing")


# =====================================================
# 📥 INPUT MODEL
# =====================================================
class AvailabilityPricingInput(BaseModel):
    checkin: str = Field(..., description="Fecha de entrada (ISO 8601)")
    checkout: str = Field(..., description="Fecha de salida (ISO 8601)")
    occupancy: int = Field(..., description="Número de huéspedes")
    pmsPropertyId: int = Field(default=int(os.getenv("ROOMDOO_PMS_PROPERTY_ID", 38)),
                               description="ID del hotel (Roomdoo)")

# =====================================================
# 🔌 ENDPOINT PRINCIPAL
# =====================================================
@router.post("/availability_pricing")
async def availability_pricing_tool(input_data: AvailabilityPricingInput):
    """
    Replica la tool de n8n:
      - Input: checkin, checkout, occupancy
      - Output: lista de habitaciones disponibles y precios
    """
    try:
        # 1️⃣ Obtener token desde Supabase
        token = get_roomdoo_token()

        # 2️⃣ Construir URL
        base_url = os.getenv("ROOMDOO_AVAIL_URL")
        if not base_url:
            raise HTTPException(status_code=500, detail="ROOMDOO_AVAIL_URL no configurado en .env")

        url = (
            f"{base_url}"
            f"?pmsPropertyId={input_data.pmsPropertyId}"
            f"&checkin={input_data.checkin}"
            f"&checkout={input_data.checkout}"
            f"&occupancy={input_data.occupancy}"
        )

        headers = {"Authorization": f"Bearer {token}"}

        log.info(f"🔍 Consultando disponibilidad: {url}")

        # 3️⃣ Llamada HTTP a Roomdoo
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()

        # 4️⃣ Respuesta formateada igual que n8n
        return {
            "success": True,
            "data": data.get("response", data),
            "results_count": len(data.get("response", data)),
        }

    except httpx.HTTPStatusError as e:
        log.error(f"❌ Error HTTP {e.response.status_code} en Roomdoo: {e}")
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)

    except Exception as e:
        log.error(f"❌ Error general en availability_pricing: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
