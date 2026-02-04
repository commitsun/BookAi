"""Pipeline principal para procesar mensajes de usuarios."""

from __future__ import annotations

import logging
import re

from core.main_agent import create_main_agent
from core.instance_context import hydrate_dynamic_context

log = logging.getLogger("Pipeline")


async def process_user_message(
    user_message: str,
    chat_id: str,
    state,
    hotel_name: str = "Hotel",
    channel: str = "whatsapp",
    instance_number: str | None = None,
    memory_id: str | None = None,
) -> str | None:
    """
    Flujo principal:
      1. Supervisor Input
      2. Main Agent
      3. Supervisor Output
      4. Escalación → InternoAgent
    """
    try:
        mem_id = memory_id or chat_id
        log.info("📨 Nuevo mensaje de %s: %s", chat_id, user_message[:150])
        if state.memory_manager:
            state.memory_manager.set_flag(mem_id, "default_channel", channel)

        clean_id = re.sub(r"\D", "", str(chat_id or "")).strip() or str(chat_id or "")
        bookai_flags = getattr(state, "tracking", {}).get("bookai_enabled", {})
        if isinstance(bookai_flags, dict) and bookai_flags.get(clean_id) is False:
            try:
                state.memory_manager.save(mem_id, "user", user_message)
            except Exception as exc:
                log.warning("No se pudo guardar mensaje con BookAI apagado: %s", exc)
            log.info("🤫 BookAI desactivado para %s; se omite respuesta automática.", clean_id)
            return None

        input_validation = await state.supervisor_input.validate(user_message)
        estado_in = input_validation.get("estado", "Aprobado")
        motivo_in = input_validation.get("motivo", "")

        if estado_in.lower() not in ["aprobado", "ok", "aceptable"]:
            log.warning("🚨 Mensaje rechazado por Supervisor Input: %s", motivo_in)
            await state.interno_agent.escalate(
                guest_chat_id=chat_id,
                guest_message=user_message,
                escalation_type="inappropriate",
                reason=motivo_in,
                context="Rechazado por Supervisor Input",
            )
            return None

        try:
            history = state.memory_manager.get_memory_as_messages(mem_id)
        except Exception as exc:
            log.warning("⚠️ No se pudo obtener memoria: %s", exc)
            history = []

        # Evitar duplicados: si el huésped confirma y ya se envió un resumen reciente con localizador.
        response_raw = None
        try:
            recent_summary = False
            raw_hist = state.memory_manager.get_memory(mem_id, limit=8) if state.memory_manager else []
            for msg in raw_hist or []:
                role = (msg.get("role") or "").lower()
                if role not in {"assistant", "bookai"}:
                    continue
                content = str(msg.get("content") or "")
                if re.search(r"Localizador\\s*[:#]?\\s*[A-Za-z0-9/\\-]{4,}", content, re.IGNORECASE):
                    recent_summary = True
                    break

            confirmation = re.fullmatch(
                r"\\s*(vale|ok|okay|perfecto|sí|si|de acuerdo|correcto|esa me va bien|me va bien|todo bien|confirmo|confirmada|est[aá] bien)\\s*[.!]*\\s*",
                user_message,
                re.IGNORECASE,
            )
            if recent_summary and confirmation:
                response_raw = "¡Perfecto! Queda confirmada. Si necesitas algo más, dímelo."
                try:
                    state.memory_manager.save(
                        mem_id,
                        role="assistant",
                        content=response_raw,
                        channel=channel,
                    )
                except Exception as exc:
                    log.warning("No se pudo guardar respuesta corta de confirmación: %s", exc)
        except Exception as exc:
            log.debug("No se pudo aplicar regla anti-duplicados: %s", exc)

        # Respuesta rápida: si el huésped pide el localizador y ya está en historial.
        localizador = None
        if state.memory_manager:
            try:
                localizador = state.memory_manager.get_flag(mem_id, "reservation_locator") or localizador
                raw_hist = state.memory_manager.get_memory(mem_id, limit=30) or []
                for msg in raw_hist:
                    content = (msg.get("content") or "")
                    if not isinstance(content, str):
                        continue
                    match = re.search(
                        r"(localizador)\\s*[:#]?\\s*([A-Za-z0-9/\\-]{4,})",
                        content,
                        re.IGNORECASE,
                    )
                    if match:
                        localizador = match.group(2)
                        continue
                    match = re.search(r"(folio(?:_id)?)\\s*[:#]?\\s*([A-Za-z0-9]{4,})", content, re.IGNORECASE)
                    if not match:
                        match = re.search(r"reserva\\s*[:#]?\\s*([A-Za-z0-9]{4,})", content, re.IGNORECASE)
                    if match:
                        candidate = match.group(2) if match.lastindex and match.lastindex >= 2 else match.group(1)
                        if re.fullmatch(r"(?=.*\\d)[A-Za-z0-9]{4,}", candidate or ""):
                            localizador = candidate
            except Exception as exc:
                log.debug("No se pudo extraer localizador de historial: %s", exc)

        asks_localizador = bool(re.search(r"localizador|folio|n[uú]mero de reserva", user_message, re.IGNORECASE))
        if not response_raw and asks_localizador and localizador:
            response_raw = f"El localizador de tu reserva es {localizador}."
            try:
                state.memory_manager.save(
                    mem_id,
                    role="assistant",
                    content=response_raw,
                    channel=channel,
                )
            except Exception as exc:
                log.warning("No se pudo guardar respuesta rápida de localizador: %s", exc)
        # response_raw ya puede venir de regla anti-duplicados o localizador rápido

        async def send_inciso_callback(msg: str):
            try:
                await state.channel_manager.send_message(
                    chat_id,
                    msg,
                    channel=channel,
                    context_id=mem_id,
                )
            except Exception as exc:
                log.error("❌ Error enviando inciso: %s", exc)

        try:
            hydrate_dynamic_context(
                state=state,
                chat_id=mem_id,
                instance_number=instance_number,
            )
        except Exception as exc:
            log.warning("No se pudo hidratar contexto dinamico: %s", exc)

        if not response_raw:
            main_agent = create_main_agent(
                memory_manager=state.memory_manager,
                send_callback=send_inciso_callback,
                interno_agent=state.interno_agent,
            )

            response_raw = await main_agent.ainvoke(
                user_input=user_message,
                chat_id=mem_id,
                hotel_name=hotel_name,
                chat_history=history,
            )

        if not response_raw:
            await state.interno_agent.escalate(
                guest_chat_id=chat_id,
                guest_message=user_message,
                escalation_type="info_not_found",
                reason="Main Agent no devolvió respuesta",
                context="Respuesta vacía o nula",
            )
            return None

        response_raw = response_raw.strip()
        log.info("🤖 Respuesta del MainAgent: %s", response_raw[:300])

        output_validation = await state.supervisor_output.validate(
            user_input=user_message,
            agent_response=response_raw,
        )
        estado_out = (output_validation.get("estado", "Aprobado") or "").lower()
        motivo_out = output_validation.get("motivo", "")

        if "aprobado" not in estado_out:
            log.warning("🚨 Respuesta rechazada por Supervisor Output: %s", motivo_out)

            hist_text = ""
            try:
                raw_hist = state.memory_manager.get_memory(mem_id, limit=6)
                if raw_hist:
                    lines = []
                    for m in raw_hist:
                        role = m.get("role")
                        if role == "guest":
                            prefix = "Huésped"
                        elif role == "user":
                            prefix = "Hotel"
                        elif role in {"assistant", "bookai"}:
                            prefix = "BookAI"
                        else:
                            prefix = "BookAI"
                        lines.append(f"{prefix}: {m.get('content','')}")
                    hist_text = "\n".join(lines)
            except Exception as exc:
                log.warning("⚠️ No se pudo recuperar historial para escalación: %s", exc)

            context_full = (
                f"Respuesta rechazada: {response_raw[:150]}\n\n"
                f"🧠 Historial reciente:\n{hist_text}"
            )

            await state.interno_agent.escalate(
                guest_chat_id=chat_id,
                guest_message=user_message,
                escalation_type="bad_response",
                reason=motivo_out,
                context=context_full,
            )
            return None

        return response_raw

    except Exception as exc:
        log.error("💥 Error crítico en pipeline: %s", exc, exc_info=True)
        await state.interno_agent.escalate(
            guest_chat_id=chat_id,
            guest_message=user_message,
            escalation_type="info_not_found",
            reason=f"Error crítico: {str(exc)}",
            context="Excepción general en process_user_message",
        )
        return None
