"""Health, status, diagnostics and activity — the "is anything wrong?" endpoints.

``/health`` and ``/status`` are public because a load balancer or an uptime probe
has to reach them; they deliberately expose nothing sensitive (no paths, no keys,
no usernames). Everything richer requires a session.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from ..container import Container
from ..db import utcnow
from ..deps import ContainerDep, Principal, current_principal
from ..hermes.client import HermesAPIError, HermesUnavailable
from ..hermes.locate import detect_install
from ..hermes.provider_catalog import free_route_summary
from ..hermes.supervisor import STATE_ATTACHED, STATE_DISABLED, STATE_EXTERNAL, STATE_RUNNING
from ..security import get_redactor

router = APIRouter(tags=["system"])

_STARTED_AT = time.time()


# ------------------------------------------------------------------- helpers
def _host_metrics() -> dict:
    """CPU/RAM/disk without requiring psutil (it is optional)."""

    metrics: dict = {
        "cpu_count": os.cpu_count() or 1,
        "load_average": None,
        "memory_total_bytes": None,
        "memory_available_bytes": None,
        "memory_used_percent": None,
        "disk_total_bytes": None,
        "disk_free_bytes": None,
    }
    try:
        load = os.getloadavg()
        metrics["load_average"] = [round(value, 2) for value in load]
    except (AttributeError, OSError):
        pass
    try:
        usage = shutil.disk_usage(str(os.path.expanduser("~")))
        metrics["disk_total_bytes"] = usage.total
        metrics["disk_free_bytes"] = usage.free
    except OSError:
        pass
    try:
        import psutil  # optional

        memory = psutil.virtual_memory()
        metrics["memory_total_bytes"] = memory.total
        metrics["memory_available_bytes"] = memory.available
        metrics["memory_used_percent"] = memory.percent
    except Exception:  # noqa: BLE001 - psutil is optional
        try:
            with open("/proc/meminfo", encoding="ascii", errors="ignore") as handle:
                info = {}
                for line in handle:
                    key, _, value = line.partition(":")
                    info[key.strip()] = int(value.strip().split()[0]) * 1024
            metrics["memory_total_bytes"] = info.get("MemTotal")
            metrics["memory_available_bytes"] = info.get("MemAvailable")
            total = info.get("MemTotal") or 0
            available = info.get("MemAvailable") or 0
            if total:
                metrics["memory_used_percent"] = round((total - available) / total * 100, 1)
        except OSError:
            pass
    return metrics


async def _model_summary(container: Container) -> dict:
    client = container.supervisor.client()
    try:
        info = await client.get("/api/model/info", timeout=15)
    except (HermesAPIError, HermesUnavailable) as exc:
        return {"available": False, "error": exc.human_detail() if isinstance(exc, HermesAPIError) else str(exc)}
    provider = str(info.get("provider") or "")
    model = str(info.get("model") or "")
    from ..hermes.provider_catalog import classify_provider

    classification = classify_provider(provider) if provider else {"billing": "unknown", "label": "not selected", "cost_note": ""}
    return {
        "available": bool(model),
        "model": model,
        "provider": provider,
        "billing": classification["billing"],
        "provider_label": classification["label"],
        "cost_note": classification["cost_note"],
        "context_length": info.get("effective_context_length") or info.get("config_context_length") or 0,
        "capabilities": info.get("capabilities") or {},
    }


async def _upstream_status(container: Container) -> dict:
    client = container.supervisor.client()
    try:
        payload = await client.get("/api/status", timeout=20)
        redactor = get_redactor()
        return {
            "reachable": True,
            "hermes_version": payload.get("version") or "",
            "hermes_release_date": payload.get("release_date") or "",
            "config_version": payload.get("config_version"),
            "gateway_running": bool(payload.get("gateway_running")),
            "gateway_state": payload.get("gateway_state"),
            "gateway_platforms": payload.get("gateway_platforms") or {},
            "active_sessions": payload.get("active_sessions", 0),
            "components": payload.get("components") or {},
            "overall": redactor.redact(payload.get("overall") or ""),
            "install_id": payload.get("install_id") or "",
        }
    except (HermesAPIError, HermesUnavailable) as exc:
        return {
            "reachable": False,
            "error": exc.human_detail() if isinstance(exc, HermesAPIError) else str(exc),
        }


async def build_status(container: Container, *, public: bool = False) -> dict:
    supervisor_status = container.supervisor.status(probe=True)
    upstream = await _upstream_status(container)
    model = await _model_summary(container) if upstream.get("reachable") else {"available": False, "error": "runtime not reachable"}
    db_health = container.db.health()
    free = free_route_summary(container)

    payload: dict = {
        "generated_at": utcnow(),
        "service": {
            "name": "Hermes Agent Control Center",
            "version": container.settings.version,
            "uptime_seconds": round(time.time() - _STARTED_AT, 1),
            "python": sys.version.split()[0],
        },
        "runtime": {
            "state": supervisor_status["state"],
            "mode": supervisor_status["mode"],
            "attached": supervisor_status.get("attached", False),
            "healthy": bool(supervisor_status["healthy"]),
            "pid": supervisor_status.get("pid"),
            "port": supervisor_status.get("port"),
            "uptime_seconds": round(supervisor_status.get("uptime_seconds") or 0, 1),
            "restarts": supervisor_status.get("restarts", 0),
            "auto_restart": supervisor_status.get("auto_restart", False),
            "last_error": supervisor_status.get("last_error", ""),
            "last_error_hint": supervisor_status.get("last_error_hint", ""),
            "python": supervisor_status.get("python", ""),
            "python_version": supervisor_status.get("python_version", ""),
        },
        "hermes": upstream,
        "model": model,
        "free_mode": {
            "enabled": container.free_mode,
            "paid_unlocked": container.paid_unlocked,
            "no_cost_so_far": True,
            "active_route": model.get("provider_label") if model.get("provider") else "none selected",
            "cost_posture": model.get("cost_note") or "No cost until you add a paid key and unlock paid usage.",
            "free_routes": free,
        },
        "database": db_health,
        "storage": {
            "cc_home": str(container.settings.home) if not public else "",
            "database_size_bytes": container.db.size_bytes(),
            "log_file": str(container.settings.logs_dir / "control-center.log") if not public else "",
        },
        "local_inference": container.db.get_setting("local_capability_summary", {}),
        "host": _host_metrics(),
        "counters": {
            "audit_events": (container.db.query_one("SELECT COUNT(*) AS n FROM audit_events") or {}).get("n", 0),
            "log_events": (container.db.query_one("SELECT COUNT(*) AS n FROM log_events") or {}).get("n", 0),
            "chat_threads": (container.db.query_one("SELECT COUNT(*) AS n FROM chat_threads WHERE archived = 0") or {}).get("n", 0),
            "runtime_events": (container.db.query_one("SELECT COUNT(*) AS n FROM runtime_events") or {}).get("n", 0),
            "users": (container.db.query_one("SELECT COUNT(*) AS n FROM users") or {}).get("n", 0),
        },
        "checks": {
            "database": bool(db_health.get("available")),
            "runtime_process": supervisor_status["state"] in {STATE_RUNNING, STATE_ATTACHED, STATE_EXTERNAL},
            "runtime_api": bool(upstream.get("reachable")),
            "model_selected": bool(model.get("available")),
            "encrypted_key_storage": container.secret_box.available,
            "runtime_management_disabled": supervisor_status["state"] == STATE_DISABLED,
        },
    }
    return payload


@router.get("/status")
async def status_public(container: ContainerDep) -> dict:
    return await build_status(container, public=True)


@router.get("/status/detail")
async def status_detail(
    principal: Annotated[Principal, Depends(current_principal)],
    container: ContainerDep,
) -> dict:
    return await build_status(container, public=False)


@router.get("/health")
async def health(container: ContainerDep) -> dict:
    supervisor_status = container.supervisor.status(probe=False)
    db_health = container.db.health()
    return {
        "ok": bool(db_health.get("available")),
        "database": bool(db_health.get("available")),
        "runtime_state": supervisor_status["state"],
        "runtime_healthy": bool(supervisor_status["healthy"]),
        "attached": supervisor_status.get("attached", False),
        "version": container.settings.version,
    }


@router.get("/host")
async def host_metrics(principal: Annotated[Principal, Depends(current_principal)]) -> dict:
    return {"host": _host_metrics(), "generated_at": utcnow()}


@router.get("/activity")
async def activity(
    principal: Annotated[Principal, Depends(current_principal)],
    container: ContainerDep,
    limit: int = Query(25, ge=1, le=200),
) -> dict:
    audit = container.db.query(
        "SELECT ts, actor, action, target, detail, level FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
    )
    runtime_events = container.db.query(
        "SELECT ts, event, detail, state, pid FROM runtime_events ORDER BY id DESC LIMIT ?", (limit,)
    )
    logs = container.logs.tail(limit)
    chat = container.db.query(
        "SELECT thread_id, title, updated_at, model FROM chat_threads WHERE archived = 0 ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    )
    return {"audit": audit, "runtime_events": runtime_events, "logs": logs, "threads": chat}


@router.get("/diagnostics")
async def diagnostics(principal: Annotated[Principal, Depends(current_principal)], container: ContainerDep) -> dict:
    """Every check that decides whether this install can actually work.

    Each entry is something we measured — never a guess — and each failure carries a
    hint, because "FAIL" without a next step is not a diagnosis.
    """

    install = detect_install(container.settings)
    runtime = container.supervisor.status(probe=True)
    db_health = container.db.health()
    upstream = await _upstream_status(container)
    model = await _model_summary(container) if upstream.get("reachable") else {"available": False}
    free = free_route_summary(container)
    storage_free = 0
    try:
        storage_free = shutil.disk_usage(str(container.settings.home)).free
    except OSError:
        pass

    checks: list[dict] = []

    def add(identifier: str, label: str, ok: bool, detail: str, *, hint: str = "", severity: str = "error") -> None:
        checks.append(
            {
                "id": identifier,
                "label": label,
                "ok": bool(ok),
                "detail": detail,
                "hint": hint,
                "severity": "info" if ok else severity,
            }
        )

    add(
        "hermes-source",
        "Hermes source checkout",
        install.is_hermes_source,
        f"{install.source_dir} — {install.source_reason or 'not found'}",
        hint="Clone https://github.com/NousResearch/hermes-agent and set CC_HERMES_SOURCE to it.",
    )
    add(
        "interpreter",
        "Python with Hermes dependencies",
        bool(install.selected_python and any(c.has_hermes_deps for c in install.python_candidates)),
        f"{install.selected_python or 'none detected'} {install.selected_python_version}".strip(),
        hint="Diagnostics can install the dependencies into the detected interpreter.",
    )
    add(
        "license",
        "Upstream licence preserved",
        install.license_present,
        "LICENSE found in the Hermes checkout" if install.license_present else "LICENSE missing from the checkout",
        hint="Hermes Agent is MIT-licensed software by Nous Research; restore the file before redistributing.",
    )
    add(
        "runtime-process",
        "Hermes runtime process",
        runtime.get("state") in {STATE_RUNNING, STATE_ATTACHED, STATE_EXTERNAL},
        f"state={runtime.get('state')} pid={runtime.get('pid') or '—'} mode={runtime.get('mode')}",
        hint=runtime.get("last_error_hint") or "Start the runtime from the Dashboard page.",
    )
    add(
        "runtime-api",
        "Hermes dashboard API",
        bool(upstream.get("reachable")),
        "answered /api/status" if upstream.get("reachable") else str(upstream.get("error") or "no answer"),
        hint="The Control Center adopts a runtime it did not start only when it publishes a rendezvous record.",
    )
    add(
        "database",
        "Application database",
        bool(db_health.get("available")),
        f"{db_health.get('dialect', container.db.dialect)} — {db_health.get('location', '')}",
        hint="SQLite is the zero-cost default; set CC_DATABASE_URL for PostgreSQL.",
    )
    add(
        "vault",
        "Encrypted key vault",
        bool(container.secret_box.available),
        (
            f"{container.secret_box.backend_name} — "
            f"{len(container.secrets.list_rows())} entr(ies) stored, ciphertext on disk"
            if container.secret_box.available
            else "unavailable: no encryption backend, so keys cannot be stored"
        ),
        hint="Without the vault, keys cannot be stored at rest and the app refuses to pretend otherwise.",
    )
    add(
        "model",
        "Model selected",
        bool(model.get("available")),
        (f"{model.get('provider')}/{model.get('model')}" if model.get("available") else "no model selected yet"),
        hint="Pick a free route on the Models page — the Nous free tier needs no API key.",
    )
    add(
        "free-route",
        "A free route exists",
        bool(free.get("available_count")),
        free.get("headline", ""),
        hint=(
            "Enable the Nous free tier, store a free provider key, or check the Local models page for this host's "
            "capability verdict."
        ),
    )
    gateway = container.gateway.status()
    add(
        "chat-gateway",
        "Chat gateway",
        bool(gateway.get("reachable")),
        f"state={gateway.get('state')} port={gateway.get('port')}",
        hint=gateway.get("last_error") or "Chat needs the gateway: press Enable on the Chat page.",
        severity="info" if gateway.get("state") != "error" else "error",
    )
    add(
        "scheduler",
        "In-process scheduler",
        True,
        "housekeeping runs in-process every 60 s; the agent's own schedules run inside Hermes, not in a browser tab",
        severity="info",
    )
    add(
        "storage",
        "State directory writable",
        os.access(container.settings.home, os.W_OK),
        f"{container.settings.home} — {storage_free // (1024 * 1024)} MB free",
        hint="The Control Center stores its database, logs and backups here.",
    )
    if runtime.get("state") == STATE_DISABLED:
        add(
            "runtime-disabled",
            "Runtime management",
            False,
            "CC_RUNTIME_MODE=disabled — this app will not start or stop Hermes here.",
            hint="Set CC_RUNTIME_MODE=dashboard (or external) to let the Control Center manage the runtime.",
            severity="info",
        )

    passed = sum(1 for check in checks if check["ok"])
    return {
        "generated_at": utcnow(),
        "checks": checks,
        "passed": passed,
        "total": len(checks),
        "blocking": [check["id"] for check in checks if not check["ok"] and check["severity"] != "info"],
        "runtime": runtime,
        "install": install.to_dict(),
        "database": db_health,
        "model": model,
        "free": free,
        "gateway": gateway,
        "verdict": (
            "Everything the Control Center can verify is in place."
            if passed == len(checks)
            else f"{len(checks) - passed} check(s) need attention — each one lists the fix."
        ),
    }
