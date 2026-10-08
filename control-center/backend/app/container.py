"""Composition root: one container holding every long-lived object.

Routes depend on ``container`` (a FastAPI dependency) rather than importing
singletons, which keeps the app testable and makes the startup order explicit:

    settings → database → log bus → secret vault → provider registry →
    runtime supervisor (last: it can start a process)
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .config import Settings, get_settings
from .db import Database, utcnow
from .errors import HumanError, database_failure
from .hermes import providers as provider_service
from .hermes.gateway import GatewaySupervisor
from .hermes.supervisor import RuntimeSupervisor
from .logbus import LogBus
from .security import SecretBox, get_redactor


#: Settings an operator may change from the UI, with their labels and defaults.
#: Anything not in here is read-only by design (ports, paths, the database URL),
#: because changing it from a phone would silently break the running app.
EDITABLE_SETTINGS: dict[str, tuple[str, Any, str]] = {
    "free_mode": ("FREE MODE (refuse paid providers)", True, "bool"),
    "allow_paid_fallback": ("Allow a paid model as the last fallback", False, "bool"),
    "nous_free_tier_enabled": ("Use Nous' free guest tier when it is offered", False, "bool"),
    "sync_keys_to_hermes_env": ("Mirror stored keys into $HERMES_HOME/.env", False, "bool"),
    "runtime_autostart": ("Start Hermes when the Control Center starts", True, "bool"),
    "runtime_auto_restart": ("Restart Hermes if it crashes", True, "bool"),
    "log_retention_days": ("Log retention (days)", 30, "int"),
    "theme": ("Theme", "dark", "str"),
    "setup_completed": ("First-run setup finished", False, "bool"),
    "setup_steps": ("Setup progress", {}, "json"),
}


class Container:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.settings.ensure_dirs()
        self._lock = threading.Lock()
        try:
            self.db = Database(self.settings.database_url, home=self.settings.home)
        except Exception as exc:  # noqa: BLE001 - surfaced as a human error by the UI
            raise database_failure(str(exc)) from exc
        self.logs = LogBus(
            self.db,
            self.settings.logs_dir / "control-center.log",
            retention_days=self.settings.log_retention_days,
        )
        self.secret_box = SecretBox(self.settings.master_key_path)
        self.secrets = provider_service.SecretVault(self)
        self.supervisor = RuntimeSupervisor(
            self.settings,
            secrets_provider=self.secrets.runtime_env,
            on_event=self._record_runtime_event,
            extra_env=self._extra_runtime_env,
        )
        # The gateway hosts the agent's chat surfaces. Upstream would hand this to
        # systemd/launchd; on a plain host or a container we run it ourselves.
        self.gateway = GatewaySupervisor(
            self.settings,
            interpreter=self.settings.hermes_python,
            secrets_provider=self.secrets.runtime_env,
            on_event=self._record_gateway_event,
        )
        self._initialised = False

    # ------------------------------------------------------------- lifecycle
    def initialise(self) -> None:
        with self._lock:
            if self._initialised:
                return
            self._initialised = True
        self._apply_defaults()
        if not self.secret_box.available:
            self.logs.warning(
                "Encrypted key storage is unavailable: the 'cryptography' package is not installed in the "
                "Control Center's interpreter. Storing provider keys stays disabled until it is.",
                "SECURITY",
            )
        self.logs.system(f"Control Center {self.settings.version} started (home {self.settings.home}).")
        with_range = self.settings.log_retention_days
        removed = self.logs.prune()
        if removed:
            self.logs.system(f"Pruned {removed} log entries older than {with_range} days.")

    def _apply_defaults(self) -> None:
        defaults = {
            "free_mode": self.settings.free_mode,
            "lock_paid_providers": self.settings.lock_paid_providers,
            "log_retention_days": self.settings.log_retention_days,
            "runtime_autostart": self.settings.runtime_autostart,
            "allow_paid_fallback": False,
            "paid_unlock_ack": "",
            "setup_completed": False,
            "setup_steps": {},
            "chat_bridge_enabled": False,
            "sync_keys_to_hermes_env": False,
            "theme": "dark",
            "hermes_env": {},
        }
        for key, value in defaults.items():
            if self.db.get_setting(key, None) is None:
                self.db.set_setting(key, value)

    def autostart_runtime(self) -> None:
        if not self.settings.runtime_autostart or self.settings.runtime_mode == "disabled":
            return
        try:
            self.supervisor.start(wait=True)
            status = self.supervisor.status(probe=True)
            if status.get("attached"):
                self.logs.system("Attached to an existing Hermes backend instead of starting a second one.")
            elif status.get("healthy"):
                self.logs.system(f"Hermes runtime ready (pid {status.get('pid')}, port {status.get('port')}).")
        except Exception as exc:  # noqa: BLE001 - never block startup on the agent
            self.logs.error(
                "The Hermes runtime did not start automatically. Open the Dashboard to see why.",
                "SYSTEM",
                {"reason": str(exc)[:500]},
            )

    def shutdown(self) -> None:
        for component in (getattr(self, "gateway", None), self.supervisor):
            try:
                if component is not None:
                    component.shutdown()
            except Exception:  # noqa: BLE001 - shutdown must never raise
                pass

    # ---------------------------------------------------------------- helpers
    @property
    def free_mode(self) -> bool:
        return bool(self.db.get_setting("free_mode", True))

    @property
    def paid_unlocked(self) -> bool:
        """Paid providers are usable only after an explicit, recorded unlock."""

        return bool(self.db.get_setting("allow_paid_fallback", False)) and bool(
            self.db.get_setting("paid_unlock_ack", "")
        )

    def audit(
        self,
        action: str,
        *,
        actor: str = "system",
        target: str = "",
        detail: str = "",
        ip: str = "",
        level: str = "info",
    ) -> None:
        try:
            self.db.insert(
                "INSERT INTO audit_events (ts, actor, action, target, detail, ip, level) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (utcnow(), actor, action, target, detail[:1000], ip, level),
            )
        except Exception as exc:  # noqa: BLE001
            self.logs.warning(f"Audit entry could not be written: {exc}", "SYSTEM")

    def log(self, level: str, category: str, message: str, meta: dict | None = None) -> dict:
        return self.logs.log(level, category, message, meta)

    def guard_paid(
        self,
        *,
        provider: str,
        model: str = "",
        action: str = "select a paid model",
        actor: str = "api",
    ) -> None:
        """Refuse paid usage unless the operator unlocked it — loudly, once."""

        from .hermes.provider_catalog import classify_provider

        classification = classify_provider(provider)
        if classification["billing"] != "paid" or self.paid_unlocked:
            return
        self.audit("paid_blocked", actor=actor, target=f"{provider}/{model}", detail=action, level="warning")
        self.logs.warning(
            f"FREE MODE blocked an attempt to {action} via {provider}.",
            "PROVIDER",
            {"provider": provider, "model": model},
        )
        raise HumanError(
            {
                "error": "paid_blocked",
                "title": "FREE MODE blocked a paid provider",
                "message": (
                    f"{classification['label']} bills per request and paid usage is locked. "
                    "Nothing was sent and nothing was charged."
                ),
                "hint": (
                    "Use a free route (local model or a provider free tier), or unlock paid usage in "
                    "Settings → Paid providers, which requires typing a confirmation phrase."
                ),
                "actions": [{"label": "Open settings", "kind": "link", "url": "#settings"}],
                "retryable": False,
            },
            status=402,
        )

    def runtime_env_extra(self) -> dict[str, str]:
        return self._extra_runtime_env()

    # ------------------------------------------------------------- internals
    def _extra_runtime_env(self) -> dict[str, str]:
        """Small, non-secret settings handed to the runtime process."""

        env: dict[str, str] = {}
        if self.db.get_setting("nous_free_tier_enabled", False):
            env["HERMES_GUEST_ONBOARDING"] = "1"
        if self.db.get_setting("log_level", "") == "DEBUG":
            env["HERMES_LOG_LEVEL"] = "DEBUG"
        return env

    def _record_gateway_event(self, event: str, detail: str = "", **kwargs: Any) -> None:
        try:
            self.db.insert(
                "INSERT INTO runtime_events (ts, event, detail, state, pid) VALUES (?, ?, ?, ?, ?)",
                (utcnow(), f"gateway:{event}", detail[:1000], kwargs.get("state"), kwargs.get("pid")),
            )
        except Exception as exc:  # noqa: BLE001
            self.logs.warning(f"Gateway event could not be recorded: {exc}", "SYSTEM")
        level = "ERROR" if event in {"gateway_crashed", "gateway_start_failed"} else "INFO"
        self.logs.log(level, "SYSTEM", f"{event}: {detail}" if detail else event)

    def _record_runtime_event(self, event: str, detail: str = "", **kwargs: Any) -> None:
        try:
            self.db.insert(
                "INSERT INTO runtime_events (ts, event, detail, state, pid) VALUES (?, ?, ?, ?, ?)",
                (utcnow(), event, detail[:1000], kwargs.get("state"), kwargs.get("pid")),
            )
        except Exception as exc:  # noqa: BLE001
            self.logs.warning(f"Runtime event could not be recorded: {exc}", "SYSTEM")
        level = "ERROR" if event in {"crashed", "start_failed"} else "INFO"
        self.logs.log(level, "SYSTEM", f"runtime:{event} — {detail}" if detail else f"runtime:{event}")


_container: Container | None = None


def build_container(settings: Settings | None = None) -> Container:
    """Create (once) and return the process-wide container.

    Only the application lifespan and tests call this. It is deliberately *not* the
    FastAPI dependency: a dependency callable may not take a ``Settings`` argument,
    because FastAPI would treat that dataclass as part of the request body — which
    is exactly what once turned every POST into "field 'settings' is required".
    """

    global _container
    if _container is None:
        _container = Container(settings)
    return _container


def get_container() -> Container:
    """FastAPI dependency: the container built by the lifespan."""

    return build_container()


def reset_container_for_tests() -> None:
    global _container
    _container = None


def backup_path(container: Container, prefix: str = "control-center") -> Path:
    stamp = utcnow().replace(":", "").replace("-", "")
    return container.settings.backups_dir / f"{prefix}-{stamp}.zip"


def register_redaction(container: Container) -> None:
    """Teach the redactor every secret currently in the vault."""

    redactor = get_redactor()
    for row in container.secrets.list_rows():
        with_value = container.secrets.reveal_safe(row["name"])
        if with_value:
            redactor.register(with_value)


# --------------------------------------------------------------------- backup
def _export_configuration(self: Container, *, include_secrets: bool = False) -> dict[str, Any]:
    """A portable snapshot of everything an operator configured.

    Secrets are excluded by default and, even when asked for, the vault export
    contains ciphertext that can only be read with the *same* master key — a backup
    is therefore safe to keep next to the app, but it is not a portable key store.
    """

    settings_rows = {
        key: self.db.get_setting(key)
        for key in EDITABLE_SETTINGS
    }
    threads = self.db.query("SELECT thread_id, title, model, provider, created_at, updated_at, archived FROM chat_threads")
    messages = self.db.query("SELECT thread_id, role, content, created_at, model, provider, cost_tier FROM chat_messages")
    payload: dict[str, Any] = {
        "kind": "control-center-backup",
        "version": 1,
        "generated_at": utcnow(),
        "app_version": self.settings.version,
        "settings": settings_rows,
        "chat": {"threads": threads, "messages": messages},
        "secrets": [],
        "runtime_mode": self.settings.runtime_mode,
        "free_mode": self.free_mode,
        "notes": [
            "This backup never contains readable API keys.",
            "Schedules, memories and skills live in Hermes and are covered by Hermes' own backup (Diagnostics → Backup).",
        ],
    }
    if include_secrets:
        payload["secrets"] = [
            {"name": row["name"], "ciphertext": row["ciphertext"], "updated_at": row["updated_at"]}
            for row in self.db.query("SELECT name, ciphertext, updated_at FROM secrets")
        ]
    return payload


def _import_configuration(self: Container, payload: dict[str, Any], *, actor: str = "system") -> dict[str, Any]:
    restored: dict[str, Any] = {"settings": 0, "threads": 0, "messages": 0, "skipped": []}
    settings_values = payload.get("settings") or {}
    for key, value in settings_values.items():
        if key in EDITABLE_SETTINGS:
            self.db.set_setting(key, value)
            restored["settings"] += 1
    chat = payload.get("chat") or {}
    for thread in chat.get("threads") or []:
        if not thread.get("thread_id"):
            continue
        existing = self.db.query_one("SELECT thread_id FROM chat_threads WHERE thread_id = ?", (thread["thread_id"],))
        if existing:
            restored["skipped"].append(thread["thread_id"])
            continue
        self.db.insert(
            "INSERT INTO chat_threads (thread_id, title, model, provider, hermes_session_id, created_at, updated_at, archived, meta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                thread["thread_id"],
                thread.get("title") or "Restored conversation",
                thread.get("model"),
                thread.get("provider"),
                None,
                thread.get("created_at") or utcnow(),
                thread.get("updated_at") or utcnow(),
                1 if thread.get("archived") else 0,
                "{}",
            ),
        )
        restored["threads"] += 1
    for message in chat.get("messages") or []:
        if not message.get("thread_id"):
            continue
        self.db.insert(
            "INSERT INTO chat_messages (thread_id, role, content, created_at, model, provider, cost_tier, tokens, meta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message["thread_id"],
                message.get("role") or "user",
                message.get("content") or "",
                message.get("created_at") or utcnow(),
                message.get("model"),
                message.get("provider"),
                message.get("cost_tier"),
                None,
                "{}",
            ),
        )
        restored["messages"] += 1
    restored["note"] = "API keys were not restored (a backup never carries them); re-enter them under Providers."
    self.audit("config_restored", actor=actor, detail=str({key: value for key, value in restored.items() if key != "skipped"}))
    return restored


Container.export_configuration = _export_configuration  # type: ignore[attr-defined]
Container.import_configuration = _import_configuration  # type: ignore[attr-defined]
