"""Runtime configuration for the Control Center.

Everything is environment driven (12-factor) so the same image runs on a laptop,
a Raspberry Pi, or a free-tier container. Nothing here is required: the defaults
produce a working, $0, single-host deployment.

Secrets never live in this file. The only generated secret is the Control Center's
own master key, which is created on first run inside ``CC_HOME`` with mode 0600.
"""

from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path

TRUE_VALUES = {"1", "true", "yes", "on", "y", "t"}
FALSE_VALUES = {"0", "false", "no", "off", "n", "f"}


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None else value.strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "").lower()
    if raw in TRUE_VALUES:
        return True
    if raw in FALSE_VALUES:
        return False
    return default


def _env_int(name: str, default: int) -> int:
    raw = _env(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_list(name: str) -> tuple[str, ...]:
    raw = _env(name, "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _expand(value: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(value))).resolve()


def _default_hermes_home() -> Path:
    configured = _env("CC_HERMES_HOME") or _env("HERMES_HOME")
    if configured:
        return _expand(configured)
    return _expand("~/.hermes")


def _default_source_dir() -> Path:
    """Where the upstream Hermes checkout lives.

    Search order, most explicit first:
      1. ``CC_HERMES_SOURCE``
      2. ``$HERMES_SOURCE_DIR`` (upstream convention)
      3. the repository this Control Center ships inside (``../..``)
      4. common clone locations
    """

    explicit = _env("CC_HERMES_SOURCE") or _env("HERMES_SOURCE_DIR")
    if explicit:
        return _expand(explicit)
    here = Path(__file__).resolve()
    # app/config.py -> backend/app -> backend -> control-center -> repo root
    repo_root = here.parents[3] if len(here.parents) > 3 else here.parent
    candidates = [
        repo_root,
        _expand("~/hermes-agent"),
        _expand("~/src/hermes-agent"),
        _expand("~/.hermes/hermes-agent"),
    ]
    for candidate in candidates:
        if (candidate / "hermes_cli" / "main.py").is_file():
            return candidate
    return repo_root


def _detect_python_candidates() -> list[str]:
    """Interpreters worth trying for the Hermes runtime, in priority order."""

    candidates: list[str] = []
    explicit = _env("CC_HERMES_PYTHON")
    if explicit:
        candidates.append(explicit)

    home = _default_hermes_home()
    for relative in (
        ".venv/bin/python",
        "venv/bin/python",
        "env/bin/python",
        "hermes-agent/.venv/bin/python",
        "hermes-venv/bin/python",
    ):
        candidate = home / relative
        if candidate.is_file():
            candidates.append(str(candidate))

    # ``setup-hermes.sh`` and hand-made installs both tend to leave a venv in the
    # home directory; scan one level deep for anything that looks like one.
    for pattern in ("hermes*/bin/python", ".hermes*/bin/python", "venv*/bin/python"):
        try:
            candidates.extend(str(candidate) for candidate in sorted(Path.home().glob(pattern)))
        except OSError:  # pragma: no cover
            pass

    source = _default_source_dir()
    for relative in (".venv/bin/python", "venv/bin/python"):
        candidate = source / relative
        if candidate.is_file():
            candidates.append(str(candidate))

    # A pip-installed console script tells us exactly which interpreter owns it.
    script = _which("hermes")
    if script:
        try:
            first_line = Path(script).read_text(encoding="utf-8", errors="ignore").splitlines()[0]
            if first_line.startswith("#!"):
                candidates.append(first_line[2:].strip())
        except OSError:
            pass

    candidates.extend([sys.executable, "/usr/bin/python3"])
    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen and Path(candidate).exists():
            seen.add(candidate)
            unique.append(candidate)
    return unique


def _which(name: str) -> str:
    from shutil import which

    return which(name) or ""


@dataclass(frozen=True)
class Settings:
    """Immutable configuration snapshot taken at process start."""

    # ---------------------------------------------------------------- hosting
    host: str = field(default_factory=lambda: _env("CC_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("CC_PORT", 8080))
    root_path: str = field(default_factory=lambda: _env("CC_ROOT_PATH", ""))
    dev_mode: bool = field(default_factory=lambda: _env_bool("CC_DEV_MODE", False))
    timezone: str = field(default_factory=lambda: _env("CC_TIMEZONE", "UTC"))

    # ------------------------------------------------------------ state (ours)
    home: Path = field(default_factory=lambda: _expand(_env("CC_HOME", "~/.hermes/control-center")))
    database_url: str = field(default_factory=lambda: _env("CC_DATABASE_URL", ""))
    static_dir: Path = field(default_factory=lambda: _expand(_env("CC_STATIC_DIR", "")) if _env("CC_STATIC_DIR") else Path())
    icon_url: str = field(default_factory=lambda: _env("CC_ICON_URL", ""))

    # ------------------------------------------------------------ the agent
    hermes_home: Path = field(default_factory=_default_hermes_home)
    hermes_source: Path = field(default_factory=_default_source_dir)
    hermes_python: str = field(default_factory=lambda: _env("CC_HERMES_PYTHON", ""))
    hermes_python_candidates: list[str] = field(default_factory=_detect_python_candidates)

    runtime_mode: str = field(default_factory=lambda: _env("CC_RUNTIME_MODE", "dashboard").lower())
    runtime_host: str = field(default_factory=lambda: _env("CC_HERMES_HOST", "127.0.0.1"))
    runtime_port: int = field(default_factory=lambda: _env_int("CC_HERMES_PORT", 9119))
    runtime_autostart: bool = field(default_factory=lambda: _env_bool("CC_RUNTIME_AUTOSTART", True))
    adopt_existing: bool = field(default_factory=lambda: _env_bool("CC_ADOPT_EXISTING", True))
    runtime_isolated: bool = field(default_factory=lambda: _env_bool("CC_HERMES_ISOLATED", True))
    runtime_auto_restart: bool = field(default_factory=lambda: _env_bool("CC_RUNTIME_AUTO_RESTART", True))
    runtime_start_timeout: float = field(default_factory=lambda: float(_env_int("CC_RUNTIME_START_TIMEOUT", 90)))
    runtime_web_dist: Path = field(default_factory=lambda: _expand(_env("CC_RUNTIME_WEB_DIST", "")) if _env("CC_RUNTIME_WEB_DIST") else Path())
    external_url: str = field(default_factory=lambda: _env("CC_EXTERNAL_HERMES_URL", "http://127.0.0.1:9119"))
    external_token: str = field(default_factory=lambda: _env("CC_EXTERNAL_HERMES_TOKEN", ""))

    # Chat with the agent. The OpenAI-compatible surface is what our UI speaks.
    chat_bridge_enabled: bool = field(default_factory=lambda: _env_bool("CC_CHAT_BRIDGE", True))
    chat_bridge_port: int = field(default_factory=lambda: _env_int("CC_CHAT_BRIDGE_PORT", 9120))
    chat_request_timeout: float = field(default_factory=lambda: float(_env_int("CC_CHAT_TIMEOUT", 300)))

    # ------------------------------------------------------------ first run
    # Optional: create the first administrator without touching the browser. Both
    # must be set, and the password is only ever read from the environment — it is
    # never stored in a config file, never logged, and never sent to the UI.
    bootstrap_admin_user: str = field(default_factory=lambda: _env("CC_ADMIN_USER", ""))
    bootstrap_admin_password: str = field(default_factory=lambda: _env("CC_ADMIN_PASSWORD", ""))

    # ------------------------------------------------------------- behaviour
    free_mode: bool = field(default_factory=lambda: _env_bool("CC_FREE_MODE", True))
    lock_paid_providers: bool = field(default_factory=lambda: _env_bool("CC_LOCK_PAID_PROVIDERS", True))
    allow_registration: bool = field(default_factory=lambda: _env_bool("CC_ALLOW_REGISTRATION", False))
    session_ttl_seconds: int = field(default_factory=lambda: _env_int("CC_SESSION_TTL", 60 * 60 * 24 * 7))
    secure_cookies: str = field(default_factory=lambda: _env("CC_SECURE_COOKIES", "auto").lower())
    trusted_proxies: tuple[str, ...] = field(default_factory=lambda: _env_list("CC_TRUSTED_PROXIES"))
    allowed_origins: tuple[str, ...] = field(default_factory=lambda: _env_list("CC_ALLOWED_ORIGINS"))

    # ------------------------------------------------------------------ logs
    log_level: str = field(default_factory=lambda: _env("CC_LOG_LEVEL", "INFO").upper())
    log_retention_days: int = field(default_factory=lambda: _env_int("CC_LOG_RETENTION_DAYS", 30))
    runtime_log_max_bytes: int = field(default_factory=lambda: _env_int("CC_RUNTIME_LOG_MAX_BYTES", 5 * 1024 * 1024))

    @property
    def version(self) -> str:
        return "1.0.0"

    def derived_home(self) -> Path:
        return self.home

    # --------------------------------------------------------------- helpers
    @property
    def sqlite_path(self) -> Path:
        return self.home / "control-center.db"

    @property
    def master_key_path(self) -> Path:
        return self.home / "secrets.key"

    @property
    def session_secret_path(self) -> Path:
        return self.home / "session.secret"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def backups_dir(self) -> Path:
        return self.home / "backups"

    @property
    def runtime_dir(self) -> Path:
        return self.home / "runtime"

    @property
    def reports_dir(self) -> Path:
        return self.home / "reports"

    def ensure_dirs(self) -> None:
        for directory in (self.home, self.logs_dir, self.backups_dir, self.runtime_dir, self.reports_dir):
            directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.home, 0o700)
        except OSError:  # pragma: no cover - non-POSIX filesystems
            pass

    def session_secret(self) -> str:
        """Stable per-install secret used to sign session cookies."""

        from_env = _env("CC_SESSION_SECRET")
        if from_env:
            return from_env
        self.ensure_dirs()
        path = self.session_secret_path
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = secrets.token_urlsafe(48)
        path.write_text(value, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover
            pass
        return value

    def summary(self) -> dict:
        """Redacted view for the UI and the diagnostics page."""

        return {
            "version": self.version,
            "host": self.host,
            "port": self.port,
            "home": str(self.home),
            "database": "postgresql" if self.database_url.startswith(("postgres://", "postgresql://")) else f"sqlite ({self.sqlite_path})",
            "hermes_home": str(self.hermes_home),
            "hermes_source": str(self.hermes_source),
            "hermes_python": self.hermes_python or "(auto-detected)",
            "runtime_mode": self.runtime_mode,
            "runtime_address": f"{self.runtime_host}:{self.runtime_port}",
            "runtime_autostart": self.runtime_autostart,
            "adopt_existing_backend": self.adopt_existing,
            "hermes_isolated": self.runtime_isolated,
            "runtime_auto_restart": self.runtime_auto_restart,
            "chat_bridge": f"{'enabled' if self.chat_bridge_enabled else 'disabled'} (127.0.0.1:{self.chat_bridge_port})",
            "free_mode": self.free_mode,
            "lock_paid_providers": self.lock_paid_providers,
            "secure_cookies": self.secure_cookies,
            "trusted_proxies": list(self.trusted_proxies),
            "log_level": self.log_level,
            "log_retention_days": self.log_retention_days,
            "static_dir": str(self.static_dir) if self.static_dir else "(bundled UI served from backend/app/static when present)",
        }


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.ensure_dirs()
    return _settings


def reset_settings_for_tests() -> None:
    global _settings
    _settings = None
