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

import os
import logging
import aiohttp
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from core.roomdoo_token import get_roomdoo_token

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
        log.info("🟢 Nueva consulta de disponibilidad y precios")
        log.info(f"   📅 Check-in: {input_data.checkin}")
        log.info(f"   📅 Check-out: {input_data.checkout}")
        log.info(f"   👥 Ocupación: {input_data.occupancy}")
        log.info(f"   🏨 Property ID: {input_data.pmsPropertyId}")

        # 1️⃣ Obtener token desde Supabase
        token = await get_roomdoo_token()
        log.info(f"   🔐 Token obtenido (primeros 15 chars): {token[:15]}...")

        # 2️⃣ Construir URL
        base_url = os.getenv("ROOMDOO_AVAIL_URL")
        if not base_url:
            raise HTTPException(status_code=500, detail="ROOMDOO_AVAIL_URL no configurado en .env")

        url = (
            f"{base_url}?pmsPropertyId={input_data.pmsPropertyId}"
            f"&checkin={input_data.checkin}"
            f"&checkout={input_data.checkout}"
            f"&occupancy={input_data.occupancy}"
        )

        log.info(f"   🌍 Llamando a Roomdoo: {url}")

        headers = {"Authorization": f"Bearer {token}"}

        # 3️⃣ Llamada HTTP a Roomdoo
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=30) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    log.error(f"❌ Roomdoo devolvió {resp.status}: {text}")
                    raise HTTPException(status_code=resp.status, detail=text)

                data = await resp.json()

        # 4️⃣ Log de resultado
        log.info(f"✅ [Roomdoo] Respuesta recibida ({len(data)} items)")
        for r in data:
            log.info(
                f"   🛏 {r.get('roomTypeName', 'Habitación')} | "
                f"Disponibles: {r.get('avail')} | "
                f"💶 Precio: {r.get('price')} €"
            )

        # 5️⃣ Respuesta igual que n8n
        return {"success": True, "response": data, "results_count": len(data)}

    except Exception as e:
        log.error(f"💥 Error en availability_pricing_tool: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
