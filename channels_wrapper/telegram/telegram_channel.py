import logging
import os
import requests
from fastapi import Request
from fastapi.responses import JSONResponse
from channels_wrapper.base_channel import BaseChannel
from channels_wrapper.utils.text_utils import send_fragmented_async
from core.escalation_manager import resolve_from_encargado, pending_escalations
from core.notification import notify_encargado

log = logging.getLogger("telegram")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


class TelegramChannel(BaseChannel):
    """Canal Telegram: encargado ↔ huésped (reenvío automático y fragmentación)."""

    def send_message(self, user_id: str, text: str):
        """Envía mensaje al encargado por Telegram."""
        if not TELEGRAM_BOT_TOKEN or not user_id:
            log.error("❌ Falta TELEGRAM_BOT_TOKEN o user_id.")
            return

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": user_id, "text": text, "parse_mode": "Markdown"}

        try:
            r = requests.post(url, json=data, timeout=10)
            if r.status_code != 200:
                log.error(f"⚠️ Telegram API error ({r.status_code}): {r.text}")
            else:
                log.info(f"📤 Telegram → {user_id}: {text[:60]}...")
        except Exception as e:
            log.error(f"💥 Error enviando Telegram: {e}", exc_info=True)

    def extract_message_data(self, payload: dict):
        """No se usa en Telegram."""
        return None, None, None, None

    # ============================================================
    # 🚀 WEBHOOK PRINCIPAL
    # ============================================================
    def register_routes(self, app):
        @app.post("/telegram/webhook")
        async def telegram_webhook(request: Request):
            """
            Webhook para manejar las respuestas del encargado.
            Admite formato:
            - RESPUESTA <id>: <mensaje>
            - Respuesta directa si hay una sola conversación pendiente.
            """
            try:
                data = await request.json()
                message = data.get("message", {})
                chat_id = message.get("chat", {}).get("id")
                text = (message.get("text") or "").strip()

                if not text:
                    return JSONResponse({"ok": True})

                log.info(f"💬 Telegram (encargado {chat_id}): {text}")

                # =====================================================
                # 🧩 Caso 1: Formato RESPUESTA <id>: <texto>
                # =====================================================
                if text.lower().startswith("respuesta "):
                    try:
                        content = text.split(" ", 1)[1]
                        target_id, respuesta = content.split(":", 1)
                        target_id, respuesta = target_id.strip(), respuesta.strip()

                        # 🔥 Reenviar al huésped con fragmentación
                        await resolve_from_encargado(target_id, respuesta, None)
                        await notify_encargado(f"✅ Respuesta enviada al cliente `{target_id}`.")
                        return JSONResponse({"ok": True})
                    except Exception as e:
                        log.error(f"❌ Error formato RESPUESTA: {e}", exc_info=True)
                        await notify_encargado(
                            "⚠️ Formato incorrecto. Usa:\n\nRESPUESTA <id>: <mensaje>"
                        )
                        return JSONResponse({"ok": False})

                # =====================================================
                # 🧩 Caso 2: Solo hay una conversación pendiente
                # =====================================================
                if len(pending_escalations) == 1:
                    target_id = next(iter(pending_escalations.keys()))
                    respuesta = text.strip()
                    log.info(f"📨 Respuesta directa → {target_id}: {respuesta}")
                    await resolve_from_encargado(target_id, respuesta, None)
                    await notify_encargado(
                        f"✅ Respuesta automática enviada al cliente `{target_id}`."
                    )
                    return JSONResponse({"ok": True})

                # =====================================================
                # 🧩 Caso 3: Varias conversaciones pendientes
                # =====================================================
                elif len(pending_escalations) > 1:
                    ids = "\n".join(f"• `{cid}`" for cid in pending_escalations.keys())
                    msg = (
                        "⚠️ Hay *varias* conversaciones pendientes.\n"
                        "Usa el formato:\n\n"
                        "`RESPUESTA <id>: <mensaje>`\n\n"
                        f"Clientes:\n{ids}"
                    )
                    await notify_encargado(msg)
                    return JSONResponse({"ok": True})

                # =====================================================
                # 🧩 Caso 4: No hay conversaciones pendientes
                # =====================================================
                else:
                    await notify_encargado("ℹ️ No hay conversaciones pendientes ahora mismo.")
                    return JSONResponse({"ok": True})

            except Exception as e:
                log.error(f"💥 Error en Telegram webhook: {e}", exc_info=True)
                return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
