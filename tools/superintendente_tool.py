"""
Herramientas para el Superintendente (implementación simple con StructuredTool)
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Optional, Callable

from langchain.tools import StructuredTool
from pydantic import BaseModel, Field

from core.db import get_conversation_history
from core.mcp_client import mcp_client
from core.config import Settings

log = logging.getLogger("SuperintendenteTools")


class AddToKBInput(BaseModel):
    topic: str = Field(..., description="Tema o categoría (ej: 'Servicios de Spa')")
    content: str = Field(..., description="Contenido detallado de la información")
    category: str = Field(
        default="general",
        description="Categoría: servicios, ubicación, politicas, etc",
    )


class SendBroadcastInput(BaseModel):
    template_id: str = Field(..., description="ID de la plantilla de WhatsApp")
    guest_ids: str = Field(..., description="IDs de huéspedes separados por comas")
    parameters: Optional[dict] = Field(
        None,
        description="Parámetros de la plantilla (JSON)",
    )
    language: str = Field(
        default="es",
        description="Código de idioma de la plantilla (ej: es, en)",
    )
    hotel_code: Optional[str] = Field(
        default=None,
        description="Código externo del hotel (opcional, para plantillas específicas)",
    )


class ReviewConversationsInput(BaseModel):
    limit: int = Field(
        default=10,
        description="Cantidad de conversaciones recientes a revisar",
    )
    guest_id: Optional[str] = Field(
        default=None,
        description="ID del huésped/WhatsApp (incluye prefijo de país, ej: +34123456789)",
    )
    mode: Optional[str] = Field(
        default=None,
        description="Modo de entrega: 'resumen' (síntesis IA) u 'original' (mensajes tal cual)",
    )


class SendMessageMainInput(BaseModel):
    message: str = Field(
        ...,
        description="Mensaje que el encargado quiere enviar al MainAgent",
    )


class SendWhatsAppInput(BaseModel):
    guest_id: str = Field(..., description="ID del huésped en WhatsApp (con prefijo país)")
    message: str = Field(..., description="Mensaje de texto a enviar (sin plantilla)")


class ConsultaReservaGeneralInput(BaseModel):
    fecha_inicio: str = Field(..., description="Fecha de inicio en formato YYYY-MM-DD")
    fecha_fin: str = Field(..., description="Fecha final en formato YYYY-MM-DD")
    pms_property_id: int = Field(
        default=38,
        description="ID de la propiedad en el PMS (por defecto 38)",
    )


class ConsultaReservaPersonaInput(BaseModel):
    folio_id: str = Field(..., description="ID del folio de la reserva")
    pms_property_id: int = Field(
        default=38,
        description="ID de la propiedad en el PMS (por defecto 38)",
    )


class RemoveFromKBInput(BaseModel):
    criterio: str = Field(
        ...,
        description="Tema/palabra clave o instrucción de qué eliminar de la base de conocimientos.",
    )
    fecha_inicio: Optional[str] = Field(
        default=None,
        description="Fecha inicial YYYY-MM-DD para filtrar los registros a eliminar.",
    )
    fecha_fin: Optional[str] = Field(
        default=None,
        description="Fecha final YYYY-MM-DD para filtrar los registros a eliminar.",
    )


class ListTemplatesInput(BaseModel):
    language: str = Field(
        default="es", description="Idioma a listar (ej: es, en, fr)"
    )
    hotel_code: Optional[str] = Field(
        default=None,
        description="Código de hotel para filtrar. Si no se pasa, usa el hotel activo.",
    )
    refresh: bool = Field(
        default=False,
        description="Si true, recarga las plantillas desde Supabase antes de listar.",
    )


class SendTemplateDraftInput(BaseModel):
    template_code: str = Field(..., description="Código interno de la plantilla.")
    guest_ids: str = Field(
        ...,
        description="IDs/phones de los huéspedes separados por coma o espacios. Se normaliza a dígitos.",
    )
    parameters: Optional[Any] = Field(
        default=None,
        description="Parámetros a rellenar (dict). También se acepta lista ordenada o JSON string.",
    )
    language: str = Field(default="es", description="Idioma de la plantilla (ej: es, en)")
    hotel_code: Optional[str] = Field(
        default=None,
        description="Código de hotel para escoger plantillas específicas.",
    )
    refresh: bool = Field(
        default=False,
        description="Si true, recarga desde Supabase antes de preparar el borrador.",
    )


def create_list_templates_tool(
    hotel_name: str,
    template_registry: Any = None,
    supabase_client: Any = None,
):
    def _format_panel(lines: list[str]) -> str:
        # Panel sin recuadro extra para evitar duplicados en el chat.
        return "\n".join(lines)

    def _normalize_lang(lang: str) -> str:
        return (lang or "es").split("-")[0].strip().lower() or "es"

    async def _list_templates(
        language: str = "es",
        hotel_code: Optional[str] = None,
        refresh: bool = False,
    ) -> str:
        if not template_registry:
            return "⚠️ No tengo acceso al registro de plantillas."

        try:
            if refresh and supabase_client:
                template_registry.load_supabase(
                    supabase_client, table=Settings.TEMPLATE_SUPABASE_TABLE
                )
        except Exception as exc:
            log.warning("No se pudo recargar las plantillas desde Supabase: %s", exc)

        lang_norm = _normalize_lang(language)
        target_hotel = (hotel_code or "").strip().upper() or None
        fallback_hotel = (hotel_name or "").strip().upper() or None

        templates = template_registry.list_templates()
        picked: dict[str, Any] = {}
        for tpl in templates:
            if _normalize_lang(tpl.language) != lang_norm:
                continue
            tpl_hotel = (tpl.hotel_code or "").strip().upper() or None

            # Filtrado: si se indicó hotel_code, acepta solo ese o las genéricas
            if target_hotel:
                if tpl_hotel and tpl_hotel != target_hotel:
                    continue
            else:
                # Sin filtro explícito: acepta las del hotel activo o genéricas
                if tpl_hotel and fallback_hotel and tpl_hotel != fallback_hotel:
                    continue

            key = tpl.code
            prev = picked.get(key)
            prefer_current = False
            if not prev:
                prefer_current = True
            elif target_hotel and tpl_hotel == target_hotel and not prev.hotel_code:
                prefer_current = True
            elif not target_hotel and fallback_hotel and tpl_hotel == fallback_hotel and not prev.hotel_code:
                prefer_current = True

            if prefer_current:
                picked[key] = tpl

        if not picked:
            hotel_label = hotel_code or hotel_name
            return f"⚠️ No encontré plantillas en {lang_norm} para {hotel_label}."

        lang_label = "español" if lang_norm == "es" else lang_norm
        hotel_label = hotel_code or hotel_name
        lines = [
            f"Estas son las plantillas de WhatsApp disponibles en {lang_label} para {hotel_label}:",
            "",
        ]

        for code in sorted(picked.keys()):
            tpl = picked[code]
            desc = tpl.description or "Sin descripción"
            params = list(tpl.parameter_hints.keys())
            params_preview = ""
            if params:
                shown = ", ".join(params[:3])
                if len(params) > 3:
                    shown += ", ..."
                params_preview = f" (pide: {shown})"
            lines.append(f"• {tpl.whatsapp_name or tpl.code}: {desc}{params_preview}")

        lines.append("")
        lines.append("Si necesitas el detalle de alguna plantilla o quieres usar alguna, indícamelo.")
        return _format_panel(lines)

    return StructuredTool.from_function(
        name="listar_plantillas_whatsapp",
        description=(
            "Lista las plantillas de WhatsApp disponibles desde Supabase para un idioma/hotel. "
            "Úsala cuando el encargado pida ver qué plantillas están registradas."
        ),
        coroutine=_list_templates,
        args_schema=ListTemplatesInput,
    )


def create_send_template_tool(
    hotel_name: str,
    channel_manager: Any,
    template_registry: Any = None,
    supabase_client: Any = None,
):
    from core.template_registry import TemplateDefinition

    def _format_panel(lines: list[str]) -> str:
        # Panel sin recuadro extra para evitar doble borde.
        return "\n".join(lines)

    def _normalize_lang(lang: str) -> str:
        return (lang or "es").split("-")[0].strip().lower() or "es"

    def _parse_guest_ids(raw: str) -> tuple[list[str], list[str]]:
        display: list[str] = []
        clean_ids: list[str] = []
        if not raw:
            return display, clean_ids
        parts = re.split(r"[,\n]+|\s+", raw)
        seen = set()
        for part in parts:
            if not part:
                continue
            disp = part.strip()
            normalized = re.sub(r"\D", "", disp)
            if not normalized:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            display.append(disp)
            clean_ids.append(normalized)
        return display, clean_ids

    def _format_param_label(tpl: TemplateDefinition, name: str) -> str:
        label = tpl.get_param_label(name) if tpl else name
        return f"{name} ({label})" if label and label != name else name

    def _normalize_parameters(raw_params: Any, tpl: TemplateDefinition) -> dict:
        """Acepta dict, lista u otras entradas y devuelve dict nominal."""
        if raw_params is None:
            return {}
        if isinstance(raw_params, dict):
            return raw_params
        # Si llega JSON como texto
        if isinstance(raw_params, str):
            try:
                parsed = json.loads(raw_params)
                if isinstance(parsed, dict):
                    return parsed
                if isinstance(parsed, list):
                    return {name: parsed[idx] for idx, name in enumerate(tpl.parameter_order) if idx < len(parsed)}
            except Exception:
                return {}
        # Si llega lista/tupla, la mapeamos al orden
        if isinstance(raw_params, (list, tuple)):
            return {name: raw_params[idx] for idx, name in enumerate(tpl.parameter_order) if idx < len(raw_params)}
        return {}

    async def _send_template(
        template_code: str,
        guest_ids: str,
        parameters: Optional[dict] = None,
        language: str = "es",
        hotel_code: Optional[str] = None,
        refresh: bool = False,
    ) -> str:
        if not channel_manager:
            return "⚠️ Canal de envío no configurado."
        if not template_registry:
            return "⚠️ No tengo acceso al registro de plantillas."

        try:
            if refresh and supabase_client:
                template_registry.load_supabase(
                    supabase_client, table=Settings.TEMPLATE_SUPABASE_TABLE
                )
        except Exception as exc:
            log.warning("No se pudo recargar las plantillas desde Supabase: %s", exc)

        lang_norm = _normalize_lang(language)
        hotel_filter = (hotel_code or "").strip().upper() or None
        tpl = None
        try:
            hotel_candidates = []
            if hotel_filter:
                hotel_candidates.append(hotel_filter)
            if hotel_name and hotel_name not in hotel_candidates:
                hotel_candidates.append(hotel_name)
            hotel_candidates.append(None)

            for h in hotel_candidates:
                tpl = template_registry.resolve(
                    hotel_code=h,
                    template_code=template_code,
                    language=lang_norm,
                )
                if tpl:
                    break
        except Exception as exc:
            log.warning("No se pudo resolver plantilla '%s': %s", template_code, exc)

        if not tpl:
            hotel_label = hotel_filter or hotel_name
            return (
                f"⚠️ No encontré la plantilla '{template_code}' en {lang_norm} "
                f"para {hotel_label}. Pide el listado para ver las disponibles."
            )

        display_ids, normalized_ids = _parse_guest_ids(guest_ids)
        if not normalized_ids:
            return "⚠️ No encontré ningún huésped válido. Indica al menos un número con prefijo de país."

        provided = _normalize_parameters(parameters, tpl)
        missing = []
        for name in tpl.parameter_order:
            val = provided.get(name)
            if val is None or (isinstance(val, str) and not val.strip()):
                missing.append(name)

        wa_template = tpl.whatsapp_name or tpl.code
        language_to_use = _normalize_lang(tpl.language or lang_norm)
        prepared_params = tpl.build_meta_parameters(provided)

        lines = [
            f"Borrador preparado para enviar la plantilla {wa_template} a {', '.join(display_ids)}.",
        ]

        if missing:
            lines.append("Faltan los siguientes parámetros obligatorios:")
            for name in missing:
                lines.append(f"• {_format_param_label(tpl, name)}")
            lines.append("")
            lines.append(
                "Por favor, indícame los valores para estos campos o confirma si deseas enviarlo tal cual "
                "(los campos pendientes aparecerán vacíos en el mensaje)."
            )
        elif tpl.parameter_order or provided:
            lines.append("Parámetros incluidos en el borrador:")
            shown = False
            for name in tpl.parameter_order:
                val = provided.get(name)
                if val is None or (isinstance(val, str) and not val.strip()):
                    continue
                shown = True
                lines.append(f"• {tpl.get_param_label(name)}: {val}")
            for name, val in provided.items():
                if name in tpl.parameter_order:
                    continue
                if val is None or (isinstance(val, str) and not val.strip()):
                    continue
                shown = True
                lines.append(f"• {name}: {val}")
            if not shown:
                lines.append("• (sin parámetros)")

        lines.append("")
        lines.append('✅ Responde "sí" para enviar.')
        lines.append('✏️ Si necesitas cambios, indícalo y preparo otro borrador.')
        lines.append('❌ Responde "no" para cancelar.')

        payload = {
            "template": wa_template,
            "language": language_to_use,
            "parameters": prepared_params,
            "guest_ids": normalized_ids,
            "display_guest_ids": display_ids,
        }

        marker = json.dumps(payload, ensure_ascii=False)
        preview = _format_panel(lines)
        return f"[TPL_DRAFT]|{marker}\n{preview}"

    return StructuredTool.from_function(
        name="preparar_envio_plantilla",
        description=(
            "Prepara un borrador para enviar una plantilla de WhatsApp a uno o varios huéspedes. "
            "Muestra parámetros faltantes y espera confirmación antes de enviarla."
        ),
        coroutine=_send_template,
        args_schema=SendTemplateDraftInput,
    )


def create_add_to_kb_tool(
    hotel_name: str,
    append_func: Callable[[str, str, str, str], Any],
    llm: Any = None,
):
    async def _rewrite_with_ai(topic: str, category: str, content: str) -> tuple[str, str, str]:
        """
        Reformula el borrador con IA para que sea apto para huéspedes y devuelva
        campos estructurados. Se usa un prompt ligero para no inventar datos.
        """
        if not llm:
            return topic, category, content

        try:
            prompt = (
                "Eres el redactor de la base de conocimientos del hotel. "
                "Reescribe el contenido en tono neutro y claro para huéspedes, sin emojis. "
                "Devuelve siempre este formato exacto:\n"
                "TEMA: <título breve>\n"
                "CATEGORÍA: <categoría>\n"
                "CONTENIDO:\n"
                "<texto en 3-6 frases cortas, solo hechos confirmados>"
            )
            user_msg = (
                f"Hotel: {hotel_name}\n"
                f"Tema propuesto: {topic}\n"
                f"Categoría: {category}\n"
                f"Notas del encargado:\n{content}"
            )

            response = await llm.ainvoke(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_msg},
                ]
            )

            text = (getattr(response, "content", None) or "").strip()
            if not text:
                return topic, category, content

            topic_match = re.search(r"tema\s*:\s*(.+)", text, flags=re.IGNORECASE)
            category_match = re.search(r"categor[ií]a\s*:\s*(.+)", text, flags=re.IGNORECASE)
            content_match = re.search(r"contenido\s*:\s*(.+)", text, flags=re.IGNORECASE | re.DOTALL)

            new_topic = topic_match.group(1).strip() if topic_match else topic
            new_category = category_match.group(1).strip() if category_match else category
            new_content = content_match.group(1).strip() if content_match else content

            # Evita pipes que rompen el marcador [KB_DRAFT]
            new_topic = new_topic.replace("|", "/")
            new_category = new_category.replace("|", "/")
            new_content = new_content.replace("|", "/")

            return new_topic or topic, new_category or category, new_content or content
        except Exception as exc:
            log.warning("No se pudo reformular KB con IA: %s", exc)
            return topic, category, content

    async def _add_to_kb(topic: str, content: str, category: str = "general") -> str:
        """
        Genera un borrador pendiente de confirmación para agregar a la KB.
        La confirmación la gestionará el webhook de Telegram antes de llamar a append_func.
        """
        log.info("Preparando borrador de KB (S3): %s (categoría: %s)", topic, category)
        safe_content = (content or "").replace("|", "/").strip()
        safe_topic = (topic or "").replace("|", "/").strip()[:200]
        safe_category = (category or "general").replace("|", "/").strip() or "general"

        ai_topic, ai_category, ai_content = await _rewrite_with_ai(safe_topic, safe_category, safe_content)

        final_topic = (ai_topic or safe_topic).strip()[:200]
        final_category = (ai_category or safe_category).strip() or "general"
        final_content = (ai_content or safe_content).strip()

        preview = (
            "📝 Borrador para base de conocimientos (revisado con IA).\n"
            "Confirma con 'OK' para guardar o envía ajustes para que los aplique.\n"
            f"[KB_DRAFT]|{hotel_name}|{final_topic}|{final_category}|{final_content}"
        )
        return preview

    return StructuredTool.from_function(
        name="agregar_a_base_conocimientos",
        description=(
            "Genera un borrador para agregar información a la base de conocimientos (documento en S3). "
            "El encargado debe confirmar antes de que se guarde."
        ),
        coroutine=_add_to_kb,
        args_schema=AddToKBInput,
    )


def create_send_broadcast_tool(
    hotel_name: str,
    channel_manager: Any,
    supabase_client: Any,
    template_registry: Any = None,
):
    from core.template_registry import TemplateRegistry

    async def _send_broadcast(
        template_id: str,
        guest_ids: str,
        parameters: Optional[dict] = None,
        language: str = "es",
        hotel_code: Optional[str] = None,
    ) -> str:
        try:
            ids = [gid.strip() for gid in guest_ids.split(",") if gid.strip()]
            if not channel_manager:
                return "⚠️ Canal de envío no configurado."

            target_hotel = hotel_code or hotel_name
            template_def = None
            if template_registry:
                try:
                    template_def = template_registry.resolve(
                        hotel_code=target_hotel,
                        template_code=template_id,
                        language=language,
                    )
                except Exception as exc:
                    log.warning("No se pudo resolver plantilla en registry: %s", exc)

            wa_template = template_def.whatsapp_name if template_def else template_id
            payload_params = (
                template_def.build_meta_parameters(parameters) if template_def else list((parameters or {}).values())
            )
            language_to_use = template_def.language if template_def else language

            success_count = 0
            for guest_id in ids:
                try:
                    await channel_manager.send_template_message(
                        guest_id,
                        wa_template,
                        parameters=payload_params,
                        language=language_to_use,
                    )
                    success_count += 1
                except Exception as exc:
                    log.warning("Error enviando a %s: %s", guest_id, exc)

            return (
                f"✅ Broadcast enviado a {success_count}/{len(ids)} huéspedes "
                f"(plantilla={wa_template}, idioma={language_to_use})"
            )
        except Exception as exc:
            log.error("Error en broadcast: %s", exc)
            return f"❌ Error: {exc}"

    return StructuredTool.from_function(
        name="enviar_broadcast",
        description=(
            "Envía un mensaje plantilla de WhatsApp a múltiples huéspedes. "
            "Ideal para comunicados masivos (ej: 'Cafetería cerrada por mantenimiento')."
        ),
        coroutine=_send_broadcast,
        args_schema=SendBroadcastInput,
    )


def create_review_conversations_tool(hotel_name: str, memory_manager: Any):
    async def _review_conversations(
        limit: int = 10,
        guest_id: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> str:
        try:
            if not memory_manager:
                return "⚠️ No hay gestor de memoria configurado."

            if not guest_id:
                return (
                    "⚠️ Para revisar una conversación necesito el ID del huésped "
                    "(guest_id). Ejemplo: +34683527049"
                )

            normalized_mode = (mode or "").strip().lower()
            if not normalized_mode:
                return (
                    "🤖 ¿Quieres un resumen IA o la conversación tal cual?\n"
                    "Responde 'resumen' para que sintetice los puntos clave o 'original' si quieres ver los mensajes completos."
                )

            valid_summary = {"resumen", "summary", "sintesis", "síntesis"}
            valid_raw = {"original", "historial", "completo", "raw", "crudo", "mensajes"}
            if normalized_mode not in valid_summary | valid_raw:
                return (
                    "⚠️ Modo no reconocido. Usa 'resumen' para síntesis o 'original' para ver los mensajes completos."
                )

            clean_id = str(guest_id).replace("+", "").strip()

            # Recupera de Supabase (limit extendido) y combina con memoria en RAM
            db_msgs = await asyncio.to_thread(
                get_conversation_history,
                clean_id,
                limit * 3,  # pedir más por si hay ruido o system messages
                None,
            )
            runtime_msgs = []
            try:
                runtime_msgs = memory_manager.runtime_memory.get(clean_id, [])
            except Exception:
                runtime_msgs = []

            combined = (db_msgs or []) + (runtime_msgs or [])

            def _parse_ts(ts: Any) -> float:
                try:
                    if isinstance(ts, datetime):
                        return ts.timestamp()
                    ts_str = str(ts).replace("Z", "")
                    return datetime.fromisoformat(ts_str).timestamp()
                except Exception:
                    return 0.0

            combined_sorted = sorted(combined, key=lambda m: _parse_ts(m.get("created_at")))
            convos = combined_sorted[-limit:] if combined_sorted else []

            # 🚫 Evita duplicados exactos (rol + contenido + timestamp)
            seen = set()
            deduped = []
            for msg in convos:
                key = (
                    msg.get("role", "assistant"),
                    (msg.get("content") or "").strip(),
                    str(msg.get("created_at")),
                )
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(msg)

            convos = deduped
            count = len(convos)

            if not convos:
                return f"🧠 Resumen de conversaciones recientes (0)\nNo hay mensajes recientes para {guest_id}."

            def _fmt_ts(ts: Any) -> str:
                try:
                    if isinstance(ts, datetime):
                        return ts.strftime("%d/%m %H:%M")
                    ts_str = str(ts).replace("Z", "")
                    return datetime.fromisoformat(ts_str).strftime("%d/%m %H:%M")
                except Exception:
                    return ""

            lines = []
            for msg in convos:
                role = msg.get("role", "assistant")
                prefix = {"user": "Huésped", "assistant": "Asistente", "system": "Sistema", "tool": "Tool"}.get(
                    role, "Asistente"
                )
                ts = _fmt_ts(msg.get("created_at"))
                ts_suffix = f" · {ts}" if ts else ""
                content = msg.get("content", "").strip()
                lines.append(f"- {prefix}{ts_suffix}: {content}")

            formatted = "\n".join(lines)
            if normalized_mode in valid_raw:
                return f"🗂️ Conversación recuperada ({count})\n{formatted}"

            return (
                "🧠 Historial recuperado para resumir\n"
                f"Mensajes ({count}):\n{formatted}\n"
                "➡️ Genera un resumen claro para el encargado con los puntos clave, dudas y acciones pendientes."
            )
        except Exception as exc:
            log.error("Error revisando conversaciones: %s", exc)
            return f"❌ Error: {exc}"

    return StructuredTool.from_function(
        name="revisar_conversaciones",
        description=(
            "Revisa conversaciones recientes de un huésped específico. "
            "Pregunta primero si el encargado quiere 'resumen' (síntesis IA) u 'original' (mensajes tal cual). "
            "Debes indicar el guest_id (por ejemplo +34683527049)."
        ),
        coroutine=_review_conversations,
        args_schema=ReviewConversationsInput,
    )


def create_send_message_main_tool(encargado_id: str, channel_manager: Any):
    async def _send_message_main(message: str) -> str:
        try:
            if not channel_manager:
                return "⚠️ Canal de envío no configurado."

            await channel_manager.send_message(
                encargado_id,
                f"📨 Mensaje enviado al MainAgent:\n{message}",
                channel="telegram",
            )
            return "✅ Mensaje enviado al MainAgent."
        except Exception as exc:
            log.error("Error enviando mensaje al MainAgent: %s", exc)
            return f"❌ Error: {exc}"

    return StructuredTool.from_function(
        name="enviar_mensaje_main",
        description=(
            "Envía un mensaje del encargado al MainAgent para coordinar respuestas o "
            "reactivar escalaciones."
        ),
        coroutine=_send_message_main,
        args_schema=SendMessageMainInput,
    )


def create_send_whatsapp_tool(channel_manager: Any):
    async def _send_whatsapp(guest_id: str, message: str) -> str:
        """
        Genera un borrador para envío por WhatsApp.
        La app principal gestionará confirmación/ajustes antes de enviar.
        """
        return f"[WA_DRAFT]|{guest_id}|{message}"

    return StructuredTool.from_function(
        name="enviar_mensaje_whatsapp",
        description=(
            "Genera un borrador de mensaje de texto directo por WhatsApp a un huésped, "
            "sin plantilla (proceso de confirmación requerido). "
            "Requiere el ID/phone del huésped (con prefijo de país). "
            "Úsala solo cuando el encargado pida explícitamente enviar un mensaje; no la uses para ajustes de KB ni para reinterpretar feedback."
        ),
        coroutine=_send_whatsapp,
        args_schema=SendWhatsAppInput,
    )


def create_remove_from_kb_tool(
    hotel_name: str,
    preview_func: Optional[Callable[[str, Optional[str], Optional[str]], Any]] = None,
):
    async def _remove_from_kb(
        criterio: str,
        fecha_inicio: Optional[str] = None,
        fecha_fin: Optional[str] = None,
    ) -> str:
        """
        Prepara borrador de eliminación en la KB (no borra sin confirmación).
        Devuelve marcador especial para que el webhook muestre el detalle/contador.
        """
        if not preview_func:
            return "⚠️ No tengo acceso para preparar la eliminación en la KB."

        try:
            payload = await preview_func(criterio, fecha_inicio, fecha_fin)
        except Exception as exc:
            log.error("Error preparando borrador de eliminación KB: %s", exc, exc_info=True)
            return f"❌ No pude preparar el borrador de eliminación: {exc}"

        total = int(payload.get("total_matches", 0) or 0) if isinstance(payload, dict) else 0
        json_payload = ""
        try:
            json_payload = json.dumps(payload, ensure_ascii=False)
        except Exception:
            json_payload = "{}"

        header = f"[KB_REMOVE_DRAFT]|{hotel_name}|{json_payload}"
        summary = (
            f"🧹 Borrador de eliminación listo. Encontré {total} registro(s) que coinciden.\n"
            "✅ Responde 'ok' para confirmarlo.\n"
            "📝 Indica qué conservar o ajusta el criterio para refinar la eliminación.\n"
            "❌ Responde 'no' para cancelar."
        )
        return f"{header}\n{summary}"

    return StructuredTool.from_function(
        name="eliminar_de_base_conocimientos",
        description=(
            "Prepara la eliminación de información en la base de conocimientos Variable. "
            "Úsala cuando el encargado pida eliminar/quitar/borrar/limpiar un tema o un rango de fechas completo. "
            "Siempre devuelve el marcador [KB_REMOVE_DRAFT]|hotel|payload_json con conteo/preview para confirmar."
        ),
        coroutine=_remove_from_kb,
        args_schema=RemoveFromKBInput,
    )


async def _obtener_token_mcp(tools: list[Any]) -> tuple[Optional[str], Optional[str]]:
    """
    Reutiliza la tool 'buscar_token' expuesta por MCP (igual que DispoPreciosAgent).
    Devuelve (token, error). Si hay error, token es None.
    """
    try:
        token_tool = next((t for t in tools if t.name == "buscar_token"), None)
        if not token_tool:
            return None, "No se encontró la tool 'buscar_token' en MCP."

        token_raw = await token_tool.ainvoke({})
        token_data = json.loads(token_raw) if isinstance(token_raw, str) else token_raw
        token = (
            token_data[0].get("key") if isinstance(token_data, list)
            else token_data.get("key")
        )

        if not token:
            return None, "No se pudo obtener el token de acceso."

        return str(token).strip(), None
    except Exception as exc:
        log.error("Error obteniendo token desde MCP: %s", exc, exc_info=True)
        return None, f"Error obteniendo token desde MCP: {exc}"


def create_consulta_reserva_general_tool():
    async def _consulta_reserva_general(
        fecha_inicio: str,
        fecha_fin: str,
        pms_property_id: int = 38,
    ) -> str:
        """
        Consulta folios/reservas en un rango de fechas vía MCP → n8n.
        """
        try:
            tools = await mcp_client.get_tools(server_name="DispoPreciosAgent")
        except Exception as exc:
            log.error("No se pudo acceder al MCP para consulta general: %s", exc, exc_info=True)
            return "❌ No se pudo acceder al servidor MCP para consultar reservas."

        token, token_err = await _obtener_token_mcp(tools)
        if not token:
            return token_err or "No se pudo obtener el token de acceso."

        consulta_tool = next((t for t in tools if t.name == "consulta_reserva_general"), None)
        if not consulta_tool:
            return "No se encontró la tool 'consulta_reserva_general' en MCP."

        payload = {
            "parameters0_Value": fecha_inicio.strip(),
            "parameters1_Value": fecha_fin.strip(),
            "key": token,
        }
        if pms_property_id is not None:
            payload["pmsPropertyId"] = pms_property_id

        try:
            raw_response = await consulta_tool.ainvoke(payload)
            if raw_response is None:
                log.error("Consulta de reservas devolvió respuesta vacía (raw_response=None)")
                return "❌ No se pudo obtener respuesta del PMS (respuesta vacía)."

            def _parse_response(data: Any) -> tuple[Optional[Any], Optional[str]]:
                """
                Intenta parsear la respuesta a JSON y devuelve el error en claro si no es posible.
                Maneja respuestas de error en texto plano (ej. SSL) para devolverlas al agente.
                """
                if isinstance(data, (dict, list)):
                    return data, None

                if isinstance(data, str):
                    text = data.strip()
                    try:
                        return json.loads(text), None
                    except json.JSONDecodeError:
                        # Propaga errores de n8n (p.ej. SSL) en vez de romper el flujo.
                        err_msg = text[:400]  # evita log/retornos excesivos
                        log.error("Respuesta no-JSON en consulta_reserva_general: %s", err_msg)
                        return None, f"Respuesta del PMS no es JSON: {err_msg}"

                log.error("Tipo de respuesta inesperado del PMS: %s", type(data))
                return None, f"Tipo de respuesta inesperado del PMS: {type(data).__name__}"

            parsed, parse_err = _parse_response(raw_response)
            if parse_err:
                return f"❌ Error consultando reservas: {parse_err}"

            # 🔎 Filtrar folios para reflejar solo el rango solicitado (tolerancia de 1 día al inicio)
            def _parse_date(val: Any) -> Optional[datetime]:
                try:
                    return datetime.fromisoformat(str(val).split("T")[0])
                except Exception:
                    return None

            date_from_dt = _parse_date(fecha_inicio)
            date_to_dt = _parse_date(fecha_fin)
            filtered = []
            if isinstance(parsed, list) and date_from_dt and date_to_dt:
                start_min = date_from_dt - timedelta(days=1)  # acepta llegadas 1 día antes (ej. 27/11 para finde 28-30)
                end_max = date_to_dt + timedelta(days=1)
                for folio in parsed:
                    fc = _parse_date(folio.get("firstCheckin"))
                    lc = _parse_date(folio.get("lastCheckout"))
                    if not fc or not lc:
                        continue
                    if fc < start_min:
                        continue  # descarta estancias largas que comienzan mucho antes
                    if lc > end_max and (lc - fc).days > 7:
                        continue  # descarta estancias demasiado largas fuera del rango
                    filtered.append(folio)
            else:
                filtered = parsed

            # 🔎 Simplificar salida para que el agente formatee panel consistente
            def _fmt_date(val: Any) -> str:
                try:
                    return str(val).split("T")[0]
                except Exception:
                    return ""

            simplified = []
            if isinstance(filtered, list):
                for folio in filtered:
                    reservations = folio.get("reservations") or []
                    first_res = reservations[0] if reservations else {}
                    simplified.append(
                        {
                            # Usa el ID como folio principal para consultas posteriores (consulta_reserva_persona)
                            "folio": folio.get("id"),
                            "folio_id": folio.get("id"),  # alias explícito para presentación/consulta
                            "folio_code": folio.get("name"),
                            "partner_name": folio.get("partnerName"),
                            "partner_phone": folio.get("partnerPhone"),
                            "partner_email": folio.get("partnerEmail"),
                            "state": folio.get("state"),
                            "amount_total": folio.get("amountTotal"),
                            "pending_amount": folio.get("pendingAmount"),
                            "payment_state": folio.get("paymentStateDescription") or folio.get("paymentStateCode"),
                            "checkin": _fmt_date(first_res.get("checkin") or folio.get("firstCheckin")),
                            "checkout": _fmt_date(first_res.get("checkout") or folio.get("lastCheckout")),
                        }
                    )
            else:
                simplified = filtered

            return json.dumps(simplified, ensure_ascii=False)
        except Exception as exc:
            log.error("Error consultando reservas generales: %s", exc, exc_info=True)
            return f"❌ Error consultando reservas: {exc}"

    return StructuredTool.from_function(
        name="consulta_reserva_general",
        description=(
            "Revisa folios/reservas creadas entre dos fechas (formato YYYY-MM-DD). "
            "Usa MCP/n8n: busca el token y consulta el PMS."
        ),
        coroutine=_consulta_reserva_general,
        args_schema=ConsultaReservaGeneralInput,
    )


def create_consulta_reserva_persona_tool():
    async def _consulta_reserva_persona(
        folio_id: str,
        pms_property_id: int = 38,
    ) -> str:
        """
        Consulta los detalles de un folio específico vía MCP → n8n.
        """
        try:
            tools = await mcp_client.get_tools(server_name="DispoPreciosAgent")
        except Exception as exc:
            log.error("No se pudo acceder al MCP para consulta de folio: %s", exc, exc_info=True)
            return "❌ No se pudo acceder al servidor MCP para consultar el folio."

        token, token_err = await _obtener_token_mcp(tools)
        if not token:
            return token_err or "No se pudo obtener el token de acceso."

        consulta_tool = next((t for t in tools if t.name == "consulta_reserva_persona"), None)
        if not consulta_tool:
            return "No se encontró la tool 'consulta_reserva_persona' en MCP."

        payload = {
            "folio_id": folio_id.strip(),
            "key": token,
        }
        if pms_property_id is not None:
            payload["pmsPropertyId"] = pms_property_id

        try:
            raw_response = await consulta_tool.ainvoke(payload)
            parsed = json.loads(raw_response) if isinstance(raw_response, str) else raw_response
            return json.dumps(parsed, ensure_ascii=False)
        except Exception as exc:
            log.error("Error consultando reserva por folio: %s", exc, exc_info=True)
            return f"❌ Error consultando el folio: {exc}"

    return StructuredTool.from_function(
        name="consulta_reserva_persona",
        description=(
            "Obtiene los datos detallados de un folio de reserva específico usando MCP/n8n. "
            "Necesita el folio_id y busca el token automáticamente. "
            "Incluye portalUrl si está disponible (enlace a portal/factura)."
        ),
        coroutine=_consulta_reserva_persona,
        args_schema=ConsultaReservaPersonaInput,
    )
