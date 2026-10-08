"""Runtime lifecycle: start, stop, restart, autostart, install plan, log tail."""

from __future__ import annotations

import asyncio
import shlex
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..container import Container
from ..db import utcnow
from ..deps import ContainerDep, Principal, mutation_principal, require_admin
from ..hermes import rendezvous
from ..hermes.locate import detect_install
from ..hermes.supervisor import RuntimeStartError
from ..security import get_redactor

router = APIRouter(prefix="/runtime", tags=["runtime"])


class AutostartBody(BaseModel):
    enabled: bool
    auto_restart: bool | None = None


class BootstrapBody(BaseModel):
    extras: str = Field(default="web", max_length=64)
    confirm: bool = Field(default=False, description="must be true: this installs Python packages")
    interpreter: str = Field(default="", max_length=400)
    include_bridge: bool = Field(
        default=True,
        description="also install aiohttp, which upstream keeps in its [messaging] extra but the OpenAI-compatible API bridge needs",
    )


async def _start(container: Container) -> dict:
    try:
        await asyncio.to_thread(container.supervisor.start, wait=True)
    except RuntimeStartError as exc:
        raise HTTPException(status_code=503, detail={"error": "runtime_start_failed", "message": exc.message, "hint": exc.human_detail(), "log_tail": exc.log_tail}) from exc
    status = container.supervisor.status(probe=True)
    if status.get("attached"):
        container.logs.system("Adopted an already-running Hermes backend instead of starting a second one.")
    return status


@router.get("")
async def runtime_status(principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep) -> dict:
    status = container.supervisor.status(probe=True)
    install = detect_install(container.settings)
    return {
        "runtime": status,
        "install": install.to_dict(),
        "adoption": {
            "attempt": rendezvous.attempt_adopt().to_dict(),
            "state_dir": str(rendezvous.state_dir()),
            "explanation": (
                "Hermes allows one machine-level backend per user and publishes a rendezvous record with its session "
                "token. When that backend is alive the Control Center attaches to it instead of starting a second one."
            ),
        },
    }


@router.post("/start")
async def runtime_start(principal: Annotated[Principal, Depends(mutation_principal)], container: ContainerDep) -> dict:
    status = await _start(container)
    container.audit("runtime_start", actor=principal.username, detail=f"state={status.get('state')} pid={status.get('pid')}")
    return {"ok": True, "runtime": status}


@router.post("/stop")
async def runtime_stop(principal: Annotated[Principal, Depends(mutation_principal)], container: ContainerDep) -> dict:
    status = await asyncio.to_thread(container.supervisor.stop)
    container.audit("runtime_stop", actor=principal.username, level="warning")
    return {"ok": True, "runtime": status}


@router.post("/restart")
async def runtime_restart(principal: Annotated[Principal, Depends(mutation_principal)], container: ContainerDep) -> dict:
    try:
        await asyncio.to_thread(container.supervisor.restart)
    except RuntimeStartError as exc:
        raise HTTPException(status_code=503, detail={"error": "runtime_start_failed", "message": exc.message, "hint": exc.human_detail()}) from exc
    status = container.supervisor.status(probe=True)
    container.audit("runtime_restart", actor=principal.username, detail=f"pid={status.get('pid')}")
    return {"ok": True, "runtime": status}


@router.post("/autostart")
async def set_autostart(body: AutostartBody, principal: Annotated[Principal, Depends(mutation_principal)], container: ContainerDep) -> dict:
    container.db.set_setting("runtime_autostart", body.enabled)
    if body.auto_restart is not None:
        container.db.set_setting("runtime_auto_restart", body.auto_restart)
    container.audit("runtime_autostart", actor=principal.username, detail=f"enabled={body.enabled} auto_restart={body.auto_restart}")
    return {
        "ok": True,
        "autostart": body.enabled,
        "auto_restart": container.db.get_setting("runtime_auto_restart", container.settings.runtime_auto_restart),
        "note": "The Control Center starts the runtime when it boots. Crash auto-restart is capped at 3 attempts in 15 minutes.",
    }


@router.get("/logs")
async def runtime_logs(
    principal: Annotated[Principal, Depends(require_admin)],
    container: ContainerDep,
    lines: int = Query(200, ge=1, le=2000),
) -> dict:
    redactor = get_redactor()
    raw = container.supervisor.log_lines(lines)
    return {
        "lines": [redactor.redact(line) for line in raw],
        "file": str(container.supervisor.log_file),
        "count": len(raw),
    }


@router.delete("/logs")
async def clear_runtime_logs(principal: Annotated[Principal, Depends(mutation_principal)], container: ContainerDep) -> dict:
    path = container.supervisor.log_file
    if not path.exists():
        raise HTTPException(status_code=404, detail="No runtime log file yet.")
    path.write_text("", encoding="utf-8")
    container.audit("runtime_logs_cleared", actor=principal.username, level="warning")
    return {"ok": True, "file": str(path)}


@router.get("/install-plan")
async def install_plan(principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep) -> dict:
    """Exactly what would be executed to make Hermes runnable on this host."""

    install = detect_install(container.settings)
    interpreter = container.settings.hermes_python or install.selected_python
    source = container.settings.hermes_source
    from ..hermes.locate import BRIDGE_IMPORTS, has_bridge_dependencies

    commands: list[str] = []
    if interpreter:
        commands.append(f"{shlex.quote(interpreter)} -m pip install -e {shlex.quote(str(source))}[web]")
        if not has_bridge_dependencies(interpreter)[0]:
            commands.append(
                f"{shlex.quote(interpreter)} -m pip install {' '.join(BRIDGE_IMPORTS)}"
                "   # the agent's OpenAI-compatible API bridge; upstream ships it in the [messaging] extra"
            )
        commands.append(f"{shlex.quote(interpreter)} -m hermes_cli.main doctor")
    commands.append(f"bash {shlex.quote(str(source))}/setup-hermes.sh    # upstream installer (Linux/macOS)")
    return {
        "install": install.to_dict(),
        "interpreter": interpreter,
        "source": str(source),
        "hermes_home": str(container.settings.hermes_home),
        "bridge_dependencies": {
            "packages": list(BRIDGE_IMPORTS),
            "installed": has_bridge_dependencies(interpreter)[0] if interpreter else False,
            "detail": has_bridge_dependencies(interpreter)[1] if interpreter else "no interpreter detected",
            "why": "Chat with the agent goes through the agent's own OpenAI-compatible API server, which imports aiohttp.",
        },
        "commands": commands,
        "what_the_button_does": (
            "Runs only the first command (pip install into the detected interpreter). Nothing else is executed for you."
        ),
    }


@router.get("/bootstrap/plan")
async def bootstrap_plan(
    principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep
) -> dict:
    """The same plan the Diagnostics page shows, under the name that page asks for."""

    plan = await install_plan(principal=principal, container=container)
    plan["plan_version"] = 1
    plan["install_after"] = plan.get("install", {}).get("selected_python", "")
    return plan


@router.post("/bootstrap")
async def bootstrap(
    body: BootstrapBody,
    background: BackgroundTasks,
    principal: Annotated[Principal, Depends(mutation_principal)],
    container: ContainerDep,
) -> dict:
    """Install the upstream dependencies into the interpreter Hermes will run under."""

    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="Confirmation required: this downloads and installs Python packages into the Hermes interpreter.",
        )
    import subprocess

    from ..hermes.locate import has_bridge_dependencies

    install = detect_install(container.settings)
    interpreter = body.interpreter or container.settings.hermes_python or install.selected_python
    if not interpreter:
        raise HTTPException(
            status_code=409,
            detail=(
                "No usable Python interpreter was found. Install Python 3.11–3.14, set CC_HERMES_PYTHON to its path, "
                "then retry."
            ),
        )
    extras = "".join(character for character in body.extras if character.isalnum() or character in ",-")
    target = f"{container.settings.hermes_source}[{extras}]" if extras else str(container.settings.hermes_source)
    commands = [[interpreter, "-m", "pip", "install", "-e", target]]
    if body.include_bridge and not has_bridge_dependencies(interpreter)[0]:
        commands.append([interpreter, "-m", "pip", "install", "aiohttp"])
    container.audit(
        "runtime_bootstrap",
        actor=principal.username,
        detail=" && ".join(" ".join(command) for command in commands),
        level="warning",
    )
    container.logs.system(f"Installing Hermes dependencies with {interpreter} (this can take minutes).", level="WARNING")
    outputs: list[dict] = []
    ok = True
    for command in commands:
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                cwd=str(container.settings.hermes_source),
                capture_output=True,
                text=True,
                timeout=1800,
            )
        except subprocess.TimeoutExpired:
            outputs.append({"command": " ".join(command), "exit_code": None, "output": "timed out after 30 minutes"})
            ok = False
            break
        redactor = get_redactor()
        outputs.append(
            {
                "command": " ".join(command),
                "exit_code": completed.returncode,
                "output": redactor.redact((completed.stdout or "") + (completed.stderr or ""))[-6000:],
            }
        )
        if completed.returncode != 0:
            ok = False
            break
    container.logs.log("INFO" if ok else "ERROR", "SYSTEM", f"Dependency install {'succeeded' if ok else 'failed'}.")
    still_missing = has_bridge_dependencies(interpreter)[1] if not ok else ""
    return {
        "ok": ok,
        "commands": outputs,
        "install_after": detect_install(container.settings).to_dict(),
        "bridge_ready": has_bridge_dependencies(interpreter)[0],
        "next": (
            "Press Start to run the runtime."
            if ok
            else "Fix the error above (often a missing build tool or no network access) and retry." + (f" {still_missing}" if still_missing else "")
        ),
    }


@router.get("/events")
async def runtime_events(
    principal: Annotated[Principal, Depends(require_admin)],
    container: ContainerDep,
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    rows = container.db.query(
        "SELECT ts, event, detail, state, pid FROM runtime_events ORDER BY id DESC LIMIT ?", (limit,)
    )
    return {"events": rows}
