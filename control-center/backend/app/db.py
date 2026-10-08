"""Database layer: SQLite by default, PostgreSQL when you point CC_DATABASE_URL at it.

The abstraction is intentionally thin. Statements are written once with ``?``
placeholders and rewritten for the active dialect, so there is no ORM to fight and
no query duplication. Every statement the app runs lives in this module or in a
router's SQL constant, never in a template string built from user input.

Supported URLs
--------------
* ``sqlite:///absolute/path.db``  (or empty → ``$CC_HOME/control-center.db``)
* ``postgresql://user:pass@host/db``  (psycopg 3)
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


def utcnow() -> str:
    """ISO-8601 UTC timestamp with a trailing Z (sorts correctly as text)."""

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class DatabaseUnavailable(RuntimeError):
    pass


def _rewrite_placeholders(sql: str) -> str:
    return sql.replace("?", "%s")


class Database:
    """A tiny synchronous database facade shared by all routes."""

    def __init__(self, url: str, *, home: Path) -> None:
        self.url = url or ""
        self.home = home
        self.dialect = "postgres" if self.url.startswith(("postgres://", "postgresql://")) else "sqlite"
        self._local = threading.local()
        self._lock = threading.Lock()
        self._sqlite_path: Path | None = None
        if self.dialect == "sqlite":
            self._sqlite_path = self._resolve_sqlite_path()
            self._sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialise()

    # ------------------------------------------------------------- connection
    def _resolve_sqlite_path(self) -> Path:
        if self.url in {"", "sqlite", "sqlite://", ":memory:"}:
            return self.home / "control-center.db"
        if self.url.startswith("sqlite:///"):
            return Path(self.url[len("sqlite:///") :])
        if self.url.startswith("sqlite://"):
            return Path(self.url[len("sqlite://") :])
        raise DatabaseUnavailable(f"Unsupported database URL scheme: {self.url.split(':', 1)[0]}")

    @contextmanager
    def connection(self):
        if self.dialect == "sqlite":
            connection = getattr(self._local, "connection", None)
            if connection is None:
                connection = sqlite3.connect(self._sqlite_path, timeout=30, isolation_level=None)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=10000")
                self._local.connection = connection
            yield connection
        else:  # pragma: no cover - exercised only with a live PostgreSQL server
            import psycopg
            from psycopg.rows import dict_row

            with psycopg.connect(self.url, row_factory=dict_row) as connection:
                yield connection

    # --------------------------------------------------------------- querying
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        with self._lock:
            with self.connection() as connection:
                if self.dialect == "sqlite":
                    cursor = connection.execute(sql, tuple(params))
                    return [dict(row) for row in cursor.fetchall()]
                cursor = connection.execute(_rewrite_placeholders(sql), tuple(params))
                rows = cursor.fetchall()
                return [dict(row) for row in rows]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Runs a statement, returns affected row count."""

        with self._lock:
            with self.connection() as connection:
                if self.dialect == "sqlite":
                    cursor = connection.execute(sql, tuple(params))
                    return cursor.rowcount
                cursor = connection.execute(_rewrite_placeholders(sql), tuple(params))
                return cursor.rowcount

    def insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Runs an INSERT and returns the new primary key."""

        with self._lock:
            with self.connection() as connection:
                if self.dialect == "sqlite":
                    cursor = connection.execute(sql, tuple(params))
                    return int(cursor.lastrowid or 0)
                statement = _rewrite_placeholders(sql)
                if "returning" not in statement.lower():
                    statement = statement.rstrip().rstrip(";") + " RETURNING id"
                cursor = connection.execute(statement, tuple(params))
                row = cursor.fetchone()
                if row is None:
                    return 0
                return int(row["id"] if isinstance(row, dict) else row[0])

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        payload = [tuple(row) for row in rows]
        if not payload:
            return
        with self._lock:
            with self.connection() as connection:
                if self.dialect == "sqlite":
                    connection.executemany(sql, payload)
                else:  # pragma: no cover
                    with connection.cursor() as cursor:
                        cursor.executemany(_rewrite_placeholders(sql), payload)

    # ----------------------------------------------------------------- schema
    def initialise(self) -> None:
        for statement in self._ddl():
            self._execute_ddl(statement)
        drift = self.verify_schema()
        if drift:
            stored = self.get_setting("schema_version", 0)
            raise DatabaseUnavailable(
                "The database schema does not match this build of the Control Center.\n"
                f"  stored schema version: {stored} (this build expects {SCHEMA_VERSION})\n"
                "  problems: " + "; ".join(drift[:4]) + "\n"
                "Your data is intact — nothing here deletes it. Fix it with either:\n"
                "  python -m app.cli reset-db --yes     # moves the old file aside and starts clean\n"
                "  or point CC_DATABASE_URL at a fresh SQLite file / Postgres database."
            )
        current = self.get_setting("schema_version", 0)
        if not current:
            self.set_setting("schema_version", SCHEMA_VERSION)

    def verify_schema(self) -> list[str]:
        """Compare the live database against the schema this build needs.

        Returns a list of human-readable problems (empty when everything matches).
        Called on every start: a half-migrated or foreign database should produce a
        clear sentence, not a 500 from deep inside a query.
        """

        problems: list[str] = []
        for table, columns in _expected_columns(self._ddl()).items():
            live = self._column_names(table)
            if not live:
                problems.append(f"missing table {table}")
                continue
            missing = [name for name in columns if name not in live]
            if missing:
                problems.append(f"{table} is missing column(s): {', '.join(sorted(missing))}")
        return problems

    def _column_names(self, table: str) -> list[str]:
        try:
            if self.dialect == "sqlite":
                rows = self.query(f"PRAGMA table_info({table})")
                return [str(row.get("name")) for row in rows]
            rows = self.query(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?", (table,)
            )
            return [str(row.get("column_name") or row.get("COLUMN_NAME")) for row in rows]
        except Exception:  # noqa: BLE001 - a nonexistent table is reported by the caller
            return []

    def _execute_ddl(self, statement: str) -> None:
        with self._lock:
            with self.connection() as connection:
                if self.dialect == "sqlite":
                    connection.execute(statement)
                else:  # pragma: no cover
                    with connection.cursor() as cursor:
                        cursor.execute(statement)

    def _ddl(self) -> list[str]:
        if self.dialect == "sqlite":
            pk = "INTEGER PRIMARY KEY AUTOINCREMENT"
        else:  # pragma: no cover
            pk = "BIGSERIAL PRIMARY KEY"
        return [
            f"""
            CREATE TABLE IF NOT EXISTS users (
              id {pk},
              username TEXT NOT NULL UNIQUE,
              password_hash TEXT NOT NULL,
              role TEXT NOT NULL DEFAULT 'admin',
              created_at TEXT NOT NULL,
              last_login_at TEXT,
              disabled INTEGER NOT NULL DEFAULT 0
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS auth_sessions (
              id {pk},
              session_id TEXT NOT NULL UNIQUE,
              user_id INTEGER NOT NULL,
              token_fingerprint TEXT NOT NULL,
              csrf_fingerprint TEXT NOT NULL,
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              last_seen_at TEXT,
              user_agent TEXT,
              ip TEXT,
              revoked INTEGER NOT NULL DEFAULT 0
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS api_tokens (
              id {pk},
              label TEXT NOT NULL,
              token_fingerprint TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL,
              last_used_at TEXT,
              user_id INTEGER NOT NULL,
              revoked INTEGER NOT NULL DEFAULT 0
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS app_settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS secrets (
              name TEXT PRIMARY KEY,
              ciphertext TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              created_by TEXT,
              last_used_at TEXT
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS audit_events (
              id {pk},
              ts TEXT NOT NULL,
              actor TEXT,
              action TEXT NOT NULL,
              target TEXT,
              detail TEXT,
              ip TEXT,
              level TEXT NOT NULL DEFAULT 'info'
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS log_events (
              id {pk},
              ts TEXT NOT NULL,
              level TEXT NOT NULL,
              category TEXT NOT NULL,
              message TEXT NOT NULL,
              meta TEXT
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS runtime_events (
              id {pk},
              ts TEXT NOT NULL,
              event TEXT NOT NULL,
              detail TEXT,
              state TEXT,
              pid INTEGER
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS chat_threads (
              id {pk},
              thread_id TEXT NOT NULL UNIQUE,
              title TEXT NOT NULL,
              model TEXT,
              provider TEXT,
              hermes_session_id TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              archived INTEGER NOT NULL DEFAULT 0,
              meta TEXT
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS chat_messages (
              id {pk},
              thread_id TEXT NOT NULL,
              role TEXT NOT NULL,
              content TEXT NOT NULL,
              created_at TEXT NOT NULL,
              model TEXT,
              provider TEXT,
              cost_tier TEXT,
              tokens INTEGER,
              meta TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_log_events_ts ON log_events (ts)",
            "CREATE INDEX IF NOT EXISTS idx_log_events_level ON log_events (level)",
            "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events (ts)",
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_thread ON chat_messages (thread_id)",
            "CREATE INDEX IF NOT EXISTS idx_runtime_events_ts ON runtime_events (ts)",
        ]

    # ---------------------------------------------------------------- settings
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM app_settings WHERE key = ?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def set_setting(self, key: str, value: Any) -> None:
        payload = json.dumps(value)
        existing = self.query_one("SELECT key FROM app_settings WHERE key = ?", (key,))
        if existing:
            self.execute("UPDATE app_settings SET value = ?, updated_at = ? WHERE key = ?", (payload, utcnow(), key))
        else:
            self.insert("INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)", (key, payload, utcnow()))

    def all_settings(self) -> dict[str, Any]:
        return {row["key"]: self.get_setting(row["key"]) for row in self.query("SELECT key FROM app_settings")}

    # ------------------------------------------------------------- diagnostics
    def health(self) -> dict[str, Any]:
        import time

        started = time.time()
        try:
            self.query("SELECT 1 AS ok")
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "dialect": self.dialect, "error": str(exc)}
        return {
            "available": True,
            "dialect": self.dialect,
            "location": str(self._sqlite_path) if self.dialect == "sqlite" else _mask_dsn(self.url),
            "latency_ms": round((time.time() - started) * 1000, 2),
            "schema_version": self.get_setting("schema_version", SCHEMA_VERSION),
        }

    def size_bytes(self) -> int:
        if self.dialect != "sqlite" or self._sqlite_path is None:
            return 0
        total = 0
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self._sqlite_path) + suffix)
            if candidate.exists():
                total += candidate.stat().st_size
        return total

    def vacuum(self) -> None:
        if self.dialect == "sqlite":
            self.execute("VACUUM")


def _expected_columns(statements: list[str]) -> dict[str, list[str]]:
    """Which columns each CREATE TABLE statement declares.

    Parsed rather than duplicated by hand, so the check can never drift from the
    schema it is checking.
    """

    import re

    expected: dict[str, list[str]] = {}
    pattern = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\n\s*\)\s*$", re.S | re.I)
    reserved = {"PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT"}
    for statement in statements:
        match = pattern.search(statement)
        if not match:
            continue
        table, body = match.group(1), match.group(2)
        columns: list[str] = []
        for raw_line in body.splitlines():
            line = raw_line.strip().rstrip(",")
            if not line:
                continue
            first = line.split()[0].strip('"`')
            if first.upper() in reserved:
                continue
            columns.append(first)
        expected[table] = columns
    return expected


def _mask_dsn(dsn: str) -> str:
    return re.sub(r"://([^:]+):[^@]+@", r"://\1:[redacted]@", dsn)


def sqlite_backup(db: Database, destination: Path) -> Path:
    """Consistent snapshot of a SQLite database (or a logical dump for Postgres)."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    if db.dialect == "sqlite" and db._sqlite_path is not None:  # noqa: SLF001 - internal by design
        source = sqlite3.connect(db._sqlite_path)
        try:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
    else:  # pragma: no cover - Postgres path
        tables = [
            "users",
            "auth_sessions",
            "api_tokens",
            "app_settings",
            "secrets",
            "audit_events",
            "log_events",
            "runtime_events",
            "chat_threads",
            "chat_messages",
        ]
        payload = {table: db.query(f"SELECT * FROM {table}") for table in tables}
        destination.write_text(json.dumps({"generated_at": utcnow(), "tables": payload}, indent=2), encoding="utf-8")
    try:
        os.chmod(destination, 0o600)
    except OSError:  # pragma: no cover
        pass
    return destination
