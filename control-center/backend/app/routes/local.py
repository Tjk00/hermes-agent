"""Local model reality check: can this host actually run a model, and at what size?

Nothing here pretends a small box can run a 7B model. The verdict merges Hermes'
own hardware view (``/api/local-models/hardware``) and catalogue
(``/api/local-models/catalog``, where each entry says whether it fits) with our own
host measurements, and explains the arithmetic when it does not fit.
"""

from __future__ import annotations

import os
import platform
import shutil
from typing import Annotated

from fastapi import APIRouter, Depends

from ..container import Container
from ..db import utcnow
from ..deps import ContainerDep, Principal, mutation_principal, require_admin
from ..hermes.client import HermesAPIError, HermesUnavailable

router = APIRouter(prefix="/local", tags=["local-models"])


def _human_bytes(value: int | float | None) -> str:
    if not value:
        return "0 B"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def host_profile() -> dict:
    """Host measurements, with psutil when available and /proc as the fallback."""

    profile: dict = {
        "cpu_count": os.cpu_count() or 1,
        "cpu_physical": None,
        "cpu_percent": None,
        "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
        "memory_total_bytes": None,
        "memory_available_bytes": None,
        "memory_used_percent": None,
        "disk_total_bytes": None,
        "disk_free_bytes": None,
        "disk_used_percent": None,
        "gpu": None,
        "platform": platform.platform(),
        "machine": platform.machine(),
    }
    try:
        usage = shutil.disk_usage("/")
        profile["disk_total_bytes"] = usage.total
        profile["disk_free_bytes"] = usage.free
        profile["disk_used_percent"] = round(usage.used / usage.total * 100, 1) if usage.total else None
    except OSError:
        pass
    try:
        import psutil  # optional

        memory = psutil.virtual_memory()
        profile["memory_total_bytes"] = memory.total
        profile["memory_available_bytes"] = memory.available
        profile["memory_used_percent"] = memory.percent
        profile["cpu_physical"] = psutil.cpu_count(logical=False)
        profile["cpu_percent"] = psutil.cpu_percent(interval=None)
        frequency = psutil.cpu_freq()
        profile["cpu_freq_mhz"] = round(frequency.current) if frequency else None
    except Exception:  # noqa: BLE001 - psutil is optional
        try:
            with open("/proc/meminfo", encoding="ascii", errors="ignore") as handle:
                info = {}
                for line in handle:
                    key, _, value = line.partition(":")
                    info[key.strip()] = int(value.strip().split()[0]) * 1024
            profile["memory_total_bytes"] = info.get("MemTotal")
            profile["memory_available_bytes"] = info.get("MemAvailable")
            total, available = info.get("MemTotal") or 0, info.get("MemAvailable") or 0
            if total:
                profile["memory_used_percent"] = round((total - available) / total * 100, 1)
        except OSError:
            pass
    return profile


def _gpu_hint() -> dict | None:
    """Best-effort GPU detection (nvidia-smi), never a requirement."""

    from shutil import which

    if not which("nvidia-smi"):
        return None
    try:
        import subprocess

        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return None
        name, _, memory = completed.stdout.strip().splitlines()[0].partition(",")
        return {"name": name.strip(), "vram": memory.strip()}
    except (OSError, subprocess.SubprocessError):
        return None


async def capability(container: Container) -> dict:
    host = host_profile()
    host["gpu"] = _gpu_hint()
    result: dict = {
        "generated_at": utcnow(),
        "host": host,
        "hermes": {"hardware": None, "status": None, "catalog": [], "error": ""},
        "verdict": {
            "state": "unknown",
            "headline": "Local capability has not been measured yet",
            "detail": "",
            "fits": [],
            "closest": [],
            "recommendation": "Press Check to measure this host and ask Hermes what it could run here.",
        },
    }
    client = container.supervisor.client()
    for key, path, timeout in (
        ("hardware", "/api/local-models/hardware", 30),
        ("status", "/api/local-models/status", 30),
        ("catalog", "/api/local-models/catalog", 60),
    ):
        try:
            payload = await client.get(path, timeout=timeout)
            if key == "catalog":
                result["hermes"]["catalog"] = payload.get("models") if isinstance(payload, dict) else payload or []
            else:
                result["hermes"][key] = payload
        except (HermesAPIError, HermesUnavailable) as exc:
            if not result["hermes"]["error"]:
                result["hermes"]["error"] = exc.human_detail() if isinstance(exc, HermesAPIError) else str(exc)

    hardware = result["hermes"]["hardware"] or {}
    catalog = result["hermes"]["catalog"] or []
    available = hardware.get("ram_available_bytes") or host["memory_available_bytes"] or 0
    total = hardware.get("ram_total_bytes") or host["memory_total_bytes"] or 0
    vram = hardware.get("vram_usable_bytes") or 0
    gpu = hardware.get("gpu_name") or (host.get("gpu") or {}).get("name")
    uma = bool(hardware.get("uma"))
    verdict = result["verdict"]

    if not catalog:
        verdict.update(
            state="unknown",
            headline="No local model catalogue available",
            detail=(
                "Hermes did not return its local-model catalogue, so this app cannot say whether local inference "
                "would work here. " + (result["hermes"]["error"] or "")
            ).strip(),
            recommendation="Start the runtime, then press Check again.",
        )
        return result

    fits = [model for model in catalog if model.get("fits")]
    closest = sorted(
        [model for model in catalog if isinstance(model.get("size_bytes"), (int, float))],
        key=lambda model: model.get("size_bytes") or 0,
    )[:5]

    if fits:
        best = fits[0]
        verdict.update(
            state="supported",
            headline="Local inference is possible on this host",
            detail=(
                f"{len(fits)} model(s) fit in the memory Hermes detected ({_human_bytes(available)} usable). "
                f"Smallest fit: {best.get('display_name') or best.get('name')} ({best.get('size_label') or _human_bytes(best.get('size_bytes'))})."
            ),
            fits=fits[:8],
            closest=closest,
            recommendation=(
                "A local model costs $0 per request but uses your CPU/GPU and multi-GB of disk. "
                "Expect CPU-only inference to be slow: usable for short answers, painful for long ones."
            ),
        )
        return result

    if uma and not gpu:
        why = (
            "This host has no discrete GPU, so CPU and RAM share one memory pool and the whole model must fit in "
            f"{_human_bytes(total)} of system memory."
        )
    elif gpu:
        why = f"The GPU found ({gpu}) has about {_human_bytes(vram)} of usable VRAM."
    else:
        why = f"No GPU was detected and this host has {_human_bytes(available)} of usable memory."

    smallest = closest[0] if closest else None
    verdict.update(
        state="unsupported",
        headline="Local inference would not fit on this host",
        detail=(
            f"{why} The smallest model Hermes knows about is "
            f"{smallest.get('display_name') if smallest else 'unknown'} "
            f"({_human_bytes((smallest or {}).get('size_bytes'))}), which does not fit."
        ),
        fits=[],
        closest=closest,
        recommendation=(
            "Use a provider free tier (LIMITED FREE TIER) with your own free key, or run the agent on a machine with "
            "more RAM. This app will not pretend a 4 GB CPU-only box can serve a 7B model."
        ),
    )
    return result


@router.get("/capability")
async def get_capability(principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep, check: bool = False) -> dict:
    cached = container.db.get_setting("local_capability_summary", {}) or {}
    if cached and not check:
        return {**cached, "cached": True}
    result = await capability(container)
    result["cached"] = False
    container.db.set_setting(
        "local_capability_summary",
        {
            "generated_at": result["generated_at"],
            "verdict": result["verdict"],
            "host": {
                "memory_total_bytes": result["host"].get("memory_total_bytes"),
                "memory_available_bytes": result["host"].get("memory_available_bytes"),
                "cpu_count": result["host"].get("cpu_count"),
                "disk_free_bytes": result["host"].get("disk_free_bytes"),
                "gpu": result["host"].get("gpu"),
            },
        },
    )
    return result


@router.post("/capability")
async def refresh_capability(principal: Annotated[Principal, Depends(mutation_principal)], container: ContainerDep) -> dict:
    result = await capability(container)
    container.db.set_setting(
        "local_capability_summary",
        {
            "generated_at": result["generated_at"],
            "verdict": result["verdict"],
            "host": {
                "memory_total_bytes": result["host"].get("memory_total_bytes"),
                "memory_available_bytes": result["host"].get("memory_available_bytes"),
                "cpu_count": result["host"].get("cpu_count"),
                "disk_free_bytes": result["host"].get("disk_free_bytes"),
                "gpu": result["host"].get("gpu"),
            },
        },
    )
    container.audit("local_capability_checked", actor=principal.username, detail=result["verdict"]["state"])
    return result
