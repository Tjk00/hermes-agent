"""Deployment compatibility: can this host, or that free tier, run the agent?"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import sys
from typing import Annotated

from fastapi import APIRouter, Depends

from ..container import Container
from ..db import utcnow
from ..deps import ContainerDep, Principal, require_admin
from ..hermes.deploy import deployment_matrix, free_hosting_mode  # noqa: E402 - plain module import
from ..hermes.locate import detect_install, DEFAULT_PYTHON, MAX_PYTHON, MIN_PYTHON
from ..hermes.supervisor import STATE_ATTACHED, STATE_EXTERNAL, STATE_RUNNING

router = APIRouter(prefix="/deploy", tags=["deploy"])


def _host_profile() -> dict:
    profile = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "cpu_count": os.cpu_count() or 1,
        "memory_total_bytes": None,
        "memory_available_bytes": None,
        "disk_free_bytes": None,
        "disk_total_bytes": None,
        "is_arm": platform.machine().lower() in {"aarch64", "arm64", "armv7l"},
        "in_container": _in_container(),
    }
    try:
        usage = shutil.disk_usage("/")
        profile["disk_total_bytes"] = usage.total
        profile["disk_free_bytes"] = usage.free
    except OSError:
        pass
    try:
        import psutil  # optional

        memory = psutil.virtual_memory()
        profile["memory_total_bytes"] = memory.total
        profile["memory_available_bytes"] = memory.available
    except Exception:  # noqa: BLE001
        try:
            with open("/proc/meminfo", encoding="ascii", errors="ignore") as handle:
                for line in handle:
                    if line.startswith("MemTotal:"):
                        profile["memory_total_bytes"] = int(line.split()[1]) * 1024
                    elif line.startswith("MemAvailable:"):
                        profile["memory_available_bytes"] = int(line.split()[1]) * 1024
        except OSError:
            pass
    return profile


def _in_container() -> bool:
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    try:
        with open("/proc/1/cgroup", encoding="utf-8", errors="ignore") as handle:
            return any(marker in handle.read() for marker in ("docker", "kubepods", "containerd", "lxc"))
    except OSError:
        return False


def _port_free(host: str, port: int) -> bool:
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.5)
        return sock.connect_ex((probe_host, port)) != 0


def _verdict(memory_ok: bool, disk_ok: bool, source_ok: bool, python_ok: bool) -> dict:
    if memory_ok and disk_ok and source_ok and python_ok:
        return {
            "state": "ready",
            "headline": "This host can run the Control Center and the Hermes runtime",
            "detail": "Start the runtime on the Dashboard. Schedules run while this process stays up.",
        }
    problems = []
    if not source_ok:
        problems.append("the Hermes checkout is missing")
    if not python_ok:
        problems.append(f"no Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}–{MAX_PYTHON[0]}.{MAX_PYTHON[1] - 1} with Hermes' dependencies")
    if not memory_ok:
        problems.append("less than 1 GB of RAM")
    if not disk_ok:
        problems.append("less than 2 GB of free disk")
    return {
        "state": "blocked",
        "headline": "Not ready yet",
        "detail": "Missing requirements: " + ", ".join(problems) + ".",
    }


@router.get("/platforms")
async def platforms(principal: Annotated[Principal, Depends(require_admin)]) -> dict:
    return deployment_matrix()


@router.get("/free-hosting")
async def free_hosting(principal: Annotated[Principal, Depends(require_admin)]) -> dict:
    return free_hosting_mode()


@router.get("/preflight")
async def preflight(principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep) -> dict:
    """Measured, not guessed: what this specific host can do."""

    host = _host_profile()
    install = detect_install(container.settings)
    memory_ok = (host["memory_total_bytes"] or 0) >= 1024**3
    disk_ok = (host["disk_free_bytes"] or 0) >= 2 * 1024**3
    python_ok = bool(install.selected_python) and any(
        candidate.path == install.selected_python and candidate.supported for candidate in install.python_candidates
    )
    deps_ok = any(
        candidate.path == install.selected_python and candidate.has_hermes_deps for candidate in install.python_candidates
    )
    port_free = _port_free(container.settings.runtime_host, container.settings.runtime_port)
    runtime = container.supervisor.status(probe=False)

    checks = [
        {
            "id": "ram",
            "label": "RAM for the agent",
            "ok": memory_ok,
            "detail": f"{(host['memory_total_bytes'] or 0) / 1024**3:.2f} GB total, {(host['memory_available_bytes'] or 0) / 1024**3:.2f} GB available",
            "hint": "1 GB is the floor for the agent with a hosted model; local models need far more.",
        },
        {
            "id": "disk",
            "label": "Disk space",
            "ok": disk_ok,
            "detail": f"{(host['disk_free_bytes'] or 0) / 1024**3:.1f} GB free",
            "hint": "Hermes stores sessions, logs and (optionally) multi-GB models.",
        },
        {
            "id": "hermes_source",
            "label": "Hermes checkout present",
            "ok": install.is_hermes_source,
            "detail": install.source_dir,
            "hint": "git clone https://github.com/NousResearch/hermes-agent and set CC_HERMES_SOURCE.",
        },
        {
            "id": "python",
            "label": "Supported Python interpreter",
            "ok": python_ok,
            "detail": f"{install.selected_python or 'none'} {install.selected_python_version}".strip(),
            "hint": f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}–{MAX_PYTHON[0]}.{MAX_PYTHON[1] - 1} (upstream develops against {DEFAULT_PYTHON[0]}.{DEFAULT_PYTHON[1]}).",
        },
        {
            "id": "dependencies",
            "label": "Hermes dependencies installed",
            "ok": deps_ok,
            "detail": "importable" if deps_ok else "missing (Diagnostics can install them)",
            "hint": "Diagnostics → Runtime dependencies runs the pip install for you.",
        },
        {
            "id": "runtime_port",
            "label": f"Runtime port {container.settings.runtime_port}",
            "ok": port_free or runtime.get("state") in {STATE_RUNNING, STATE_ATTACHED, STATE_EXTERNAL},
            "detail": "free" if port_free else "in use",
            "hint": "Change CC_HERMES_PORT or stop the other process — unless the Control Center is already attached to it.",
        },
        {
            "id": "persistence",
            "label": "Persistent storage for CC_HOME",
            "ok": not _looks_ephemeral(str(container.settings.home)),
            "detail": str(container.settings.home),
            "hint": "If this path is on an ephemeral container filesystem, sessions and encrypted keys vanish on redeploy.",
        },
    ]

    verdict = _verdict(memory_ok, disk_ok, install.is_hermes_source, python_ok and deps_ok)
    return {
        "generated_at": utcnow(),
        "host": host,
        "checks": checks,
        "verdict": verdict,
        "runtime": {"state": runtime.get("state"), "attached": runtime.get("attached", False)},
        "notes": [
            "A host that sleeps cannot run schedules: cron jobs execute inside the runtime process.",
            "Free container tiers are usually 512 MB — enough for this UI, tight for the agent.",
            "Local model inference needs multi-GB RAM/VRAM; the Models page measures what actually fits.",
        ],
    }


def _looks_ephemeral(path: str) -> bool:
    markers = ("/tmp/", "/var/tmp/", "/run/", "/mnt/ephemeral")
    return any(path.startswith(marker) for marker in markers)
