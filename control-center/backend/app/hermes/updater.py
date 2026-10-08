"""Upstream update strategy.

The brief asks for a documented, low-friction way to keep following upstream
Hermes. The architecture makes that cheap:

    UPSTREAM HERMES  ->  Integration layer  ->  Control Center backend  ->  Web UI

Only the *integration layer* (``app/hermes/*``) knows Hermes specifics, and even
that only through its HTTP API plus the CLI's documented flags. Updating the
checkout therefore needs no changes here; if upstream ever renames a route, the
affected card reports it instead of failing silently.

Nothing in this module edits files inside the Hermes checkout.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .locate import _run  # noqa: PLC2701 - shared tiny helper, intentionally internal


def git_status(source_dir: Path) -> dict:
    if not (source_dir / ".git").exists():
        return {"is_git": False, "reason": "the checkout has no .git directory (a tarball install?)"}
    commit, _ = _run(["git", "-C", str(source_dir), "rev-parse", "--short", "HEAD"])
    branch, _ = _run(["git", "-C", str(source_dir), "rev-parse", "--abbrev-ref", "HEAD"])
    date, _ = _run(["git", "-C", str(source_dir), "log", "-1", "--format=%cs"])
    subject, _ = _run(["git", "-C", str(source_dir), "log", "-1", "--format=%s"])
    dirty_code, dirty, _ = _run(["git", "-C", str(source_dir), "status", "--porcelain"])
    remote, _ = _run(["git", "-C", str(source_dir), "remote", "get-url", "origin"])
    return {
        "is_git": True,
        "commit": commit,
        "branch": branch,
        "commit_date": date,
        "commit_subject": subject,
        "dirty": bool(dirty.strip()) if dirty_code == 0 else None,
        "dirty_files": len([line for line in dirty.splitlines() if line.strip()]) if dirty_code == 0 else None,
        "remote": remote,
    }


def upstream_strategy() -> dict:
    return {
        "order": ["UPSTREAM HERMES", "INTEGRATION LAYER", "BACKEND", "WEB UI"],
        "rules": [
            "Hermes stays an unmodified upstream dependency: no vendoring, no fork-only patches.",
            "The Control Center talks to it over its own documented HTTP API (the same routes its dashboard uses).",
            "Provider/model/cron/memory/session knowledge is normalised inside app/hermes/* only.",
            "Keys live in the Control Center's encrypted vault and are injected into the runtime environment at launch; upstream config is never edited silently.",
            "Updating upstream = git pull in the checkout. No rebuild of the Control Center is required.",
        ],
        "commands": [
            "cd <hermes-checkout> && git fetch --all && git pull --ff-only",
            "Restart the runtime from the Dashboard (or let auto-restart pick it up after a crash)",
            "Diagnostics → Run doctor to let upstream verify its own install",
        ],
        "risks": [
            "A renamed upstream route makes one card degrade; the response includes the missing path and status.",
            "A change in the dashboard session-token handshake would break supervision — the runtime log shows the 401 immediately.",
            "Upstream always runs on its own port; the Control Center never proxies its static UI.",
        ],
    }


def update_status(settings, client) -> dict:
    """Combines what upstream reports with what git says about the checkout."""

    source_dir = Path(settings.hermes_source)
    payload: dict = {
        "checkout": git_status(source_dir),
        "control_center": {"version": settings.version, "update_method": "git pull / redeploy"},
        "strategy": upstream_strategy(),
        "upstream_check": None,
        "upstream_error": "",
    }
    try:
        payload["upstream_check"] = client.request_sync("GET", "/api/hermes/update/check", timeout=30)
    except Exception as exc:  # noqa: BLE001 - optional endpoint across versions
        payload["upstream_error"] = (
            "This Hermes build does not expose an update check over HTTP "
            f"({exc.__class__.__name__}). Update the checkout with git instead."
        )
    return payload


def apply_update(settings, client) -> dict:
    """Runs upstream's own update routine, if it offers one over HTTP.

    Never called automatically: the UI requires an explicit confirmation.
    """

    source_dir = Path(settings.hermes_source)
    before = git_status(source_dir)
    result: dict = {"ok": False, "before": before, "output": "", "method": ""}
    try:
        response = client.request_sync("POST", "/api/hermes/update", json_body={}, timeout=600)
        result.update(ok=True, method="upstream HTTP API", output=_stringify(response))
    except Exception as exc:  # noqa: BLE001
        result["upstream_error"] = f"HTTP update endpoint unavailable: {exc}"
        if (source_dir / ".git").exists():
            code, out, err = _run(["git", "-C", str(source_dir), "pull", "--ff-only"], timeout=300)
            result.update(
                ok=code == 0,
                method="git pull --ff-only",
                output=(out + ("\n" + err if err else ""))[-4000:],
            )
        else:
            result["output"] = (
                "Neither an HTTP update endpoint nor a git checkout is available. "
                "Re-download the upstream release and restart the runtime."
            )
    result["after"] = git_status(source_dir)
    result["next"] = "Restart the runtime to run the new version."
    return result


def _stringify(payload) -> str:
    if isinstance(payload, dict):
        for key in ("output", "log", "stdout", "message", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value[-4000:]
        return str(payload)[:4000]
    return str(payload)[:4000]


def subprocess_available() -> bool:  # pragma: no cover - trivial
    return shutil_which("git") is not None


def shutil_which(name: str) -> str | None:  # pragma: no cover - trivial
    from shutil import which

    return which(name)


__all__ = ["apply_update", "git_status", "update_status", "upstream_strategy", "subprocess_available", "subprocess"]
