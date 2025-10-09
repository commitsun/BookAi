import os
import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

def register_routes(app):
    @app.post("/telegram/webhook")
    async def telegram_webhook(request: Request):
        try:
            data = await request.json()
            msg = (
                data.get("message") or
                data.get("edited_message") or
                {}
            )

            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = (msg.get("text") or "").strip()

            ENCARGADO_CHAT_ID = os.getenv("TELEGRAM_ENCARGADO_CHAT_ID")
            if not ENCARGADO_CHAT_ID or chat_id != str(ENCARGADO_CHAT_ID):
                logging.warning("⚠️ Mensaje desde chat no autorizado.")
                return JSONResponse({"status": "ignored"})

            # =============================================
            # 1️⃣ Si el texto tiene formato RESPUESTA ID:
            #    sigue funcionando igual (modo manual)
            # =============================================
            import re
            m = re.match(r"^\s*RESPUESTA\s+(\d+)\s*:(.+)$", text, flags=re.IGNORECASE | re.DOTALL)
            if m:
                conversation_id = m.group(1).strip()
                raw_text = m.group(2).strip()
                from main import resolve_from_encargado
                await resolve_from_encargado(conversation_id, raw_text)
                return JSONResponse({"status": "ok"})

            # =============================================
            # 2️⃣ Modo automático:
            #    Si hay conversaciones pendientes, toma la más reciente
            # =============================================
            from main import pending_escalations, resolve_from_encargado

            if not pending_escalations:
                logging.info("ℹ️ No hay conversaciones pendientes. Ignorando mensaje normal.")
                return JSONResponse({"status": "no_pending"})

            # Buscar la conversación pendiente más reciente
            sorted_pending = sorted(
                pending_escalations.items(),
                key=lambda x: x[1]["ts"],
                reverse=True
            )
            latest_id, latest_data = sorted_pending[0]
            logging.info(f"📨 Asociando respuesta automática con cliente {latest_id}")

            # Resolver y reenviar
            await resolve_from_encargado(latest_id, text)

            return JSONResponse({"status": "ok_auto", "conversation_id": latest_id})

        except Exception as e:
            logging.error(f"❌ Error en /telegram/webhook: {e}", exc_info=True)
            return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)
