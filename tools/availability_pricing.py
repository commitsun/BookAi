# =====================================================
# tools/availability_pricing.py
# =====================================================
import logging
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from core.roomdoo_token import get_roomdoo_token
from core.config import os

router = APIRouter()
log = logging.getLogger("availability_pricing")


class AvailabilityPricingInput(BaseModel):
    checkin: str
    checkout: str
    occupancy: int
    pmsPropertyId: int = Field(default=int(os.getenv("ROOMDOO_PMS_PROPERTY_ID", 38)))


@router.post("/availability_pricing")
async def availability_pricing_tool(input_data: AvailabilityPricingInput):
    """Consulta disponibilidad y precios en Roomdoo."""
    try:
        # 👇 Eliminar el await (la función no es async)
        token = get_roomdoo_token()

        base_url = os.getenv("ROOMDOO_AVAIL_URL")
        if not base_url:
            raise HTTPException(status_code=500, detail="ROOMDOO_AVAIL_URL no configurado")

        url = (
            f"{base_url}"
            f"?pmsPropertyId={input_data.pmsPropertyId}"
            f"&checkin={input_data.checkin}"
            f"&checkout={input_data.checkout}"
            f"&occupancy={input_data.occupancy}"
        )

        headers = {"Authorization": f"Bearer {token}"}
        log.info(f"🔍 Consultando disponibilidad: {url}")

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()

            # Soportar ambos formatos de respuesta
            if isinstance(data, dict):
                items = data.get("response", data)
            elif isinstance(data, list):
                items = data
            else:
                items = []

            return {
                "success": True,
                "data": items,
                "results_count": len(items),
            }

    except httpx.HTTPStatusError as e:
        log.error(f"❌ Error HTTP {e.response.status_code} en Roomdoo: {e}")
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
    except Exception as e:
        log.error(f"❌ Error general en availability_pricing: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
