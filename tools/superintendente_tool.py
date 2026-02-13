"""
Herramientas para el Superintendente (implementación simple con StructuredTool)
"""

import asyncio
import json
import logging
import re
import unicodedata
from datetime import datetime, timedelta
from typing import Any, Optional, Callable

from langchain.tools import StructuredTool
from pydantic import BaseModel, Field

from core.db import get_conversation_history
from core.db import supabase
from core.mcp_client import get_tools
from core.instance_context import (
    fetch_instance_by_code,
    fetch_property_by_code,
    fetch_property_by_id,
    fetch_property_by_name,
    DEFAULT_PROPERTY_TABLE,
)
from core.config import Settings, ModelConfig, ModelTier
from core.utils.time_context import DEFAULT_TZ
import pytz

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
    instance_id: Optional[str] = Field(
        default=None,
        description="Identificador de la instancia (opcional, para plantillas específicas).",
    )
    property_id: Optional[int] = Field(
        default=None,
        description="ID de propiedad (property_id) para contexto multipropiedad.",
    )


class SendBroadcastCheckinInput(BaseModel):
    template_id: str = Field(..., description="ID de la plantilla de WhatsApp")
    date: Optional[str] = Field(
        default=None,
        description="Fecha de check-in objetivo (YYYY-MM-DD). Si no se indica, usa mañana.",
    )
    parameters: Optional[dict] = Field(
        None,
        description="Parámetros de la plantilla (JSON)",
    )
    language: str = Field(
        default="es",
        description="Código de idioma de la plantilla (ej: es, en)",
    )
    instance_id: Optional[str] = Field(
        default=None,
        description="Identificador de la instancia (opcional, para plantillas específicas).",
    )
    property_id: Optional[int] = Field(
        default=None,
        description="ID de propiedad (property_id) para contexto multipropiedad.",
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
    property_id: Optional[int] = Field(
        default=None,
        description="ID de propiedad (property_id) para filtrar el historial.",
    )
    instance_id: Optional[str] = Field(
        default=None,
        description="Identificador de la instancia (opcional) para fijar el contexto.",
    )


class SendMessageMainInput(BaseModel):
    message: str = Field(
        ...,
        description="Mensaje que el encargado quiere enviar al MainAgent",
    )


class SendWhatsAppInput(BaseModel):
    guest_id: str = Field(..., description="ID del huésped en WhatsApp (con prefijo país)")
    message: str = Field(..., description="Mensaje de texto a enviar (sin plantilla)")
    property_id: Optional[int] = Field(
        default=None,
        description="ID de propiedad (property_id) para contexto multipropiedad.",
    )
    instance_id: Optional[str] = Field(
        default=None,
        description="Identificador de la instancia para contexto multipropiedad.",
    )


class ConsultaReservaGeneralInput(BaseModel):
    fecha_inicio: str = Field(..., description="Fecha de inicio en formato YYYY-MM-DD")
    fecha_fin: str = Field(..., description="Fecha final en formato YYYY-MM-DD")
    property_id: Optional[int] = Field(
        default=None,
        description="ID de propiedad (property_id).",
    )
    pms_property_id: Optional[int] = Field(
        default=None,
        description="ID de la propiedad en el PMS (compatibilidad)",
    )
    instance_url: Optional[str] = Field(
        default=None,
        description="URL de la instancia (opcional)",
    )
    instance_id: Optional[str] = Field(
        default=None,
        description="Identificador de la instancia (opcional).",
    )
    enrich_contact: bool = Field(
        default=False,
        description="Si es true, completa teléfono/email consultando detalle por folio.",
    )


class ConsultaReservaPersonaInput(BaseModel):
    folio_id: str = Field(..., description="ID del folio de la reserva")
    property_id: Optional[int] = Field(
        default=None,
        description="ID de propiedad (property_id).",
    )
    pms_property_id: Optional[int] = Field(
        default=None,
        description="ID de la propiedad en el PMS (compatibilidad)",
    )
    instance_url: Optional[str] = Field(
        default=None,
        description="URL de la instancia (opcional)",
    )
    instance_id: Optional[str] = Field(
        default=None,
        description="Identificador de la instancia (opcional).",
    )


def _instance_id_variants(raw: Optional[str]) -> list[str]:
    clean = (raw or "").strip()
    if not clean:
        return []
    return [clean]


def _resolve_property_table(memory_manager: Any, chat_id: str) -> str:
    if memory_manager and chat_id:
        try:
            table = memory_manager.get_flag(chat_id, "property_table")
            if table:
                return str(table)
        except Exception:
            pass
    return DEFAULT_PROPERTY_TABLE


def _clean_phone(value: str) -> str:
    return re.sub(r"\D", "", str(value or "")).strip()


def _looks_like_phone(value: str) -> bool:
    digits = _clean_phone(value)
    return len(digits) >= 6


def _normalize_name(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    deaccented = "".join(
        ch for ch in unicodedata.normalize("NFKD", raw) if not unicodedata.combining(ch)
    )
    cleaned = re.sub(r"[^a-z0-9]+", " ", deaccented)
    return re.sub(r"\s+", " ", cleaned).strip()


def _parse_ts(value: Any) -> float:
    try:
        if isinstance(value, datetime):
            return value.timestamp()
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return 0.0


def _split_guest_tokens(raw: str) -> list[str]:
    raw = (raw or "").strip()
    if not raw:
        return []
    if "," in raw:
        return [part.strip() for part in raw.split(",") if part.strip()]
    if re.fullmatch(r"[0-9+()\s-]+", raw):
        return [part.strip() for part in re.split(r"\s+", raw) if part.strip()]
    return [raw]


def _resolve_guest_ids(
    raw: str,
    property_id: Optional[int] = None,
    memory_manager: Any = None,
    chat_id: str = "",
) -> tuple[list[str], list[str], list[dict]]:
    display: list[str] = []
    clean_ids: list[str] = []
    unresolved: list[dict] = []
    seen = set()

    for token in _split_guest_tokens(raw):
        if not token:
            continue
        if _looks_like_phone(token):
            normalized = _clean_phone(token)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            display.append(token)
            clean_ids.append(normalized)
            continue

        resolved, candidates = _resolve_guest_id_by_name(
            token,
            property_id=property_id,
            memory_manager=memory_manager,
            chat_id=chat_id,
        )
        if resolved:
            if resolved in seen:
                continue
            seen.add(resolved)
            display.append(token)
            clean_ids.append(resolved)
        else:
            unresolved.append({"name": token, "candidates": candidates})

    return display, clean_ids, unresolved


def _format_unresolved_guests(unresolved: list[dict]) -> str:
    if not unresolved:
        return ""
    lines = ["⚠️ Necesito el teléfono exacto para estos huéspedes:"]
    for item in unresolved:
        name = item.get("name") or "Sin nombre"
        candidates = item.get("candidates") or []
        if candidates:
            lines.append(f"• {name} (posibles coincidencias):")
            for cand in candidates[:5]:
                label = cand.get("client_name") or "Sin nombre"
                lines.append(f"  • {label} → {cand.get('phone')}")
        else:
            lines.append(f"• {name} (no encontrado)")
    return "\n".join(lines)


def _resolve_guest_id_by_name(
    name: str,
    property_id: Optional[int] = None,
    limit: int = 50,
    memory_manager: Any = None,
    chat_id: str = "",
) -> tuple[Optional[str], list[dict]]:
    name = (name or "").strip()
    if not name:
        return None, []

    query_name = _normalize_name(name)

    def _score(candidate: dict) -> int:
        candidate_name = _normalize_name(candidate.get("client_name"))
        if candidate_name == query_name:
            return 0
        if candidate_name.startswith(query_name):
            return 1
        if query_name in candidate_name:
            return 2
        return 3

    def _token_match(query: str, candidate: str) -> bool:
        if not query or not candidate:
            return False
        q_tokens = query.split()
        c_tokens = candidate.split()
        for qt in q_tokens:
            for ct in c_tokens:
                if qt == ct or qt.startswith(ct) or ct.startswith(qt):
                    return True
        return False

    # 1) Intentar resolver con reservas en memoria (consulta_reserva_general reciente)
    try:
        if memory_manager and chat_id:
            cached = memory_manager.get_flag(chat_id, "superintendente_last_reservations") or {}
            items = cached.get("items") if isinstance(cached, dict) else None
            if isinstance(items, list) and items:
                candidates = []
                for item in items:
                    client_name = (item.get("partner_name") or "").strip()
                    phone = _clean_phone(item.get("partner_phone") or "")
                    if not client_name or not phone:
                        continue
                    candidate_norm = _normalize_name(client_name)
                    if not _token_match(query_name, candidate_norm):
                        continue
                    candidates.append(
                        {
                            "phone": phone,
                            "client_name": client_name,
                            "created_at": item.get("checkin") or item.get("checkout"),
                            "property_id": property_id,
                            "source": "reservations",
                        }
                    )

                if candidates:
                    candidates.sort(key=lambda c: (_score(c), -_parse_ts(c.get("created_at"))))
                    unique: list[dict] = []
                    seen = set()
                    for cand in candidates:
                        phone = cand.get("phone")
                        if not phone or phone in seen:
                            continue
                        seen.add(phone)
                        cand["score"] = _score(cand)
                        unique.append(cand)
                    if unique:
                        best_score = unique[0].get("score", 3)
                        best = [c for c in unique if c.get("score", 3) == best_score]
                        if len(best) == 1:
                            return best[0].get("phone"), unique
                        # Si hay empate pero todos comparten el mismo teléfono, úsalo.
                        phones = {c.get("phone") for c in best if c.get("phone")}
                        if len(phones) == 1:
                            return next(iter(phones)), unique
                        return None, unique
            # 1b) Intentar resolver con el último detalle de reserva consultado
            last_detail = memory_manager.get_flag(chat_id, "superintendente_last_reservation_detail")
            if isinstance(last_detail, dict):
                client_name = (last_detail.get("partner_name") or last_detail.get("partnerName") or "").strip()
                phone = _clean_phone(last_detail.get("partner_phone") or last_detail.get("partnerPhone") or "")
                if client_name and phone:
                    candidate_norm = _normalize_name(client_name)
                    if _token_match(query_name, candidate_norm):
                        return phone, [
                            {
                                "phone": phone,
                                "client_name": client_name,
                                "created_at": last_detail.get("checkin") or last_detail.get("checkout"),
                                "property_id": property_id,
                                "source": "last_detail",
                            }
                        ]
    except Exception:
        pass

    def _run_query(filter_property: bool) -> list[dict]:
        query = (
            supabase.table("chat_history")
            .select("conversation_id, original_chat_id, client_name, created_at, property_id")
            .eq("role", "guest")
            .ilike("client_name", f"%{name}%")
        )
        if filter_property and property_id is not None:
            query = query.eq("property_id", property_id)
        resp = query.order("created_at", desc=True).limit(limit).execute()
        return resp.data or []

    try:
        rows = _run_query(filter_property=True)
        if not rows and property_id is not None:
            # Fallback cuando los mensajes no tienen property_id guardado.
            rows = _run_query(filter_property=False)
    except Exception as exc:
        log.warning("No se pudo resolver guest_id por nombre: %s", exc)
        return None, []

    candidates = []
    for row in rows:
        client_name = (row.get("client_name") or "").strip()
        phone = _clean_phone(row.get("conversation_id") or "")
        if not phone:
            phone = _clean_phone(row.get("original_chat_id") or "")
        if not phone:
            continue
        candidates.append(
            {
                "phone": phone,
                "client_name": client_name,
                "created_at": row.get("created_at"),
                "property_id": row.get("property_id"),
            }
        )

    if not candidates:
        return None, []

    candidates.sort(
        key=lambda c: (_score(c), -_parse_ts(c.get("created_at")))
    )

    # Deduplicar por phone, mantener el mejor match más reciente.
    unique: list[dict] = []
    seen = set()
    for cand in candidates:
        phone = cand.get("phone")
        if not phone or phone in seen:
            continue
        seen.add(phone)
        cand["score"] = _score(cand)
        unique.append(cand)

    if not unique:
        return None, []

    best_score = unique[0].get("score", 3)
    best = [c for c in unique if c.get("score", 3) == best_score]
    if len(best) == 1:
        return best[0].get("phone"), unique

    return None, unique


def _set_instance_context(
    memory_manager: Any,
    chat_id: str,
    property_id: Optional[int] = None,
    instance_id: Optional[str] = None,
) -> None:
    if not memory_manager or not chat_id:
        return

    resolved_property_id = property_id
    resolved_instance_id = (instance_id or "").strip() or None
    resolved_property_name = None
    property_table = _resolve_property_table(memory_manager, chat_id)

    log.info(
        "🏨 [WA_CTX] start chat_id=%s property_id=%s instance_id=%s table=%s",
        chat_id,
        resolved_property_id,
        resolved_instance_id,
        property_table,
    )

    if resolved_property_id is not None:
        log.info("🏨 [WA_CTX] resolve property_name via property_id=%s", resolved_property_id)
        prop_payload = fetch_property_by_id(property_table, resolved_property_id)
        prop_name = prop_payload.get("name")
        if prop_name:
            resolved_property_name = prop_name
        resolved_instance_id = prop_payload.get("instance_id") or resolved_instance_id

    if resolved_property_id is not None:
        memory_manager.set_flag(chat_id, "property_id", resolved_property_id)
    if resolved_property_name:
        memory_manager.set_flag(chat_id, "property_name", resolved_property_name)
    if resolved_instance_id:
        memory_manager.set_flag(chat_id, "instance_id", resolved_instance_id)
        memory_manager.set_flag(chat_id, "instance_hotel_code", resolved_instance_id)

    if resolved_instance_id:
        log.info("🏨 [WA_CTX] fetch instance by instance_id=%s", resolved_instance_id)
        inst_payload = fetch_instance_by_code(str(resolved_instance_id))
        if not inst_payload:
            log.info("🏨 [WA_CTX] no instance for instance_id=%s", resolved_instance_id)
        else:
            for key in ("whatsapp_phone_id", "whatsapp_token", "whatsapp_verify_token"):
                val = inst_payload.get(key)
                if val:
                    memory_manager.set_flag(chat_id, key, val)
                    log.info("🏨 [WA_CTX] set %s=%s", key, "set" if key != "whatsapp_phone_id" else val)

    if resolved_property_id is not None:
        memory_manager.set_flag(chat_id, "wa_context_property_id", resolved_property_id)
    if resolved_instance_id:
        memory_manager.set_flag(chat_id, "wa_context_instance_id", str(resolved_instance_id))

    log.info(
        "🏨 [WA_CTX] done chat_id=%s property_id=%s instance_id=%s",
        chat_id,
        memory_manager.get_flag(chat_id, "property_id"),
        memory_manager.get_flag(chat_id, "instance_id"),
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
    instance_id: Optional[str] = Field(
        default=None,
        description="Identificador de la instancia para filtrar. Si no se pasa, usa la instancia activa.",
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
    instance_id: Optional[str] = Field(
        default=None,
        description="Identificador de la instancia para escoger plantillas específicas.",
    )
    property_id: Optional[int] = Field(
        default=None,
        description="ID de propiedad (property_id) para contexto multipropiedad.",
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
        instance_id: Optional[str] = None,
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
        target_hotel = (instance_id or "").strip().upper() or None
        fallback_hotel = (hotel_name or "").strip().upper() or None

        templates = template_registry.list_templates()
        picked: dict[str, Any] = {}
        for tpl in templates:
            if _normalize_lang(tpl.language) != lang_norm:
                continue
            tpl_hotel = (tpl.instance_id or "").strip().upper() or None

            # Filtrado: si se indicó instance_id, acepta solo ese o las genéricas
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
            elif target_hotel and tpl_hotel == target_hotel and not prev.instance_id:
                prefer_current = True
            elif not target_hotel and fallback_hotel and tpl_hotel == fallback_hotel and not prev.instance_id:
                prefer_current = True

            if prefer_current:
                picked[key] = tpl

        if not picked:
            hotel_label = instance_id or hotel_name
            return f"⚠️ No encontré plantillas en {lang_norm} para {hotel_label}."

        lang_label = "español" if lang_norm == "es" else lang_norm
        hotel_label = instance_id or hotel_name
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
            "Lista las plantillas de WhatsApp disponibles desde Supabase para un idioma/instancia. "
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
    memory_manager: Any = None,
    chat_id: str = "",
):
    from core.template_registry import TemplateDefinition

    def _format_panel(lines: list[str]) -> str:
        # Panel sin recuadro extra para evitar doble borde.
        return "\n".join(lines)

    def _normalize_lang(lang: str) -> str:
        return (lang or "es").split("-")[0].strip().lower() or "es"

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
        instance_id: Optional[str] = None,
        property_id: Optional[int] = None,
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

        if memory_manager and chat_id:
            try:
                if property_id is None:
                    property_id = memory_manager.get_flag(chat_id, "property_id")
                if not instance_id:
                    instance_id = (
                        memory_manager.get_flag(chat_id, "instance_id")
                        or memory_manager.get_flag(chat_id, "instance_hotel_code")
                    )
                _set_instance_context(
                    memory_manager,
                    chat_id,
                    property_id=property_id,
                    instance_id=instance_id,
                )
            except Exception:
                pass

        lang_norm = _normalize_lang(language)
        hotel_filter = (instance_id or "").strip().upper() or None
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
                    instance_id=h,
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

        display_ids, normalized_ids, unresolved = _resolve_guest_ids(
            guest_ids,
            property_id=property_id,
            memory_manager=memory_manager,
            chat_id=chat_id,
        )
        if unresolved:
            return _format_unresolved_guests(unresolved)
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
    memory_manager: Any = None,
    chat_id: str = "",
):
    from core.template_registry import TemplateRegistry

    async def _send_broadcast(
        template_id: str,
        guest_ids: str,
        parameters: Optional[dict] = None,
        language: str = "es",
        instance_id: Optional[str] = None,
        property_id: Optional[int] = None,
    ) -> str:
        try:
            if memory_manager and chat_id:
                try:
                    if property_id is None:
                        property_id = memory_manager.get_flag(chat_id, "property_id")
                    if not instance_id:
                        instance_id = (
                            memory_manager.get_flag(chat_id, "instance_id")
                            or memory_manager.get_flag(chat_id, "instance_hotel_code")
                        )
                    _set_instance_context(
                        memory_manager,
                        chat_id,
                        property_id=property_id,
                        instance_id=instance_id,
                    )
                except Exception:
                    pass
            display_ids, ids, unresolved = _resolve_guest_ids(
                guest_ids,
                property_id=property_id,
                memory_manager=memory_manager,
                chat_id=chat_id,
            )
            if unresolved:
                return _format_unresolved_guests(unresolved)
            if not channel_manager:
                return "⚠️ Canal de envío no configurado."
            if not ids:
                return "⚠️ No encontré ningún huésped válido. Indica al menos un número con prefijo de país."

            target_hotel = instance_id or hotel_name
            template_def = None
            if template_registry:
                try:
                    template_def = template_registry.resolve(
                        instance_id=target_hotel,
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
                        context_id=chat_id or None,
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


def create_send_broadcast_checkin_tool(
    hotel_name: str,
    channel_manager: Any,
    supabase_client: Any,
    template_registry: Any = None,
    memory_manager: Any = None,
    chat_id: str = "",
):
    async def _send_broadcast_checkin(
        template_id: str,
        date: Optional[str] = None,
        parameters: Optional[dict] = None,
        language: str = "es",
        instance_id: Optional[str] = None,
        property_id: Optional[int] = None,
    ) -> str:
        if not channel_manager:
            return "⚠️ Canal de envío no configurado."

        tz = pytz.timezone(DEFAULT_TZ)
        if date:
            target_date = date.strip()
        else:
            target_date = (datetime.now(tz) + timedelta(days=1)).strftime("%Y-%m-%d")

        if memory_manager and chat_id:
            try:
                if property_id is None:
                    property_id = memory_manager.get_flag(chat_id, "property_id")
                if not instance_id:
                    instance_id = (
                        memory_manager.get_flag(chat_id, "instance_id")
                        or memory_manager.get_flag(chat_id, "instance_hotel_code")
                    )
                _set_instance_context(
                    memory_manager,
                    chat_id,
                    property_id=property_id,
                    instance_id=instance_id,
                )
            except Exception:
                pass

        tpl_def = None
        if template_registry:
            try:
                candidates = []
                if instance_id:
                    candidates.append(instance_id)
                if hotel_name and hotel_name not in candidates:
                    candidates.append(hotel_name)
                candidates.append(None)
                for cand in candidates:
                    tpl_def = template_registry.resolve(
                        instance_id=cand,
                        template_code=template_id,
                        language=language,
                    )
                    if tpl_def:
                        break
            except Exception as exc:
                log.warning("No se pudo resolver plantilla '%s': %s", template_id, exc)

        consulta_tool = create_consulta_reserva_general_tool(
            memory_manager=memory_manager,
            chat_id=chat_id,
        )
        consulta_payload = {
            "fecha_inicio": target_date,
            "fecha_fin": target_date,
            "property_id": property_id,
            "instance_id": instance_id,
        }
        raw = await consulta_tool.ainvoke(consulta_payload)
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:
            return f"❌ No pude leer las reservas para {target_date}: {exc}"

        if not isinstance(data, list):
            return f"⚠️ No encontré reservas válidas para {target_date}."

        def _normalize_phone(raw_phone: Any) -> str:
            return re.sub(r"\D", "", str(raw_phone or ""))

        def _auto_params_from_folio(folio: dict, hotel_label: str | None) -> dict:
            return {
                "buyer_name": folio.get("partner_name"),
                "guest_name": folio.get("partner_name"),
                "client_name": folio.get("partner_name"),
                "hotel_name": hotel_label,
                "checkin_date": folio.get("checkin"),
                "checkout_date": folio.get("checkout"),
                "reservation_locator": folio.get("folio_code") or folio.get("folio"),
                "reservation_code": folio.get("folio_code") or folio.get("folio"),
                "reservation_id": folio.get("folio_id") or folio.get("folio"),
                "reservation_url": folio.get("portalUrl"),
            }

        guest_ids: list[str] = []
        per_guest_params: dict[str, dict] = {}
        for folio in data:
            checkin = str(folio.get("checkin") or "")
            if checkin != target_date:
                continue
            phone = _normalize_phone(folio.get("partner_phone"))
            if not phone:
                continue
            guest_ids.append(phone)
            hotel_label = hotel_name
            auto_params = _auto_params_from_folio(folio, hotel_label)
            provided = parameters or {}
            params_for_guest = {**auto_params, **provided}
            per_guest_params[phone] = params_for_guest

        guest_ids = list(dict.fromkeys(guest_ids))
        if not guest_ids:
            return f"⚠️ No encontré huéspedes con check-in {target_date}."

        if tpl_def and tpl_def.parameter_order:
            missing_map: dict[str, list[str]] = {}
            for gid in guest_ids:
                vals = per_guest_params.get(gid, {})
                missing = []
                for key in tpl_def.parameter_order:
                    val = vals.get(key)
                    if val is None or (isinstance(val, str) and not val.strip()):
                        missing.append(key)
                if missing:
                    missing_map[gid] = missing

            if missing_map:
                missing_fields = sorted({m for missing in missing_map.values() for m in missing})
                labels = [tpl_def.get_param_label(name) for name in missing_fields]
                sample = ", ".join(guest_ids[:3])
                payload = {
                    "template_id": template_id,
                    "date": target_date,
                    "language": language,
                    "instance_id": instance_id,
                    "property_id": property_id,
                    "missing_fields": missing_fields,
                }
                header = json.dumps(payload, ensure_ascii=False)
                message = (
                    "⚠️ No puedo enviar la plantilla porque faltan parámetros obligatorios.\n"
                    f"Campos requeridos: {', '.join(labels)}.\n"
                    f"Huéspedes afectados (ejemplo): {sample}.\n"
                    "Envía un JSON con los valores (o si falta solo 1 campo, responde con el valor). "
                    "Ejemplo: {\"hotel_name\":\"Hotel X\",\"checkin_date\":\"2026-01-23\"}"
                )
                return f"[BROADCAST_DRAFT]|{header}\n{message}"

        sent = 0
        errors = 0
        for gid in guest_ids:
            params_for_guest = per_guest_params.get(gid) or parameters or {}
            final_params = params_for_guest
            if tpl_def:
                if tpl_def.parameter_order:
                    params_for_guest = {k: params_for_guest.get(k) for k in tpl_def.parameter_order}
                final_params = tpl_def.build_meta_parameters(params_for_guest)
            try:
                await channel_manager.send_template_message(
                    gid,
                    template_id,
                    parameters=final_params,
                    language=language,
                    channel="whatsapp",
                    context_id=chat_id,
                )
                sent += 1
            except Exception as exc:
                errors += 1
                log.warning("Error enviando plantilla %s a %s: %s", template_id, gid, exc)

        return f"✅ Broadcast de check-in {target_date}: enviado {sent}/{len(guest_ids)} (errores {errors})."

    return StructuredTool.from_function(
        name="enviar_broadcast_checkin",
        description=(
            "Envía una plantilla a huéspedes con check-in en una fecha (por defecto, mañana). "
            "Resuelve reservas vía MCP/Roomdoo y luego envía la plantilla masiva."
        ),
        coroutine=_send_broadcast_checkin,
        args_schema=SendBroadcastCheckinInput,
    )


def create_review_conversations_tool(hotel_name: str, memory_manager: Any, chat_id: str = ""):
    async def _review_conversations(
        limit: int = 10,
        guest_id: Optional[str] = None,
        mode: Optional[str] = None,
        property_id: Optional[int] = None,
        instance_id: Optional[str] = None,
    ) -> str:
        try:
            if not memory_manager:
                return "⚠️ No hay gestor de memoria configurado."

            normalized_mode = (mode or "").strip().lower()
            valid_summary = {"resumen", "summary", "sintesis", "síntesis"}
            valid_raw = {"original", "historial", "completo", "raw", "crudo", "mensajes"}

            if property_id is None and memory_manager and chat_id:
                try:
                    property_id = memory_manager.get_flag(chat_id, "property_id")
                except Exception:
                    property_id = None
            if not instance_id and memory_manager and chat_id:
                try:
                    instance_id = (
                        memory_manager.get_flag(chat_id, "instance_id")
                        or memory_manager.get_flag(chat_id, "instance_hotel_code")
                    )
                except Exception:
                    instance_id = None
            if instance_id and memory_manager and chat_id:
                try:
                    _set_instance_context(
                        memory_manager,
                        chat_id,
                        property_id=property_id,
                        instance_id=instance_id,
                    )
                except Exception:
                    pass

            if not guest_id:
                return (
                    "⚠️ Para revisar una conversación necesito el ID del huésped "
                    "(guest_id) o el nombre exacto. Ejemplo: +34683527049 o 'Rafa Perez'."
                )

            resolved_guest_id = None
            if not _looks_like_phone(guest_id):
                resolved_guest_id, candidates = _resolve_guest_id_by_name(
                    guest_id,
                    property_id=property_id,
                    memory_manager=memory_manager,
                    chat_id=chat_id,
                )
                if not resolved_guest_id:
                    if candidates:
                        lines = []
                        for cand in candidates[:5]:
                            label = cand.get("client_name") or "Sin nombre"
                            lines.append(f"• {label} → {cand.get('phone')}")
                        suggestions = "\n".join(lines)
                        return (
                            "⚠️ Encontré varios huéspedes con ese nombre. "
                            "Indícame el teléfono exacto:\n"
                            f"{suggestions}"
                        )
                    return (
                        f"⚠️ No encontré un huésped con el nombre '{guest_id}'. "
                        "Indícame el teléfono exacto."
                    )
                guest_id = resolved_guest_id

            if not normalized_mode:
                return (
                    "🤖 ¿Quieres un resumen IA o la conversación tal cual?\n"
                    "Responde 'resumen' para que sintetice los puntos clave o 'original' si quieres ver los mensajes completos."
                )

            if normalized_mode not in valid_summary | valid_raw:
                return (
                    "⚠️ Modo no reconocido. Usa 'resumen' para síntesis o 'original' para ver los mensajes completos."
                )

            clean_id = _clean_phone(guest_id)
            if not clean_id:
                return "⚠️ El guest_id no parece un teléfono válido. Indícame el número completo con prefijo."

            resolved_property_id = property_id


            # Recupera de Supabase (limit extendido) y combina con memoria en RAM
            db_msgs = await asyncio.to_thread(
                get_conversation_history,
                conversation_id=clean_id,
                limit=limit * 3,  # pedir más por si hay ruido o system messages
                since=None,
                property_id=resolved_property_id,
                table="chat_history",
                channel="whatsapp",
            )
            if resolved_property_id is not None and not db_msgs:
                # Fallback para historiales antiguos donde property_id no se guardó.
                db_msgs = await asyncio.to_thread(
                    get_conversation_history,
                    conversation_id=clean_id,
                    limit=limit * 3,
                    since=None,
                    property_id=None,
                    table="chat_history",
                    channel="whatsapp",
                )
            combined = (db_msgs or [])

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
                prefix = {
                    "user": "Huésped",
                    "guest": "Huésped",
                    "assistant": "Asistente",
                    "bookai": "BookAI",
                    "system": "Sistema",
                    "tool": "Tool",
                }.get(role, "Asistente")
                ts = _fmt_ts(msg.get("created_at"))
                ts_suffix = f" · {ts}" if ts else ""
                content = msg.get("content", "").strip()
                lines.append(f"- {prefix}{ts_suffix}: {content}")

            formatted = "\n".join(lines)
            if normalized_mode in valid_raw:
                return f"🗂️ Conversación recuperada ({count})\n{formatted}"

            system_prompt = (
                "Eres un asistente interno para un encargado de hotel. "
                "Resume conversaciones de WhatsApp con precisión y brevedad operativa."
            )
            user_prompt = (
                f"Huésped: {guest_id}\n"
                f"Total de mensajes analizados: {count}\n\n"
                "Mensajes:\n"
                f"{formatted}\n\n"
                "Genera un resumen útil para operación con este formato:\n"
                "1) Motivo principal del huésped\n"
                "2) Datos concretos detectados (fechas, folio, habitación, teléfono)\n"
                "3) Estado actual y acciones pendientes\n"
                "4) Riesgos o dudas abiertas"
            )
            try:
                llm = ModelConfig.get_llm(ModelTier.INTERNAL)
                ai_raw = await asyncio.to_thread(
                    llm.invoke,
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                )
                summary = (getattr(ai_raw, "content", None) or str(ai_raw or "")).strip()
            except Exception as exc:
                log.warning("No se pudo generar resumen con LLM: %s", exc)
                summary = ""

            if not summary:
                summary = (
                    "No pude generar el resumen automáticamente. "
                    "Prueba de nuevo o pide 'original' para ver los mensajes completos."
                )

            return f"🧠 Resumen de conversaciones recientes ({count})\n{summary}"
        except Exception as exc:
            log.error("Error revisando conversaciones: %s", exc)
            return f"❌ Error: {exc}"

    return StructuredTool.from_function(
        name="revisar_conversaciones",
        description=(
            "Revisa conversaciones recientes de un huésped específico. "
            "Pregunta primero si el encargado quiere 'resumen' (síntesis IA) u 'original' (mensajes tal cual). "
            "Puedes indicar guest_id (por ejemplo +34683527049) o nombre exacto del huésped."
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


def create_send_whatsapp_tool(channel_manager: Any, memory_manager: Any = None, chat_id: str = ""):
    async def _send_whatsapp(
        guest_id: str,
        message: str,
        property_id: Optional[int] = None,
        instance_id: Optional[str] = None,
    ) -> str:
        """
        Genera un borrador para envío por WhatsApp.
        La app principal gestionará confirmación/ajustes antes de enviar.
        """
        if memory_manager and chat_id:
            try:
                if property_id is None:
                    property_id = memory_manager.get_flag(chat_id, "property_id")
                if not instance_id:
                    instance_id = (
                        memory_manager.get_flag(chat_id, "instance_id")
                        or memory_manager.get_flag(chat_id, "instance_hotel_code")
                    )
                _set_instance_context(
                    memory_manager,
                    chat_id,
                    property_id=property_id,
                    instance_id=instance_id,
                )
            except Exception:
                pass

        resolved_guest_id = None
        if _looks_like_phone(guest_id):
            resolved_guest_id = _clean_phone(guest_id)
        else:
            resolved_guest_id, candidates = _resolve_guest_id_by_name(
                guest_id,
                property_id=property_id,
                memory_manager=memory_manager,
                chat_id=chat_id,
            )
            if not resolved_guest_id:
                if candidates:
                    lines = []
                    for cand in candidates[:5]:
                        label = cand.get("client_name") or "Sin nombre"
                        lines.append(f"• {label} → {cand.get('phone')}")
                    suggestions = "\n".join(lines)
                    return (
                        "⚠️ Encontré varios huéspedes con ese nombre. "
                        "Indícame el teléfono exacto:\n"
                        f"{suggestions}"
                    )
                return (
                    f"⚠️ No encontré un huésped con el nombre '{guest_id}'. "
                    "Indícame el teléfono exacto."
                )

        if not resolved_guest_id:
            return "⚠️ El guest_id no parece un teléfono válido. Indícame el número completo con prefijo."

        return f"[WA_DRAFT]|{resolved_guest_id}|{message}"

    return StructuredTool.from_function(
        name="enviar_mensaje_whatsapp",
        description=(
            "Genera un borrador de mensaje de texto directo por WhatsApp a un huésped, "
            "sin plantilla (proceso de confirmación requerido). "
            "Requiere el ID/phone del huésped (con prefijo de país) o su nombre exacto. "
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


def create_consulta_reserva_general_tool(memory_manager=None, chat_id: str = ""):
    async def _consulta_reserva_general(
        fecha_inicio: str,
        fecha_fin: str,
        property_id: Optional[int] = None,
        pms_property_id: Optional[int] = None,
        instance_url: Optional[str] = None,
        instance_id: Optional[str] = None,
        enrich_contact: bool = False,
    ) -> str:
        """
        Consulta folios/reservas en un rango de fechas vía MCP → n8n.
        """
        try:
            tools = await get_tools(server_name="DispoPreciosAgent")
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
        if property_id is not None:
            pms_property_id = property_id
        if instance_url:
            payload["instance_url"] = instance_url
        if memory_manager and chat_id:
            try:
                if instance_id:
                    memory_manager.set_flag(chat_id, "instance_id", str(instance_id))
                    memory_manager.set_flag(chat_id, "instance_hotel_code", str(instance_id))
                dynamic_instance_url = memory_manager.get_flag(chat_id, "instance_url")
                dynamic_property_id = memory_manager.get_flag(chat_id, "property_id")
            except Exception:
                dynamic_instance_url = None
                dynamic_property_id = None
            if dynamic_instance_url:
                payload["instance_url"] = dynamic_instance_url
            if dynamic_property_id is not None and pms_property_id is None:
                pms_property_id = dynamic_property_id

        if instance_id and not payload.get("instance_url"):
            inst_payload = fetch_instance_by_code(str(instance_id))
            inst_url = inst_payload.get("instance_url")
            if inst_url:
                payload["instance_url"] = inst_url
                if memory_manager and chat_id:
                    memory_manager.set_flag(chat_id, "instance_url", inst_url)

        if (not payload.get("instance_url")) and pms_property_id is not None:
            prop_payload = fetch_property_by_id(DEFAULT_PROPERTY_TABLE, pms_property_id)
            instance_id = prop_payload.get("instance_id")
            if instance_id:
                inst_payload = fetch_instance_by_code(str(instance_id))
                inst_url = inst_payload.get("instance_url")
                if inst_url:
                    payload["instance_url"] = inst_url
                    if memory_manager and chat_id:
                        memory_manager.set_flag(chat_id, "instance_url", inst_url)
                        memory_manager.set_flag(chat_id, "instance_id", instance_id)
                        memory_manager.set_flag(chat_id, "instance_hotel_code", instance_id)
        if pms_property_id is not None:
            payload["property_id"] = pms_property_id
            payload["pmsPropertyId"] = pms_property_id
        if "instance_url" not in payload or "property_id" not in payload:
            return (
                "Necesito el contexto de la instancia (instance_url y property_id) "
                "para consultar reservas. Indica el hotel o la instancia."
            )

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

            # 🔎 Enriquecer con contacto (teléfono/email) solo si se solicita explícitamente
            if enrich_contact and isinstance(simplified, list) and simplified:
                consulta_persona_tool = create_consulta_reserva_persona_tool(
                    memory_manager=memory_manager,
                    chat_id=chat_id,
                )

                def _extract_contact(detail: Any) -> tuple[Optional[str], Optional[str]]:
                    if not isinstance(detail, dict):
                        return None, None
                    phone = detail.get("partnerPhone") or detail.get("partner_phone")
                    email = detail.get("partnerEmail") or detail.get("partner_email")
                    partner = detail.get("partner") if isinstance(detail.get("partner"), dict) else {}
                    if not phone and isinstance(partner, dict):
                        phone = partner.get("phone") or partner.get("mobile")
                    if not email and isinstance(partner, dict):
                        email = partner.get("email")
                    return phone, email

                for item in simplified:
                    if not isinstance(item, dict):
                        continue
                    if item.get("partner_phone"):
                        continue
                    folio_id = item.get("folio_id") or item.get("folio")
                    if not folio_id:
                        continue
                    try:
                        detail_raw = await consulta_persona_tool.ainvoke(
                            {
                                "folio_id": str(folio_id),
                                "property_id": pms_property_id,
                                "instance_id": instance_id,
                            }
                        )
                        detail = json.loads(detail_raw) if isinstance(detail_raw, str) else detail_raw
                        phone, email = _extract_contact(detail)
                        if phone:
                            item["partner_phone"] = phone
                        if email and not item.get("partner_email"):
                            item["partner_email"] = email
                    except Exception as exc:
                        log.warning("No se pudo enriquecer folio_id=%s: %s", folio_id, exc)

            if memory_manager and chat_id:
                try:
                    cache_payload = {
                        "items": simplified,
                            "meta": {
                                "fecha_inicio": fecha_inicio,
                                "fecha_fin": fecha_fin,
                                "property_id": pms_property_id,
                                "instance_id": instance_id,
                                "instance_url": payload.get("instance_url"),
                            },
                        "stored_at": datetime.utcnow().isoformat(),
                    }
                    memory_manager.set_flag(chat_id, "superintendente_last_reservations", cache_payload)
                except Exception:
                    pass

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


def create_consulta_reserva_persona_tool(memory_manager=None, chat_id: str = ""):
    async def _consulta_reserva_persona(
        folio_id: str,
        property_id: Optional[int] = None,
        pms_property_id: Optional[int] = None,
        instance_url: Optional[str] = None,
        instance_id: Optional[str] = None,
    ) -> str:
        """
        Consulta los detalles de un folio específico vía MCP → n8n.
        """
        try:
            tools = await get_tools(server_name="DispoPreciosAgent")
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
        if property_id is not None:
            pms_property_id = property_id
        if instance_url:
            payload["instance_url"] = instance_url
        if memory_manager and chat_id:
            try:
                if instance_id:
                    memory_manager.set_flag(chat_id, "instance_id", str(instance_id))
                    memory_manager.set_flag(chat_id, "instance_hotel_code", str(instance_id))
                dynamic_instance_url = memory_manager.get_flag(chat_id, "instance_url")
                dynamic_property_id = memory_manager.get_flag(chat_id, "property_id")
            except Exception:
                dynamic_instance_url = None
                dynamic_property_id = None
            if dynamic_instance_url:
                payload["instance_url"] = dynamic_instance_url
            if dynamic_property_id is not None and pms_property_id is None:
                pms_property_id = dynamic_property_id

        if instance_id and not payload.get("instance_url"):
            inst_payload = fetch_instance_by_code(str(instance_id))
            inst_url = inst_payload.get("instance_url")
            if inst_url:
                payload["instance_url"] = inst_url
                if memory_manager and chat_id:
                    memory_manager.set_flag(chat_id, "instance_url", inst_url)

        if (not payload.get("instance_url")) and pms_property_id is not None:
            prop_payload = fetch_property_by_id(DEFAULT_PROPERTY_TABLE, pms_property_id)
            instance_id = prop_payload.get("instance_id")
            if instance_id:
                inst_payload = fetch_instance_by_code(str(instance_id))
                inst_url = inst_payload.get("instance_url")
                if inst_url:
                    payload["instance_url"] = inst_url
                    if memory_manager and chat_id:
                        memory_manager.set_flag(chat_id, "instance_url", inst_url)
                        memory_manager.set_flag(chat_id, "instance_id", instance_id)
                        memory_manager.set_flag(chat_id, "instance_hotel_code", instance_id)
        if pms_property_id is not None:
            payload["property_id"] = pms_property_id
            payload["pmsPropertyId"] = pms_property_id
        if "instance_url" not in payload or "property_id" not in payload:
            return (
                "Necesito el contexto de la instancia (instance_url y property_id) "
                "para consultar reservas. Indica el hotel o la instancia."
            )

        try:
            raw_response = await consulta_tool.ainvoke(payload)
            parsed = json.loads(raw_response) if isinstance(raw_response, str) else raw_response
            if memory_manager and chat_id and isinstance(parsed, dict):
                try:
                    memory_manager.set_flag(
                        chat_id,
                        "superintendente_last_reservation_detail",
                        {
                            "detail": parsed,
                            "stored_at": datetime.utcnow().isoformat(),
                        },
                    )
                except Exception:
                    pass
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
