"""Can this machine actually run a local model?

Hermes ships a real local-inference manager (catalog, hardware probe, llama.cpp
runtime download). This module reads that truth and turns it into an honest
verdict for the UI:

* ``supported``   — at least one catalog model fits; local chat is realistic.
* ``marginal``    — something fits, but only tiny models/quantisations.
* ``unsupported`` — nothing in the catalog fits; local inference will not work
  here, and the UI must say so instead of pretending.

The verdict never *promises* free inference: a model that "fits" still needs
CPU/GPU time, and a free container that sleeps cannot serve a 24/7 agent.
"""

from __future__ import annotations

import shutil
from typing import Any

import psutil

from .client import HermesAPIError, HermesClient, HermesUnavailable


def host_profile() -> dict:
    """Local resource snapshot (also used by /api/status)."""
    memory = psutil.virtual_memory()
    disk = shutil.disk_usage("/")
    cpu_freq = psutil.cpu_freq()
    return {
        "cpu_count": psutil.cpu_count(logical=True),
        "cpu_physical": psutil.cpu_count(logical=False),
        "cpu_percent": psutil.cpu_percent(interval=None),
        "cpu_freq_mhz": round(cpu_freq.current) if cpu_freq else None,
        "load_avg": list(getattr(psutil, "getloadavg", lambda: (0, 0, 0))()),
        "memory_total_bytes": memory.total,
        "memory_available_bytes": memory.available,
        "memory_used_percent": memory.percent,
        "disk_total_bytes": disk.total,
        "disk_free_bytes": disk.free,
        "disk_used_percent": round((disk.used / disk.total) * 100, 1) if disk.total else None,
    }


def human_bytes(value: int | float | None) -> str:
    if not value:
        return "0 B"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


async def local_capability(client: HermesClient) -> dict:
    """Merge Hermes' own local-model view with the Control Center's host view."""
    result: dict[str, Any] = {
        "host": host_profile(),
        "hermes": {"hardware": None, "status": None, "catalog": [], "error": ""},
        "verdict": {
            "state": "unknown",
            "headline": "",
            "detail": "",
            "fits": [],
            "closest": [],
            "recommendation": "",
        },
    }
    try:
        result["hermes"]["hardware"] = await client.get("/api/local-models/hardware", timeout=30)
    except (HermesAPIError, HermesUnavailable) as exc:
        result["hermes"]["error"] = str(exc)
    try:
        result["hermes"]["status"] = await client.get("/api/local-models/status", timeout=30)
    except (HermesAPIError, HermesUnavailable):
        pass
    try:
        catalog = await client.get("/api/local-models/catalog", timeout=60)
        models = catalog.get("models") if isinstance(catalog, dict) else catalog
        result["hermes"]["catalog"] = models or []
    except (HermesAPIError, HermesUnavailable):
        pass

    host = result["host"]
    hardware = result["hermes"]["hardware"] or {}
    status = result["hermes"]["status"] or {}
    catalog = result["hermes"]["catalog"]

    available = hardware.get("ram_available_bytes") or host["memory_available_bytes"]
    total = hardware.get("ram_total_bytes") or host["memory_total_bytes"]
    vram_usable = hardware.get("vram_usable_bytes") or 0
    gpu = hardware.get("gpu_name")
    uma = bool(hardware.get("uma"))

    fits = [m for m in catalog if m.get("fits")]
    closest = sorted(
        [m for m in catalog if isinstance(m.get("size_bytes"), (int, float))],
        key=lambda m: m.get("size_bytes") or 0,
    )[:3]

    verdict = result["verdict"]
    if not catalog:
        verdict.update(
            state="unknown",
            headline="Local model catalogue unavailable",
            detail=(
                "Hermes could not return its local-model catalogue, so the Control Center cannot "
                "tell whether local inference would fit on this host."
            ),
            recommendation="Start the Hermes runtime, then re-run this check.",
        )
        return result

    if fits:
        best = fits[0]
        verdict.update(
            state="supported",
            headline="Local inference is available on this machine",
            detail=(
                f"{len(fits)} model(s) fit in the memory Hermes detected "
                f"({human_bytes(available)} usable). Smallest fit: {best.get('display_name')} "
                f"({best.get('size_label')})."
            ),
            fits=fits[:6],
            closest=closest,
            recommendation=(
                "Local inference is $0 in API spend but consumes your CPU/GPU. "
                "Download a model from the Local Models page (Hermes manages the llama.cpp runtime)."
            ),
        )
        return result

    # Nothing fits — explain precisely why, with the numbers we actually read.
    if uma and not gpu:
        why = (
            "This host has no discrete GPU; CPU and RAM share one memory pool, so the whole model "
            f"must live in {human_bytes(total)} of system memory."
        )
    elif gpu:
        why = f"Detected GPU: {gpu}, with {human_bytes(vram_usable)} usable VRAM."
    else:
        why = f"No GPU detected; available memory is {human_bytes(available)}."
    smallest = closest[0] if closest else None
    verdict.update(
        state="unsupported",
        headline="This machine cannot run a local Hermes model",
        detail=(
            f"{why} The smallest model in Hermes' catalogue is "
            f"{smallest.get('display_name') if smallest else 'larger than this host'} "
            f"({smallest.get('size_label') if smallest else 'unknown size'}). "
            f"{smallest.get('fit_detail') if smallest else ''}".strip()
        ),
        fits=[],
        closest=closest,
        recommendation=(
            "Free options that do not need local hardware: the Nous free tier (no key, availability "
            "gated) or another provider with a documented free allowance. Paid keys stay optional."
        ),
    )
    if available < 2 * 1024**3:
        verdict["detail"] += (
            " Under ~2 GB of free memory even a 0.5B parameter model will thrash — treat local "
            "inference as unavailable here."
        )
    if status.get("runtime_installed") is False:
        verdict["detail"] += " The managed llama.cpp runtime is not installed on this host."
    return result
