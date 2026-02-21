"""Registro ligero de plantillas de WhatsApp para BookAi.

Permite:
- Registrar plantillas por hotel + código + idioma.
- Convertir parámetros nominales a orden ordinal que espera Meta.
- Cargar definiciones desde un JSON opcional en disco.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

log = logging.getLogger("TemplateRegistry")


def _norm_lang(lang: Optional[str]) -> str:
    """Normaliza el código de idioma."""
    if not lang:
        return "es"
    return str(lang).split("-")[0].strip().lower() or "es"


def _norm_code(code: Optional[str]) -> str:
    """Normaliza códigos/ids de plantilla."""
    return (code or "").strip().lower()


def _canonical_template_code(
    raw_code: Optional[str],
    language: Optional[str],
    whatsapp_name: Optional[str],
) -> str:
    """
    Código lógico para resolver plantillas aunque en BD el `code` venga como
    `nombre__idioma` (ej. reserva_confirmation_...__es).
    """
    wa_name = _norm_code(whatsapp_name)
    if wa_name:
        return wa_name

    code = _norm_code(raw_code)
    lang = _norm_lang(language)
    # Compatibilidad con formatos históricos:
    # - nombre__es
    # - nombre_es
    # - nombre-es
    for suffix in (f"__{lang}", f"_{lang}", f"-{lang}"):
        if code.endswith(suffix):
            return code[: -len(suffix)]
    return code


def _norm_instance(instance_id: Optional[str]) -> Optional[str]:
    if instance_id is None:
        return None
    clean = str(instance_id).strip()
    return clean.upper() or None


def _norm_param_key(key: Any) -> str:
    return re.sub(r"\s+", "_", str(key or "").strip())


def _is_missing_column_error(exc: Exception, column: str) -> bool:
    """
    Detecta errores de columna inexistente en Supabase/Postgrest.
    Se usa un chequeo relajado porque la estructura del APIError puede variar.
    """
    code = getattr(exc, "code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if str(code) == "42703":  # undefined_column en Postgres
        return True

    text = str(getattr(exc, "message", "")) or str(exc)
    if hasattr(exc, "args") and exc.args:
        payload = exc.args[0]
        if isinstance(payload, dict):
            payload_code = payload.get("code") or payload.get("status")
            if str(payload_code) == "42703":
                return True
            text = str(payload)
        else:
            text = str(payload)
    return column in text and "does not exist" in text


def _extract_param_hints(data: Dict[str, Any]) -> Dict[str, str]:
    """
    Extrae metadatos de parámetros (labels/ayudas) desde la fila de Supabase.
    Soporta estructuras flexibles: dict plano, lista de dicts o campos conocidos.
    """
    candidate = (
        data.get("parameter_hints")
        or data.get("param_hints")
        or data.get("parameters_info")
        or data.get("parameters_meta")
        or data.get("parameters_labels")
        or data.get("parameters")
        or data.get("params")
    )

    hints: Dict[str, str] = {}

    def _pick_label(val: Any) -> Optional[str]:
        if isinstance(val, str):
            return val.strip() or None
        if isinstance(val, dict):
            return (
                val.get("label")
                or val.get("title")
                or val.get("description")
                or val.get("hint")
                or val.get("help")
            )
        return None

    if isinstance(candidate, dict):
        for key, val in candidate.items():
            name = _norm_param_key(key)
            if not name:
                continue
            label = _pick_label(val)
            if label:
                hints[name] = str(label).strip()
    elif isinstance(candidate, list):
        for item in candidate:
            if not isinstance(item, dict):
                continue
            name = _norm_param_key(item.get("name") or item.get("key") or item.get("code"))
            if not name:
                continue
            label = _pick_label(item)
            if label:
                hints[name] = str(label).strip()

    # Intenta extraer desde la estructura de Meta si no se obtuvo nada
    if not hints:
        components = data.get("components") or []
        for comp in components:
            if not isinstance(comp, dict):
                continue
            example = comp.get("example") or {}
            params = example.get("body_text_named_params") or example.get("header_text_named_params") or []
            for p in params:
                if not isinstance(p, dict):
                    continue
                name = _norm_param_key(p.get("param_name") or p.get("name"))
                if not name:
                    continue
                label = _pick_label(p) or name
                hints[name] = str(label).strip()

    return hints


def _extract_template_text(data: Dict[str, Any]) -> Optional[str]:
    """
    Intenta extraer el texto base de la plantilla desde la fila de Supabase.
    Soporta múltiples campos y estructura components (Meta).
    """
    for key in (
        "content",
        "body",
        "body_text",
        "message",
        "template_text",
        "template_body",
    ):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    components = data.get("components") or []
    if isinstance(components, list):
        parts: List[str] = []
        for comp in components:
            if not isinstance(comp, dict):
                continue
            text = comp.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            comp_type = str(comp.get("type") or "").upper()
            if comp_type and comp_type != "BODY":
                parts.append(f"{comp_type}: {text.strip()}")
            else:
                parts.append(text.strip())
        if parts:
            return "\n".join(parts)

    return None


@dataclass
class TemplateDefinition:
    """Definición de plantilla WhatsApp."""

    code: str
    language: str = "es"
    instance_id: Optional[str] = None
    whatsapp_name: Optional[str] = None
    parameter_order: List[str] = field(default_factory=list)
    parameter_format: str = "ORDINAL"  # ORDINAL o NAMED
    description: Optional[str] = None
    active: bool = True
    parameter_hints: Dict[str, str] = field(default_factory=dict)
    content: Optional[str] = None
    components: List[Dict[str, Any]] = field(default_factory=list)

    def key(self) -> str:
        return TemplateRegistry.build_key(
            instance_id=self.instance_id,
            template_code=self.code,
            language=self.language,
        )

    def to_ordinal_params(self, provided: Dict[str, Any] | None) -> List[str]:
        """
        Convierte parámetros nominales en una lista ordinal en el orden definido.
        - Rellena huecos con cadenas vacías para mantener posiciones.
        - Añade parámetros extra al final respetando el orden de entrada.
        """
        provided = provided or {}
        ordered: List[str] = []
        seen = set()

        for key in self.parameter_order:
            ordered.append("" if provided.get(key) is None else str(provided.get(key)))
            seen.add(key)

        for key, val in provided.items():
            if key in seen:
                continue
            ordered.append("" if val is None else str(val))

        return ordered

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TemplateDefinition":
        hints = _extract_param_hints(data)
        order = list(data.get("parameter_order") or [])
        if not order and hints:
            order = list(hints.keys())
        raw_code = data.get("code")
        raw_language = data.get("language")
        raw_whatsapp_name = data.get("whatsapp_name")
        canonical_code = _canonical_template_code(raw_code, raw_language, raw_whatsapp_name)
        param_format_raw = str(data.get("parameter_format", "") or "").strip().upper()
        if not param_format_raw:
            # Si no viene especificado pero hay hints/orden, asumimos NAMED (nuevo formato de Meta).
            param_format_raw = "NAMED" if order or hints else "ORDINAL"
        return cls(
            code=canonical_code,
            language=_norm_lang(raw_language),
            instance_id=_norm_instance(data.get("instance_id")),
            whatsapp_name=(raw_whatsapp_name or canonical_code or raw_code or "").strip(),
            parameter_order=order,
            description=data.get("description"),
            active=bool(data.get("active", True)),
            parameter_format=param_format_raw or "ORDINAL",
            parameter_hints=hints,
            content=_extract_template_text(data),
            components=list(data.get("components") or []) if isinstance(data.get("components"), list) else [],
        )

    def get_param_label(self, name: str) -> str:
        key = _norm_param_key(name)
        return self.parameter_hints.get(key) or name

    def build_meta_parameters(self, provided: Dict[str, Any] | None) -> List[Any]:
        """
        Devuelve la lista de parámetros listos para Meta:
        - Si parameter_format == NAMED → [{type,text,parameter_name}, ...]
        - Si ORDINAL → lista ordenada (strings) siguiendo parameter_order
        """
        provided = provided or {}
        if self.parameter_format == "NAMED":
            ordered: List[Dict[str, Any]] = []
            seen = set()
            for name in self.parameter_order:
                val = provided.get(name)
                ordered.append(
                    {
                        "type": "text",
                        "parameter_name": name,
                        "text": "" if val is None else str(val),
                    }
                )
                seen.add(name)

            for name, val in provided.items():
                if name in seen:
                    continue
                ordered.append(
                    {
                        "type": "text",
                        "parameter_name": name,
                        "text": "" if val is None else str(val),
                    }
                )
            return ordered

            # Para ORDINAL mantenemos compatibilidad
        return self.to_ordinal_params(provided)

    def render_content(self, provided: Dict[str, Any] | None) -> Optional[str]:
        """
        Rellena el texto base de la plantilla usando los parametros disponibles.
        Soporta placeholders tipo {{1}} para ordinal y {{nombre}} para named.
        """
        base = (self.content or "").strip()
        if not base:
            return None

        text = base
        provided = provided or {}

        def _replace(token: str, value: Any) -> None:
            nonlocal text
            pattern = r"\{\{\s*" + re.escape(token) + r"\s*\}\}"
            text = re.sub(pattern, "" if value is None else str(value), text)

        if self.parameter_format == "NAMED":
            ordered_keys = list(self.parameter_order)
            for key in provided.keys():
                if key not in ordered_keys:
                    ordered_keys.append(key)
            for key in ordered_keys:
                _replace(key, provided.get(key))
        else:
            values = self.to_ordinal_params(provided)
            for idx, value in enumerate(values, start=1):
                _replace(str(idx), value)

        return text

    def render_fallback_summary(self, provided: Dict[str, Any] | None) -> Optional[str]:
        """
        Genera un resumen minimo a partir de parametros si no hay content.
        Ayuda a mantener contexto cuando la plantilla no trae texto base.
        """
        provided = provided or {}
        if not provided:
            return None

        lines: List[str] = []
        ordered_keys = list(self.parameter_order or [])
        for key in provided.keys():
            if key not in ordered_keys:
                ordered_keys.append(key)

        for key in ordered_keys:
            val = provided.get(key)
            if val is None or val == "":
                continue
            label = self.get_param_label(key)
            lines.append(f"{label}: {val}")

        if not lines:
            return None
        return "\n".join(lines)


class TemplateRegistry:
    """Registro en memoria de plantillas (fuente Supabase)."""

    def __init__(self, templates: Iterable[TemplateDefinition] | None = None) -> None:
        self._templates: Dict[str, TemplateDefinition] = {}
        for tpl in templates or []:
            self.register(tpl)

    # ------------------------------------------------------------------
    @staticmethod
    def build_key(instance_id: Optional[str], template_code: str, language: str | None) -> str:
        return f"{_norm_instance(instance_id) or '*'}|{_norm_code(template_code)}|{_norm_lang(language)}"

    # ------------------------------------------------------------------
    @classmethod
    def from_supabase(cls, supabase_client, table: str = "whatsapp_templates") -> "TemplateRegistry":
        registry = cls()
        registry.load_supabase(supabase_client, table=table)
        return registry

    # ------------------------------------------------------------------
    def load_supabase(self, supabase_client, table: str = "whatsapp_templates") -> None:
        """Carga definiciones desde Supabase."""
        if not supabase_client:
            log.warning("TemplateRegistry: supabase_client no disponible.")
            return
        try:
            def _base_query():
                return supabase_client.table(table).select("*").limit(1000)

            query = _base_query()
            try:
                resp = query.eq("active", True).execute()
            except Exception as exc:
                if _is_missing_column_error(exc, "active"):
                    log.info("TemplateRegistry: la columna 'active' no existe en %s; se carga sin filtro.", table)
                    resp = _base_query().execute()
                else:
                    raise
            data = resp.data or []
            if data and any(isinstance(item, dict) and "active" in item for item in data):
                # Si la columna existe, filtramos localmente para mantener compatibilidad.
                data = [item for item in data if item.get("active", True)]
            loaded = 0
            for item in data:
                tpl = TemplateDefinition.from_dict(item)
                self.register(tpl)
                loaded += 1
            log.info("TemplateRegistry: %s plantilla(s) cargadas desde Supabase (%s)", loaded, table)
        except Exception as exc:
            log.error("TemplateRegistry: error cargando desde Supabase: %s", exc, exc_info=True)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def register(self, template: TemplateDefinition) -> None:
        if not template.active:
            return
        key = template.key()
        self._templates[key] = template

    # ------------------------------------------------------------------
    def resolve(self, instance_id: Optional[str], template_code: str, language: str | None = None) -> Optional[TemplateDefinition]:
        """Busca plantilla por hotel + código + idioma con varios fallbacks."""
        lang = _norm_lang(language)
        hotel = _norm_instance(instance_id)
        code = _norm_code(template_code)
        canonical_code = _canonical_template_code(code, lang, None)
        candidate_codes: List[str] = [code]
        if canonical_code and canonical_code not in candidate_codes:
            candidate_codes.append(canonical_code)

        candidates: List[str] = []
        for candidate_code in candidate_codes:
            candidates.extend(
                [
                    self.build_key(hotel, candidate_code, lang),
                    self.build_key(hotel, candidate_code, None),
                    self.build_key(None, candidate_code, lang),
                    self.build_key(None, candidate_code, None),
                ]
            )

        for key in candidates:
            tpl = self._templates.get(key)
            if tpl and tpl.active:
                return tpl
        return None

    # ------------------------------------------------------------------
    def list_templates(self) -> List[TemplateDefinition]:
        return list(self._templates.values())
