"""Application settings, backups, exports and the cost guard switch."""

from __future__ import annotations

import contextlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..container import Container, EDITABLE_SETTINGS
from ..db import utcnow
from ..deps import ContainerDep, Principal, mutation_principal, require_admin
from ..hermes import provider_catalog
from ..hermes.provider_catalog import PAID_ACK

router = APIRouter(prefix="/settings", tags=["settings"])


def _describe_database(container: Container) -> str:
    """What the app is storing its own state in — phrased for a human.

    The DSN is masked: it can contain a password, and this value is rendered in the UI.
    """

    health = container.db.health()
    if not health.get("available", True):
        return f"unavailable ({health.get('error', 'unknown error')})"
    kind = health.get("dialect") or container.db.dialect
    if kind == "sqlite":
        return f"sqlite ({health.get('location') or container.settings.sqlite_path})"
    return f"{kind} ({health.get('location') or 'configured by CC_DATABASE_URL'})"


class SettingsBody(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


class FreeModeBody(BaseModel):
    enabled: bool
    allow_paid_fallback: bool = False
    acknowledgement: str = Field(default="", max_length=64)


@router.get("")
async def get_settings(principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep) -> dict:
    values = {key: container.db.get_setting(key, default) for key, (_, default, _) in EDITABLE_SETTINGS.items()}
    return {
        "values": values,
        "schema": [
            {
                "key": key,
                "label": label,
                "type": "bool" if isinstance(default, bool) else ("int" if isinstance(default, int) else "string"),
                "default": default,
            }
            for key, (label, default, _) in EDITABLE_SETTINGS.items()
        ],
        "read_only": {
            "home": str(container.settings.home),
            "database": _describe_database(container),
            "hermes_home": str(container.settings.hermes_home),
            "hermes_source": str(container.settings.hermes_source),
            "runtime_mode": container.settings.runtime_mode,
        },
    }


@router.put("")
async def update_settings(body: SettingsBody, principal: Annotated[Principal, Depends(mutation_principal)], container: ContainerDep) -> dict:
    unknown = [key for key in body.values if key not in EDITABLE_SETTINGS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown setting(s): {', '.join(sorted(unknown))}")
    for key, value in body.values.items():
        _, default, _ = EDITABLE_SETTINGS[key]
        if isinstance(default, bool) and not isinstance(value, bool):
            value = str(value).lower() in {"1", "true", "yes", "on"}
        elif isinstance(default, int) and not isinstance(default, bool):
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"{key} must be a whole number.") from None
        container.db.set_setting(key, value)
    container.audit("settings_updated", actor=principal.username, detail=",".join(sorted(body.values)))
    return {"ok": True, "values": {key: container.db.get_setting(key, default) for key, (_, default, _) in EDITABLE_SETTINGS.items()}}


@router.put("/free-mode")
async def set_free_mode(body: FreeModeBody, principal: Annotated[Principal, Depends(mutation_principal)], container: ContainerDep) -> dict:
    if body.allow_paid_fallback and body.acknowledgement.strip() != PAID_ACK:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "paid_acknowledgement_required",
                "message": (
                    "Allowing a paid model as the last fallback can cost real money. To confirm, send "
                    f'acknowledgement="{PAID_ACK}" and make sure a key for that provider is stored by you.'
                ),
            },
        )
    if not body.enabled and body.allow_paid_fallback:
        raise HTTPException(status_code=400, detail="FREE MODE must stay on while paid fallback is allowed; the fallback is simply the last resort.")
    container.db.set_setting("free_mode", body.enabled)
    container.db.set_setting("allow_paid_fallback", body.allow_paid_fallback)
    container.audit(
        "free_mode_changed",
        actor=principal.username,
        detail=f"free_mode={body.enabled} paid_fallback={body.allow_paid_fallback}",
        level="warning" if body.allow_paid_fallback else "info",
    )
    return {
        "ok": True,
        "free_mode": body.enabled,
        "allow_paid_fallback": body.allow_paid_fallback,
        "message": (
            "FREE MODE is on. Paid providers are refused unless you switch this off in Settings."
            if body.enabled
            else "FREE MODE is off. Requests may now reach whatever model you selected, including paid ones."
        ),
    }


@router.get("/backup")
async def download_backup(principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep) -> StreamingResponse:
    payload = container.export_configuration(include_secrets=False)
    body = json.dumps(payload, indent=2, default=str)
    stamp = utcnow().replace(":", "").replace("-", "")
    container.audit("config_exported", actor=principal.username)
    return StreamingResponse(
        iter([body]),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="control-center-backup-{stamp}.json"'},
    )


@router.post("/restore")
async def restore_backup(
    file: UploadFile,
    principal: Annotated[Principal, Depends(mutation_principal)],
    container: ContainerDep,
) -> dict:
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Backup files larger than 5 MB are refused.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="That file is not a Control Center backup (invalid JSON).") from None
    if not isinstance(payload, dict) or payload.get("kind") != "control-center-backup":
        raise HTTPException(status_code=400, detail="That JSON is not a Control Center backup (missing kind marker).")
    result = container.import_configuration(payload, actor=principal.username)
    container.audit("config_restored", actor=principal.username, detail=str(result), level="warning")
    return {"ok": True, "restored": result, "note": "Secrets are never included in a backup; re-enter keys after a restore."}


@router.get("/cost-guard")
async def cost_guard(principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep) -> dict:
    """Plain-language explanation of what this app will refuse to do."""

    return {
        "free_mode": bool(container.db.get_setting("free_mode", True)),
        "allow_paid_fallback": bool(container.db.get_setting("allow_paid_fallback", False)),
        "acknowledgement_phrase": PAID_ACK,
        "rules": [
            "With FREE MODE on, any request naming a PAID provider is refused before it leaves this app.",
            "A fallback chain needs two FREE entries before an optional paid one is even stored.",
            "The paid entry is never used automatically unless you set allow_paid_fallback yourself.",
            "No part of this app ships with a paid key, and none is created for you.",
        ],
        "tiers": provider_catalog.tier_legend(),
    }


# --------------------------------------------------------- first-run wizard
SETUP_STEP_ORDER = ["deployment", "model", "providers", "admin", "test", "launch"]


class SetupStepBody(BaseModel):
    step: str = Field(min_length=1, max_length=40)
    completed: bool = True
    data: dict[str, Any] = Field(default_factory=dict)


def _setup_steps(container: Container) -> dict[str, Any]:
    steps = container.db.get_setting("setup_steps", {}) or {}
    return steps if isinstance(steps, dict) else {}


def _free_options(container: Container) -> list[dict[str, Any]]:
    """The free routes, phrased for a first-time user."""

    summary = provider_catalog.free_route_summary(container)
    return [
        {
            "id": route["id"],
            "label": route["label"],
            "detail": route["detail"],
            "cost_label": route["cost_label"],
            "available": route["available"],
            "requires_key": route["requires_key"],
        }
        for route in summary.get("routes", [])
    ]


@router.get("/setup")
async def setup_state(principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep) -> dict:
    """Everything the install wizard needs, in one request."""

    from ..hermes.locate import detect_install
    from ..hermes.localcap import host_profile

    steps = _setup_steps(container)
    capability = container.db.get_setting("local_capability_summary", {}) or {}
    verdict = capability.get("verdict", {}) if isinstance(capability, dict) else {}
    install = detect_install(container.settings).to_dict()
    runtime = container.supervisor.status(probe=False)
    recommended = "local" if verdict.get("state") == "supported" else "cloud"
    return {
        "steps": {step: {"completed": bool((steps.get(step) or {}).get("completed"))} for step in SETUP_STEP_ORDER}
        | {key: value for key, value in steps.items() if key not in SETUP_STEP_ORDER},
        "step_order": SETUP_STEP_ORDER,
        "completed_count": sum(1 for step in SETUP_STEP_ORDER if (steps.get(step) or {}).get("completed")),
        "total": len(SETUP_STEP_ORDER),
        "next_step": next((step for step in SETUP_STEP_ORDER if not (steps.get(step) or {}).get("completed")), ""),
        "setup_completed": bool(container.db.get_setting("setup_completed", False)),
        "free_mode": bool(container.db.get_setting("free_mode", True)),
        "install": install,
        "runtime": {
            "state": runtime.get("state"),
            "pid": runtime.get("pid"),
            "healthy": runtime.get("healthy"),
            "last_error_hint": runtime.get("last_error_hint", ""),
            "mode": runtime.get("mode", ""),
        },
        "capability": {
            "host": host_profile(),
            "verdict": verdict,
        },
        "free_options": _free_options(container),
        "recommended_mode": recommended,
        "free_mode_statement": (
            "Nothing in this wizard requires a paid account. Paid providers are optional and only ever activate with a "
            "key you store yourself."
        ),
    }


@router.post("/setup")
async def record_setup_step(
    body: SetupStepBody,
    principal: Annotated[Principal, Depends(mutation_principal)],
    container: ContainerDep,
) -> dict:
    steps = _setup_steps(container)
    steps[body.step] = {
        "completed": bool(body.completed),
        "data": body.data,
        "at": utcnow(),
        "by": principal.username,
    }
    container.db.set_setting("setup_steps", steps)
    if body.step == "launch" and body.completed:
        container.db.set_setting("setup_completed", True)
    container.audit("setup_step", actor=principal.username, target=body.step, detail=f"completed={body.completed}")
    return {
        "ok": True,
        "step": body.step,
        "completed_count": sum(1 for step in SETUP_STEP_ORDER if (steps.get(step) or {}).get("completed")),
        "total": len(SETUP_STEP_ORDER),
        "setup_completed": bool(container.db.get_setting("setup_completed", False)),
    }


# ------------------------------------------------------------ about / export
@router.get("/about")
async def about(principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep) -> dict:
    """Version, licences and attribution — the honest provenance of this build."""

    import sys

    from ..hermes.locate import detect_install

    install = detect_install(container.settings)
    runtime = container.supervisor.status(probe=False)
    upstream_release, upstream_version = "", ""
    try:
        from ..hermes.client import HermesAPIError, HermesUnavailable

        status = await container.supervisor.client().get("/api/status", timeout=15)
        upstream_release = str((status or {}).get("release_date") or "")
        upstream_version = str((status or {}).get("version") or "")
    except (HermesAPIError, HermesUnavailable):
        upstream_release = ""
    return {
        "control_center": {
            "name": "Hermes Agent Control Center",
            "version": container.settings.version,
            "license": "MIT",
            "author": "built on top of the open-source Hermes Agent",
            "python": sys.version.split()[0],
        },
        "upstream": {
            "project": "Hermes Agent",
            "author": "Nous Research",
            "license": "MIT",
            "source": "https://github.com/NousResearch/hermes-agent",
            "docs": "https://hermes-agent.nousresearch.com/",
            "path": str(container.settings.hermes_source),
            "commit": install.commit,
            "commit_date": install.commit_date,
            "declared_version": install.version,
            "release_date": upstream_release,
            "reported_version": upstream_version,
            "license_present": install.license_present,
            "interpreter": runtime.get("python") or install.selected_python,
            "interpreter_version": runtime.get("python_version") or install.selected_python_version,
            "install_layout": "upstream checkout (editable install); the agent core is not vendored or forked",
        },
        "strategy": {
            "order": ["UPSTREAM HERMES", "Integration Layer", "Backend", "Web UI"],
            "rule": (
                "Hermes stays the upstream checkout. The Control Center talks to it over its own HTTP API and the "
                "gateway CLI; it does not vendor or fork the agent's core."
            ),
        },
        "paths": {
            "state": str(container.settings.home),
            "hermes_home": str(container.settings.hermes_home),
            "database": _describe_database(container),
            "logs": str(container.settings.logs_dir),
            "backups": str(container.settings.backups_dir),
        },
        "notices": [
            "Hermes Agent is MIT-licensed software by Nous Research; this Control Center does not relicense it.",
            "Model providers are third parties: their free tiers, limits and prices are theirs to change.",
            "No API key or subscription ships with this project, and none is created on your behalf.",
        ],
    }


@router.get("/export")
async def export_configuration(
    principal: Annotated[Principal, Depends(require_admin)],
    container: ContainerDep,
    include_secrets: bool = False,
) -> dict:
    """Portable configuration JSON.

    ``include_secrets`` adds the vault's *ciphertext* rows, never readable keys: those
    can only be decrypted with the same master key on this machine, and plaintext is
    never sent to a browser.
    """

    payload = container.export_configuration(include_secrets=include_secrets)
    payload["secrets_policy"] = (
        "No readable API key is ever exported. With include_secrets=true the encrypted vault rows are included; they "
        "are useless without the master key in your CC_HOME."
    )
    container.audit("config_exported", actor=principal.username, detail=f"include_secrets={include_secrets}")
    return payload


@router.post("/import")
async def import_configuration(
    payload: dict[str, Any],
    principal: Annotated[Principal, Depends(mutation_principal)],
    container: ContainerDep,
) -> dict:
    if payload.get("kind") != "control-center-backup":
        raise HTTPException(status_code=400, detail="That JSON is not a Control Center export (missing kind marker).")
    result = container.import_configuration(payload, actor=principal.username)
    container.audit("config_imported", actor=principal.username, detail=str(result)[:300], level="warning")
    return {
        "ok": True,
        "restored": result,
        "message": "Configuration imported. Keys are not part of an export — re-enter any you need on the Keys page.",
    }


# ---------------------------------------------------------------- backups
def _backup_files(container: Container) -> list[dict[str, Any]]:
    directory = container.settings.backups_dir
    items: list[dict[str, Any]] = []
    if not directory.exists():
        return items
    for path in sorted(directory.iterdir(), key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True):
        if not path.is_file():
            continue
        info = path.stat()
        items.append(
            {
                "name": path.name,
                "size_bytes": info.st_size,
                "created_at": datetime.fromtimestamp(info.st_mtime, tz=timezone.utc).isoformat(),
            }
        )
    return items


@router.get("/backups")
async def list_backups(principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep) -> dict:
    files = _backup_files(container)
    return {
        "backups": files,
        "directory": str(container.settings.backups_dir),
        "note": (
            "A backup contains your configuration, chat history and a snapshot of the Control Center database. It never "
            "contains readable API keys or the vault master key."
        ),
    }


@router.post("/backups")
async def create_backup(principal: Annotated[Principal, Depends(mutation_principal)], container: ContainerDep) -> dict:
    stamp = utcnow().replace(":", "").replace("-", "")[:15]
    name = f"control-center-backup-{stamp}.zip"
    target = container.settings.backups_dir / name
    container.settings.ensure_dirs()
    payload = container.export_configuration(include_secrets=True)
    manifest = {
        "kind": "control-center-backup",
        "version": 1,
        "created_at": utcnow(),
        "app_version": container.settings.version,
        "contents": ["config.json", "control-center.db", "README.txt"],
        "secrets": "encrypted rows only — the vault master key is NOT included",
    }
    readme = (
        "Hermes Agent Control Center backup\r\n"
        "===================================\r\n\r\n"
        "config.json          settings, chat history and provider choices\r\n"
        "control-center.db    the application database (SQLite)\r\n"
        "manifest.json        what this archive is\r\n\r\n"
        "No readable API key is stored in this archive, and the vault master key\r\n"
        "(secret.key in your CC_HOME) is deliberately excluded. Keep that file safe\r\n"
        "separately: without it, stored keys cannot be decrypted after a restore.\r\n"
    )
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2, default=str))
        archive.writestr("config.json", json.dumps(payload, indent=2, default=str))
        archive.writestr("README.txt", readme)
        database_path = container.settings.sqlite_path
        if database_path and database_path.exists():
            archive.write(database_path, "control-center.db")
    with contextlib.suppress(OSError):
        target.chmod(0o600)
    container.audit("backup_created", actor=principal.username, target=name, detail=f"{target.stat().st_size} bytes")
    container.logs.system(f"Backup written to {target}.")
    return {"ok": True, "name": name, "size_bytes": target.stat().st_size, "path": str(target)}


@router.get("/backups/{name}")
async def download_backup(
    name: str,
    principal: Annotated[Principal, Depends(require_admin)],
    container: ContainerDep,
) -> StreamingResponse:
    safe = Path(name).name
    if safe != name or not safe:
        raise HTTPException(status_code=400, detail="Invalid backup name.")
    target = container.settings.backups_dir / safe
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"No backup named {safe}.")
    container.audit("backup_downloaded", actor=principal.username, target=safe)
    return StreamingResponse(
        iter([target.read_bytes()]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe}"'},
    )
