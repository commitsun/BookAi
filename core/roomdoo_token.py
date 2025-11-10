# =====================================================
# core/roomdoo_token.py
# =====================================================
"""
Gestión del token Roomdoo.
Lee el token activo desde Supabase (tabla: tokens_roomdoo).
"""

import logging
from core.config import supabase_client

log = logging.getLogger("roomdoo_token")


def get_roomdoo_token() -> str:
    """
    Obtiene el valor más reciente del token Roomdoo desde Supabase.
    Retorna el campo 'key' asociado al 'token' = 'roomdoo_token'.
    """
    try:
        response = supabase_client.table("tokens_roomdoo") \
            .select("key") \
            .eq("token", "roomdoo_token") \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()

        data = response.data
        if not data:
            raise ValueError("❌ No se encontró ningún token Roomdoo en Supabase")

        token = data[0]["key"]
        log.info("🔐 Token Roomdoo obtenido correctamente desde Supabase.")
        return token

    except Exception as e:
        log.error(f"Error al obtener token Roomdoo: {e}", exc_info=True)
        raise
