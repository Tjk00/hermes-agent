"""Logging: one place where events are categorised, redacted and stored.

Two sinks are written for everything the Control Center produces:

* ``log_events`` in the database — searchable, filterable, exportable, cleared
  from the UI;
* ``$CC_HOME/logs/control-center.log`` with mode 0600 — survives a lost UI and is
  what you would attach to a bug report.

A third, in-memory ring buffer backs the live tail in the UI without hammering
the database. Every message passes through the redactor first, so a key pasted
into a chat prompt or an environment editor cannot end up in a log file.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any

from .db import Database, utcnow
from .security import get_redactor

CATEGORIES = (
    "INFO",
    "WARNING",
    "ERROR",
    "MODEL",
    "PROVIDER",
    "SECURITY",
    "SCHEDULER",
    "SYSTEM",
)

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class LogBus:
    def __init__(self, db: Database, log_file: Path, *, retention_days: int = 30, ring_size: int = 500) -> None:
        self.db = db
        self.log_file = log_file
        self.retention_days = retention_days
        self._ring: deque[dict[str, Any]] = deque(maxlen=ring_size)
        self._lock = threading.Lock()
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_file()
        self._install_root_handler()

    # ------------------------------------------------------------------ setup
    def _ensure_file(self) -> None:
        if not self.log_file.exists():
            self.log_file.touch()
        try:
            self.log_file.chmod(0o600)
        except OSError:  # pragma: no cover
            pass

    def _install_root_handler(self) -> None:
        handler = _ForwardingHandler(self)
        handler.setLevel(logging.INFO)
        root = logging.getLogger()
        if not any(isinstance(existing, _ForwardingHandler) for existing in root.handlers):
            root.addHandler(handler)
        if not any(isinstance(existing, logging.StreamHandler) for existing in root.handlers):
            stream = logging.StreamHandler(sys.stderr)
            stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            root.addHandler(stream)

    # ----------------------------------------------------------------- writing
    def log(self, level: str, category: str, message: str, meta: dict | None = None) -> dict:
        level = (level or "INFO").upper()
        category = (category or "INFO").upper()
        redactor = get_redactor()
        safe_message = redactor.redact(message)
        safe_meta = {key: redactor.redact(value) for key, value in (meta or {}).items()} if meta else None
        entry = {
            "ts": utcnow(),
            "level": level,
            "category": category if category in CATEGORIES else "INFO",
            "message": safe_message,
            "meta": json.dumps(safe_meta) if safe_meta else None,
        }
        with self._lock:
            try:
                self.db.insert(
                    "INSERT INTO log_events (ts, level, category, message, meta) VALUES (?, ?, ?, ?, ?)",
                    (entry["ts"], entry["level"], entry["category"], entry["message"], entry["meta"]),
                )
            except Exception:  # noqa: BLE001 - logging must never break the request
                pass
            self._ring.append(entry)
            try:
                with self.log_file.open("a", encoding="utf-8") as handle:
                    handle.write(f"{entry['ts']} {entry['level']} {entry['category']}: {entry['message']}\n")
            except OSError:  # pragma: no cover
                pass
        return entry

    # convenience wrappers -----------------------------------------------------
    def info(self, message: str, category: str = "INFO", meta: dict | None = None) -> dict:
        return self.log("INFO", category, message, meta)

    def warning(self, message: str, category: str = "WARNING", meta: dict | None = None) -> dict:
        return self.log("WARNING", category, message, meta)

    def error(self, message: str, category: str = "ERROR", meta: dict | None = None) -> dict:
        return self.log("ERROR", category, message, meta)

    def security(self, message: str, level: str = "WARNING", meta: dict | None = None) -> dict:
        return self.log(level, "SECURITY", message, meta)

    def model(self, message: str, meta: dict | None = None) -> dict:
        return self.log("INFO", "MODEL", message, meta)

    def provider(self, message: str, level: str = "INFO", meta: dict | None = None) -> dict:
        return self.log(level, "PROVIDER", message, meta)

    def scheduler(self, message: str, level: str = "INFO", meta: dict | None = None) -> dict:
        return self.log(level, "SCHEDULER", message, meta)

    def system(self, message: str, level: str = "INFO", meta: dict | None = None) -> dict:
        return self.log(level, "SYSTEM", message, meta)

    # ------------------------------------------------------------------ reading
    def tail(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return list(self._ring)[-limit:]

    def prune(self) -> int:
        """Drops entries older than the retention window. Returns rows removed."""

        from datetime import UTC, datetime, timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=max(1, self.retention_days))).replace(microsecond=0)
        cutoff_text = cutoff.isoformat().replace("+00:00", "Z")
        return self.db.execute("DELETE FROM log_events WHERE ts < ?", (cutoff_text,))


class _ForwardingHandler(logging.Handler):
    """Routes stdlib logging records into the LogBus (redacted)."""

    def __init__(self, bus: LogBus) -> None:
        super().__init__()
        self.bus = bus

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            category = "INFO"
            name = (record.name or "").lower()
            if "security" in name or "auth" in name:
                category = "SECURITY"
            elif "provider" in name or "keys" in name:
                category = "PROVIDER"
            elif "model" in name or "chat" in name:
                category = "MODEL"
            elif "schedule" in name or "cron" in name:
                category = "SCHEDULER"
            elif "runtime" in name or "hermes" in name:
                category = "SYSTEM"
            message = record.getMessage()
            if record.exc_info:
                message = f"{message} | {record.exc_info[0].__name__ if record.exc_info[0] else 'error'}"
            self.bus.log(record.levelname, category, message)
        except Exception:  # noqa: BLE001 - never raise from logging
            pass
