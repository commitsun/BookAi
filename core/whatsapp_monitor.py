"""Ejecución mínima de monitorización reutilizando healthchecks ya existentes."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from types import SimpleNamespace
from typing import Iterable

from agents.supervisor_input_agent import SupervisorInputAgent
from agents.supervisor_output_agent import SupervisorOutputAgent
from channels_wrapper.manager import ChannelManager
from core.config import Settings
from core.instance_context import ensure_instance_credentials, fetch_instance_by_code
from core.memory_manager import MemoryManager
from core.whatsapp_healthcheck import (
    build_whatsapp_healthcheck_response,
    detect_whatsapp_healthcheck,
    execute_whatsapp_healthcheck,
)

log = logging.getLogger("WhatsAppMonitor")

_VALID_CHECKS = {"basic", "ia", "complete"}


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if str(item or "").strip()]


def _clean_phone(value: str) -> str | None:
    digits = re.sub(r"\D", "", str(value or "")).strip()
    return digits or None


def _normalize_check(value: str) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None

    detected = detect_whatsapp_healthcheck(raw)
    if detected:
        path = str(detected.get("path") or "").strip().lower()
        if path in _VALID_CHECKS:
            return path

    normalized = raw.strip().lower()
    if normalized in _VALID_CHECKS:
        return normalized
    return None


def resolve_monitor_targets(values: Iterable[str] | None = None) -> list[str]:
    raw_values = list(values) if values is not None else _split_csv(Settings.WHATSAPP_MONITOR_TARGET_NUMBERS)
    targets: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        phone = _clean_phone(raw)
        if not phone or phone in seen:
            continue
        seen.add(phone)
        targets.append(phone)
    return targets


def resolve_monitor_checks(values: Iterable[str] | None = None) -> list[str]:
    raw_values = list(values) if values is not None else _split_csv(Settings.WHATSAPP_MONITOR_CHECKS)
    checks: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        check = _normalize_check(raw)
        if not check or check in seen:
            continue
        seen.add(check)
        checks.append(check)
    return checks


def resolve_monitor_instance_id(instance_id: str | None = None) -> str | None:
    return str(instance_id or Settings.WHATSAPP_MONITOR_INSTANCE_ID or "").strip() or None


def prepare_monitor_context(
    state,
    *,
    instance_id: str | None = None,
) -> tuple[str | None, dict]:
    resolved_instance_id = resolve_monitor_instance_id(instance_id=instance_id)
    if not resolved_instance_id:
        return None, {}

    payload = fetch_instance_by_code(resolved_instance_id) or {}
    context_id = f"whatsapp-monitor:{resolved_instance_id}"

    if state.memory_manager:
        state.memory_manager.set_flag(context_id, "instance_id", resolved_instance_id)
        state.memory_manager.set_flag(context_id, "instance_hotel_code", resolved_instance_id)
        ensure_instance_credentials(state.memory_manager, context_id)

    return context_id, payload


def build_monitor_state():
    memory_manager = MemoryManager()
    return SimpleNamespace(
        memory_manager=memory_manager,
        supervisor_input=SupervisorInputAgent(memory_manager=memory_manager),
        supervisor_output=SupervisorOutputAgent(memory_manager=memory_manager),
        channel_manager=ChannelManager(memory_manager=memory_manager),
    )


async def run_whatsapp_monitor(
    state,
    *,
    targets: Iterable[str] | None = None,
    checks: Iterable[str] | None = None,
    instance_id: str | None = None,
) -> dict:
    resolved_targets = resolve_monitor_targets(targets)
    resolved_checks = resolve_monitor_checks(checks)
    effective_instance_id = str(instance_id or Settings.WHATSAPP_MONITOR_INSTANCE_ID or "").strip() or None
    context_id, instance_payload = prepare_monitor_context(
        state,
        instance_id=effective_instance_id,
    )

    if not resolved_targets:
        raise ValueError("WHATSAPP_MONITOR_TARGET_NUMBERS no tiene números válidos configurados")
    if not resolved_checks:
        raise ValueError("WHATSAPP_MONITOR_CHECKS no tiene comprobaciones válidas configuradas")

    if effective_instance_id and not context_id:
        raise ValueError("No se pudo resolver instance_id para WHATSAPP_MONITOR_INSTANCE_ID")

    check_results: list[dict] = []
    for path in resolved_checks:
        log.info("whatsapp monitor check started path=%s", path)
        if path == "basic":
            message = build_whatsapp_healthcheck_response("basic", has_real_meta_inbound=False)
        else:
            message = await execute_whatsapp_healthcheck(
                state,
                path,
                has_real_meta_inbound=False,
                chat_id=None,
                trace_id=f"monitor:{path}",
            )
        ok = str(message or "").strip().startswith("✅")
        check_results.append(
            {
                "path": path,
                "ok": ok,
                "message": message,
            }
        )
        log.info("whatsapp monitor check result path=%s ok=%s", path, ok)

    dispatches: list[dict] = []
    for target in resolved_targets:
        for result in check_results:
            path = result["path"]
            message = result["message"]
            log.info("whatsapp monitor outbound requested target=%s path=%s", target, path)
            await state.channel_manager.send_message(
                target,
                message,
                channel="whatsapp",
                context_id=context_id,
                backup_role="bookai",
                raise_on_error=True,
            )
            dispatches.append(
                {
                    "target": target,
                    "path": path,
                    "requested": True,
                    "message": message,
                    "ok": bool(result["ok"]),
                }
            )
            log.info("whatsapp monitor outbound call completed target=%s path=%s", target, path)

    return {
        "targets": resolved_targets,
        "checks": check_results,
        "dispatches": dispatches,
        "overall_ok": all(item["ok"] for item in check_results),
        "instance_id": str((instance_payload or {}).get("instance_id") or "").strip() or None,
        "source_whatsapp_number": str((instance_payload or {}).get("whatsapp_number") or "").strip() or None,
        "source_whatsapp_phone_id": str((instance_payload or {}).get("whatsapp_phone_id") or "").strip() or None,
    }


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        force=True,
    )


def _parse_csv_argument(value: str | None) -> list[str] | None:
    if value is None:
        return None
    items = [item.strip() for item in str(value or "").split(",") if item.strip()]
    return items or []


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ejecuta el monitor de WhatsApp reutilizando los healthchecks existentes de BookAI.",
    )
    parser.add_argument(
        "--checks",
        default=None,
        help="CSV de checks a ejecutar (basic, ia, complete o keywords completas).",
    )
    parser.add_argument(
        "--targets",
        default=None,
        help="CSV de números destino. Si no se indica, usa WHATSAPP_MONITOR_TARGET_NUMBERS.",
    )
    parser.add_argument(
        "--instance-id",
        default=None,
        help="instance_id explícito para elegir la línea de WhatsApp origen del monitor.",
    )
    return parser


async def _run_from_cli(args: argparse.Namespace) -> int:
    state = build_monitor_state()
    result = await run_whatsapp_monitor(
        state,
        targets=_parse_csv_argument(args.targets),
        checks=_parse_csv_argument(args.checks),
        instance_id=args.instance_id,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("overall_ok", False):
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run_from_cli(args))
    except Exception as exc:
        log.error("whatsapp monitor execution failed: %s", exc, exc_info=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
