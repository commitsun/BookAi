import asyncio
import logging
import requests
from typing import Tuple, Optional, Iterable, Any

from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse

from core.config import Settings as C
from core.message_buffer import MessageBufferManager
from channels_wrapper.base_channel import BaseChannel
from channels_wrapper.utils.media_utils import transcribe_audio
from channels_wrapper.utils.text_utils import send_fragmented_async

# ✅ Nuevo sistema de escalaciones (v4)
from agents.interno_agent import InternoAgent

log = logging.getLogger("whatsapp")

# 🕒 Tiempo de espera sin nuevos mensajes antes de procesar el lote
BUFFER_WAIT_SECONDS = 8


class WhatsAppChannel(BaseChannel):
    """
    Canal WhatsApp (Meta Graph API) con integración a InternoAgent v4.
      - Agrupa mensajes consecutivos del mismo usuario.
      - Envía el bloque tras un periodo de inactividad.
      - Si la IA detecta una falta de información o necesidad de confirmación,
        se activa la escalación automática con el agente interno ReAct.
    """

    def __init__(self, openai_api_key: str = None):
        super().__init__(openai_api_key=openai_api_key or C.OPENAI_API_KEY)
        self.buffer_manager = MessageBufferManager(idle_seconds=BUFFER_WAIT_SECONDS)
        self._processed_ids: set[str] = set()
        self.interno_agent = InternoAgent()  # ✅ Inicialización del nuevo agente
        log.info("✅ WhatsAppChannel inicializado con InternoAgent v4 y MessageBufferManager")

    # ---------------------------------------------------------------------
    # Webhooks
    # ---------------------------------------------------------------------
    def register_routes(self, app):
        @app.get("/webhook")
        @app.get("/webhook/whatsapp")
        async def verify_webhook(request: Request):
            params = request.query_params
            if (
                params.get("hub.mode") == "subscribe"
                and params.get("hub.verify_token") == C.WHATSAPP_VERIFY_TOKEN
            ):
                log.info("✅ Webhook WhatsApp verificado.")
                return PlainTextResponse(params.get("hub.challenge"), status_code=200)
            return PlainTextResponse("Error de verificación", status_code=403)

        @app.post("/webhook")
        @app.post("/webhook/whatsapp")
        async def whatsapp_webhook(request: Request):
            """Recibe eventos de Meta y delega el procesamiento en background."""
            try:
                data = await request.json()
                asyncio.create_task(self._process_in_background(data))
                return JSONResponse({"status": "ok"})
            except Exception as e:
                log.error(f"❌ Error procesando webhook: {e}", exc_info=True)
                return JSONResponse({"status": "error", "detail": str(e)})

    # ---------------------------------------------------------------------
    # Procesamiento de mensajes entrantes
    # ---------------------------------------------------------------------
    async def _process_in_background(self, data: dict):
        try:
            user_id, msg_id, msg_type, user_message = self.extract_message_data(data)
            if not user_id or not msg_id or not user_message:
                return

            # Evitar reprocesos por reintentos del webhook
            if msg_id in self._processed_ids:
                log.debug(f"↩️ Duplicado ignorado (msg_id={msg_id})")
                return
            self._processed_ids.add(msg_id)

            cid = str(user_id).replace("+", "").strip()
            log.info(f"📥 Mensaje recibido ({cid}, tipo={msg_type}): {user_message[:80]}")

            # Registrar mensaje en historial
            self._append(cid, "user", user_message)

            # Callback que procesará el bloque tras el timeout de inactividad
            async def process_callback(conversation_id: str, combined_text: str, version: int):
                await self._process_block(conversation_id, combined_text)

            # Agregar al buffer (MessageBufferManager se encarga del debounce)
            await self.buffer_manager.add_message(cid, user_message, process_callback)

        except Exception as e:
            log.error(f"💥 Error en _process_in_background: {e}", exc_info=True)

    # ---------------------------------------------------------------------
    # Procesamiento principal del bloque
    # ---------------------------------------------------------------------
    async def _process_block(self, cid: str, user_block: str):
        """Procesa el bloque completo acumulado tras el tiempo de inactividad."""
        try:
            if not self.agent:
                log.error("❌ No hay agente asignado al canal.")
                return

            log.info(f"🤖 Procesando bloque ({cid}): {user_block[:150]}...")
            response = await self.agent.ainvoke(
                user_input=user_block,
                chat_id=cid,
                hotel_name="Hotel",
                chat_history=self.conversations.get(cid, []),
            )

            if not response or not response.strip():
                log.warning(f"⚠️ Respuesta vacía para {cid}")
                return

            # Enviar respuesta fragmentada (si es muy larga)
            await send_fragmented_async(self.send_message, cid, response)
            self._append(cid, "assistant", response)
            log.info(f"📩 Respuesta enviada a {cid}: {response[:120]}")

            # ===========================================================
            # 🆕 Nueva lógica: detección de escalación (InternoAgent v4)
            # ===========================================================
            if any(
                p in response.lower()
                for p in ["encargado", "consultarlo", "permíteme contactar", "no dispongo"]
            ):
                log.info(f"🚨 Escalación detectada automáticamente para {cid}")
                asyncio.create_task(
                    self.interno_agent.escalate(
                        guest_chat_id=cid,
                        guest_message=user_block,
                        escalation_type="info_not_found",
                        reason="El agente indica falta de información",
                        context=f"Detección automática desde WhatsApp para: {user_block[:100]}",
                    )
                )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"Error procesando bloque: {e}", exc_info=True)

    # ---------------------------------------------------------------------
    # Envío de mensajes a WhatsApp (Meta Graph API)
    # ---------------------------------------------------------------------
    def send_message(self, user_id: str, text: str):
        phone_id = getattr(self, "_dynamic_whatsapp_phone_id", None) or C.WHATSAPP_PHONE_ID
        token = getattr(self, "_dynamic_whatsapp_token", None) or C.WHATSAPP_TOKEN
        if not phone_id or not token:
            log.error("❌ Faltan credenciales de WhatsApp para enviar mensaje.")
            return {
                "ok": False,
                "provider": "meta_whatsapp",
                "message_type": "text",
                "recipient_id": user_id,
                "delivery_status": "error",
                "raw_error": "missing_whatsapp_credentials",
            }
        url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": user_id,
            "type": "text",
            "text": {"body": text},
        }
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=10)
            if r.status_code != 200:
                log.error(f"⚠️ Error WhatsApp ({r.status_code}): {r.text}")
                return {
                    "ok": False,
                    "provider": "meta_whatsapp",
                    "message_type": "text",
                    "recipient_id": user_id,
                    "status_code": r.status_code,
                    "delivery_status": "error",
                    "raw_error": r.text,
                }
            else:
                response_payload = {}
                try:
                    response_payload = r.json() or {}
                except Exception:
                    response_payload = {}
                wamid = str(
                    ((response_payload.get("messages") or [{}])[0] or {}).get("id") or ""
                ).strip() or None
                log.info(f"🚀 WhatsApp → {user_id}: {text[:80]}... ({r.status_code})")
                return {
                    "ok": True,
                    "provider": "meta_whatsapp",
                    "message_type": "text",
                    "recipient_id": user_id,
                    "status_code": r.status_code,
                    "delivery_status": "pending",
                    "wamid": wamid,
                }
        except Exception as e:
            log.error(f"⚠️ Error enviando mensaje WhatsApp: {e}", exc_info=True)
            return {
                "ok": False,
                "provider": "meta_whatsapp",
                "message_type": "text",
                "recipient_id": user_id,
                "delivery_status": "error",
                "raw_error": str(e),
            }

    # ---------------------------------------------------------------------
    # Envío de plantillas (WhatsApp)
    # ---------------------------------------------------------------------
    def send_template_message(
        self,
        user_id: str,
        template_id: str,
        parameters: dict | list | tuple | None = None,
        *,
        language: str = "es",
    ) -> bool:
        """
        Envía una plantilla preaprobada usando la API de WhatsApp Cloud.
        Soporta parámetros opcionales en orden de aparición.
        """
        phone_id = getattr(self, "_dynamic_whatsapp_phone_id", None) or C.WHATSAPP_PHONE_ID
        token = getattr(self, "_dynamic_whatsapp_token", None) or C.WHATSAPP_TOKEN
        if not token or not phone_id:
            log.error("❌ Faltan credenciales de WhatsApp para enviar plantillas.")
            return {
                "ok": False,
                "provider": "meta_whatsapp",
                "message_type": "template",
                "template_name": template_id,
                "recipient_id": user_id,
                "delivery_status": "error",
                "raw_error": "missing_whatsapp_credentials",
            }

        def _iter_params(params: dict | list | tuple | None) -> Iterable[Any]:
            if params is None:
                return []
            if isinstance(params, dict):
                if "body" in params:
                    return _iter_params(params.get("body"))
                return params.values()
            if isinstance(params, (list, tuple)):
                return params
            return [params]

        def _normalize_param(val: Any) -> dict | None:
            """
            Normaliza parámetros para Meta evitando enviar parameter_name vacío
            (causa error #100 en la API).
            """
            if val is None:
                return None
            if not isinstance(val, dict):
                return {"type": "text", "text": str(val)}

            pname = (val.get("parameter_name") or "").strip()
            ptype = val.get("type") or "text"
            ptext = "" if val.get("text") is None else str(val.get("text"))

            if pname:
                return {"type": ptype, "parameter_name": pname, "text": ptext}

            # Sin nombre: mandamos como ordinal simple para evitar rechazo.
            if "text" in val:
                return {"type": ptype, "text": ptext}
            return {"type": "text", "text": str(val)}

        body_params = []
        for val in _iter_params(parameters):
            norm = _normalize_param(val)
            if norm:
                body_params.append(norm)
        components = [{"type": "body", "parameters": body_params}] if body_params else []

        if isinstance(parameters, dict):
            raw_buttons = parameters.get("buttons")
            if isinstance(raw_buttons, list):
                for btn in raw_buttons:
                    if not isinstance(btn, dict):
                        continue
                    raw_value = btn.get("text")
                    if raw_value is None:
                        raw_value = btn.get("value")
                    if raw_value is None:
                        continue
                    idx = btn.get("index", 0)
                    sub_type = str(btn.get("sub_type") or "url").strip().lower() or "url"
                    components.append(
                        {
                            "type": "button",
                            "sub_type": sub_type,
                            "index": str(idx),
                            "parameters": [
                                {
                                    "type": "text",
                                    "text": str(raw_value),
                                }
                            ],
                        }
                    )

        payload = {
            "messaging_product": "whatsapp",
            "to": user_id,
            "type": "template",
            "template": {
                "name": template_id,
                "language": {"code": language or "es"},
                "components": components,
            },
        }

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        try:
            r = requests.post(
                f"https://graph.facebook.com/v19.0/{phone_id}/messages",
                headers=headers,
                json=payload,
                timeout=10,
            )
            if r.status_code != 200:
                log.error(
                    "⚠️ Error WhatsApp (template %s → %s): status=%s body=%s payload=%s",
                    template_id,
                    user_id,
                    r.status_code,
                    r.text,
                    payload,
                )
                return {
                    "ok": False,
                    "provider": "meta_whatsapp",
                    "message_type": "template",
                    "template_name": template_id,
                    "recipient_id": user_id,
                    "status_code": r.status_code,
                    "delivery_status": "error",
                    "raw_error": r.text,
                }

            response_payload = {}
            try:
                response_payload = r.json() or {}
            except Exception:
                response_payload = {}
            wamid = str(
                ((response_payload.get("messages") or [{}])[0] or {}).get("id") or ""
            ).strip() or None
            log.info(
                "🚀 WhatsApp (plantilla) → %s: %s (%s) params=%s",
                user_id,
                template_id,
                r.status_code,
                payload.get("template", {}).get("components"),
            )
            return {
                "ok": True,
                "provider": "meta_whatsapp",
                "message_type": "template",
                "template_name": template_id,
                "recipient_id": user_id,
                "status_code": r.status_code,
                "delivery_status": "pending",
                "wamid": wamid,
            }
        except Exception as e:
            log.error(
                "⚠️ Error enviando plantilla WhatsApp (%s → %s): %s",
                template_id,
                user_id,
                e,
                exc_info=True,
            )
            return {
                "ok": False,
                "provider": "meta_whatsapp",
                "message_type": "template",
                "template_name": template_id,
                "recipient_id": user_id,
                "delivery_status": "error",
                "raw_error": str(e),
            }

    # ---------------------------------------------------------------------
    # Parser de payload (Meta Webhook)
    # ---------------------------------------------------------------------
    def extract_message_data(
        self, payload: dict
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        """Extrae (user_id, msg_id, msg_type, user_msg) del webhook de Meta."""
        try:
            entries = payload.get("entry", [])
            if not entries:
                return None, None, None, None
            changes = entries[0].get("changes", [])
            if not changes:
                return None, None, None, None
            value = changes[0].get("value", {})
            messages = value.get("messages", [])
            if not messages:
                return None, None, None, None
            msg = messages[0]
            msg_type = msg.get("type")
            user_id = msg.get("from")
            msg_id = msg.get("id")

            if msg_type == "text":
                user_msg = msg.get("text", {}).get("body", "").strip()
            elif msg_type == "interactive":
                inter = msg.get("interactive", {})
                if "button_reply" in inter:
                    user_msg = inter["button_reply"].get("title", "").strip()
                elif "list_reply" in inter:
                    user_msg = inter["list_reply"].get("title", "").strip()
                else:
                    user_msg = "[Respuesta interactiva]"
            elif msg_type == "audio":
                media_id = msg.get("audio", {}).get("id")
                user_msg = transcribe_audio(media_id, C.WHATSAPP_TOKEN, C.OPENAI_API_KEY)
            elif msg_type == "image":
                user_msg = msg.get("image", {}).get("caption", "Imagen recibida.").strip()
            else:
                user_msg = f"[Tipo de mensaje no soportado: {msg_type}]"

            return user_id, msg_id, msg_type, user_msg

        except Exception as e:
            log.error(f"Error extrayendo datos: {e}", exc_info=True)
            return None, None, None, None
