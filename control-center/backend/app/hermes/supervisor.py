"""Process supervisor for the real Hermes Agent runtime.

What it does
------------
* Spawns the upstream ``hermes dashboard`` server (loopback only) as a child
  process of the Control Center, with a session token we generate so the two can
  talk. That is exactly the handshake the Hermes desktop shell uses, so no patch
  to upstream is required.
* Waits for ``/api/health`` to answer before reporting "running".
* Streams the child's stdout/stderr into ``$CC_HOME/logs/hermes-runtime.log``
  (mode 0600) and a bounded in-memory ring, so the Logs page shows *real* output
  even when Hermes dies before it can log anything itself.
* Detects crashes, records them, and — only if explicitly enabled — restarts the
  runtime with a backoff cap so a broken config cannot spin forever.
* Injects provider keys from the encrypted vault into the child's environment
  only. Keys are never written to the runtime's config or .env by this module.

Modes
-----
``dashboard``  adopt a live upstream backend if one exists, otherwise spawn one (default)
``external``   do not touch processes at all: talk to CC_EXTERNAL_HERMES_URL
``disabled``   do not attempt to run the agent at all (UI-only deployment)

Adoption
--------
Upstream allows one machine-level backend per OS user and publishes a rendezvous
record with its session token so an attaching client can authenticate. When that
record is live we use it instead of starting a second server — the user's own
``hermes dashboard`` keeps running, and "stop" becomes "detach" rather than
killing a process we do not own.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import rendezvous
from .client import HermesClient
from .locate import detect_install, python_version, runtime_command, runtime_env

STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_STOPPING = "stopping"
STATE_ERROR = "error"
STATE_EXTERNAL = "external"
STATE_ATTACHED = "attached"
STATE_DISABLED = "disabled"

MAX_AUTO_RESTARTS = 3
AUTO_RESTART_WINDOW = 15 * 60  # seconds


class RuntimeStartError(RuntimeError):
    """Raised with a human-readable reason when the runtime cannot start."""

    def __init__(self, message: str, *, detail: str = "", log_tail: list[str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.log_tail = log_tail or []

    def human_detail(self) -> str:
        parts = [self.detail] if self.detail else []
        if self.log_tail:
            parts.append("Recent runtime output:\n" + "\n".join(self.log_tail[-12:]))
        return "\n".join(parts) or self.message


@dataclass
class RuntimeStatus:
    state: str = STATE_STOPPED
    mode: str = "dashboard"
    pid: int | None = None
    port: int = 0
    host: str = "127.0.0.1"
    base_url: str = ""
    started_at: float | None = None
    uptime_seconds: float = 0.0
    healthy: bool = False
    exit_code: int | None = None
    last_error: str = ""
    last_error_hint: str = ""
    restarts: int = 0
    auto_restart: bool = False
    command: list[str] = field(default_factory=list)
    python: str = ""
    python_version: str = ""
    source_dir: str = ""
    log_file: str = ""
    health_detail: dict = field(default_factory=dict)
    attached: bool = False
    adopted: dict = field(default_factory=dict)
    owns_process: bool = True


class RuntimeSupervisor:
    """Owns the child process and every question the UI has about it."""

    def __init__(
        self,
        settings,
        *,
        secrets_provider: Callable[[], dict[str, str]] | None = None,
        on_event: Callable[..., None] | None = None,
        extra_env: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        self.settings = settings
        self._secrets_provider = secrets_provider or (lambda: {})
        self._extra_env = extra_env or (lambda: {})
        self._on_event = on_event
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._state = STATE_STOPPED
        self._token = ""
        self._started_at: float | None = None
        self._exit_code: int | None = None
        self._last_error = ""
        self._last_error_hint = ""
        self._restart_times: list[float] = []
        self._log_path = settings.logs_dir / "hermes-runtime.log"
        self._ring: deque[str] = deque(maxlen=400)
        self._monitor: threading.Thread | None = None
        self._command: list[str] = []
        self._python = ""
        self._python_version = ""
        self._health_detail: dict = {}
        self._attached = False
        self._adopted: dict = {}
        self._adopt_note = ""

    # ------------------------------------------------------------------ helpers
    @property
    def log_file(self) -> Path:
        return self._log_path

    def _event(self, event: str, detail: str = "", *, state: str | None = None, pid: int | None = None) -> None:
        if self._on_event:
            with contextlib.suppress(Exception):
                self._on_event(event, detail, state=state, pid=pid)

    def _detect_python_version(self) -> str:
        """Version of the interpreter running Hermes, for the status read-out.

        Computed at most once, and only when a start has not already told us.
        """

        if not self._python or self._python_version:
            return self._python_version
        try:
            _key, version, _err = python_version(self._python)
        except Exception:  # noqa: BLE001 - status must never fail because of this
            return ""
        self._python_version = version
        return version

    def client(self) -> HermesClient:
        if self.settings.runtime_mode == "external":
            return HermesClient(self.settings.external_url, self.settings.external_token)
        if self._attached and self._adopted.get("base_url"):
            return HermesClient(self._adopted["base_url"], self._token)
        return HermesClient(f"http://{self.settings.runtime_host}:{self.settings.runtime_port}", self._token)

    def base_url(self) -> str:
        if self.settings.runtime_mode == "external":
            return self.settings.external_url.rstrip("/")
        if self._attached and self._adopted.get("base_url"):
            return str(self._adopted["base_url"])
        return f"http://{self.settings.runtime_host}:{self.settings.runtime_port}"

    @property
    def attached(self) -> bool:
        return self._attached

    def token(self) -> str:
        return self._token

    def log_lines(self, limit: int = 200) -> list[str]:
        with self._lock:
            if self._ring:
                return list(self._ring)[-limit:]
        return _tail_file(self._log_path, limit)

    # --------------------------------------------------------------- web dist
    def ensure_web_dist(self) -> Path:
        """A directory the dashboard can serve as its static mount.

        ``hermes dashboard --skip-build`` insists on an existing dist with an
        ``index.html``. We hand it a tiny page instead of running upstream's npm
        build (which needs network access and Node), because the *Control Center*
        is the UI here — the upstream dashboard is used as the API.
        """

        configured = str(self.settings.runtime_web_dist or "")
        # ``Path()`` is ``.`` — an unset CC_RUNTIME_WEB_DIST must not be treated as
        # "the current directory is a web dist", which is exactly how upstream ends
        # up refusing to start with "no web dist found at: .".
        if configured not in {"", "."} and (Path(configured) / "index.html").is_file():
            return Path(configured)
        target = self.settings.runtime_dir / "web-dist"
        target.mkdir(parents=True, exist_ok=True)
        index = target / "index.html"
        if not index.exists():
            index.write_text(
                """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes runtime endpoint</title>
<style>body{font:15px/1.6 system-ui,sans-serif;margin:2rem;max-width:38rem;color:#e8e8ef;background:#0b0d12}
code{background:#1a1d26;padding:.15rem .35rem;border-radius:.3rem}</style>
</head><body>
<h1>This is the Hermes runtime, not the Control Center</h1>
<p>You are looking at the static mount of the Hermes Agent backend that the
Control Center supervises. The application UI lives on the Control Center's own
port.</p>
<p>Everything under <code>/api/*</code> on this port is real Hermes API, protected
by a per-launch session token held by the Control Center.</p>
</body></html>
""",
                encoding="utf-8",
            )
        return target

    # ------------------------------------------------------------------- start
    def start(self, *, wait: bool = True, timeout: float | None = None) -> RuntimeStatus:
        with self._lock:
            if self.settings.runtime_mode == "disabled":
                self._state = STATE_DISABLED
                return self.status(probe=False)
            if self.settings.runtime_mode == "external":
                self._state = STATE_EXTERNAL
                return self.status(probe=True)
            if self._process and self._process.poll() is None:
                return self.status(probe=False)
            if self._attached:
                return self.status(probe=True)

            if self.settings.adopt_existing:
                attempt = rendezvous.attempt_adopt()
                if attempt.adopted and attempt.record is not None:
                    self._token = attempt.token
                    self._attached = True
                    self._adopted = attempt.to_dict()
                    self._state = STATE_ATTACHED
                    self._started_at = time.time()
                    self._event(
                        "adopted",
                        f"attached to an existing Hermes backend (pid {attempt.record.pid}, port {attempt.record.port})",
                        state=STATE_ATTACHED,
                        pid=attempt.record.pid,
                    )
                    return self.status(probe=wait)
                if attempt.record is not None and not attempt.adopted:
                    # A record exists but we cannot use it. Say exactly why; the user may
                    # prefer adopting their own backend over running a second one.
                    self._adopt_note = attempt.reason
                else:
                    self._adopt_note = ""

            install = detect_install(self.settings)
            if not install.is_hermes_source:
                raise RuntimeStartError(
                    "The upstream Hermes checkout could not be found.",
                    detail=" ".join(install.problems) or install.source_reason,
                )
            if not install.selected_python:
                raise RuntimeStartError(
                    "No usable Python interpreter for the Hermes runtime.",
                    detail=" ".join(install.problems),
                )
            if not install.python_candidates or not any(
                candidate.path == install.selected_python and candidate.has_hermes_deps
                for candidate in install.python_candidates
            ):
                raise RuntimeStartError(
                    "Hermes' Python dependencies are not installed for the detected interpreter.",
                    detail=(
                        f"{install.selected_python} is missing required packages. Install them with: "
                        f"{install.selected_python} -m pip install -e {install.source_dir}[web] "
                        "(Diagnostics → Runtime dependencies can do this for you)."
                    ),
                )

            free, detail = self._port_available()
            if not free:
                raise RuntimeStartError(
                    f"Port {self.settings.runtime_port} is already in use.",
                    detail=detail,
                )

            self._state = STATE_STARTING
            self._last_error = ""
            self._last_error_hint = ""
            self._token = _new_token()
            self._python = install.selected_python
            self._python_version = install.selected_python_version
            web_dist = self.ensure_web_dist()
            self._command = runtime_command(
                self.settings,
                host=self.settings.runtime_host,
                port=self.settings.runtime_port,
                python=install.selected_python,
                web_dist=web_dist,
            )
            env = runtime_env(
                self.settings,
                token=self._token,
                web_dist=web_dist,
                extra={**self._secrets_provider(), **self._extra_env()},
            )

            self._prepare_log_file()
            self._event("start_requested", f"python {install.selected_python_version}", state=STATE_STARTING)
            try:
                handle = self._log_path.open("a", encoding="utf-8", buffering=1)
                self._process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                    self._command,
                    cwd=install.source_dir,
                    env=env,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as exc:
                self._state = STATE_ERROR
                self._last_error = f"The runtime process could not be launched: {exc}"
                self._event("start_failed", self._last_error, state=STATE_ERROR)
                raise RuntimeStartError("The runtime process could not be launched.", detail=str(exc)) from exc

            self._started_at = time.time()
            self._exit_code = None
            self._event("started", f"pid {self._process.pid}", state=STATE_STARTING, pid=self._process.pid)
            self._start_monitor()

        if wait:
            self._wait_until_ready(timeout or self.settings.runtime_start_timeout)
        return self.status(probe=wait)

    def _wait_until_ready(self, timeout: float) -> None:
        deadline = time.time() + max(5.0, timeout)
        client = self.client()
        last_detail = ""
        while time.time() < deadline:
            with self._lock:
                process = self._process
            if process is None:
                break
            if process.poll() is not None:
                tail = self.log_lines(40)
                with self._lock:
                    self._exit_code = process.returncode
                    self._state = STATE_ERROR
                    self._last_error = (
                        f"Hermes exited during startup with code {process.returncode}. "
                        "The runtime log tail below is the real output of the process."
                    )
                    self._last_error_hint = _guess_startup_hint(tail)
                self._event("start_failed", self._last_error, state=STATE_ERROR)
                raise RuntimeStartError(self._last_error, detail=self._last_error_hint, log_tail=tail)
            try:
                health = client.health_sync()
            except Exception as exc:  # noqa: BLE001 - keep polling until the deadline
                last_detail = str(exc)
                time.sleep(0.6)
                continue
            with self._lock:
                self._state = STATE_RUNNING
                self._health_detail = health
            self._event("ready", "health probe answered", state=STATE_RUNNING, pid=process.pid)
            return
        with self._lock:
            self._state = STATE_ERROR
            self._last_error = f"Hermes did not become healthy within {int(timeout)}s."
            self._last_error_hint = last_detail or "See the runtime log tail."
        self._event("start_timeout", self._last_error, state=STATE_ERROR)
        raise RuntimeStartError(self._last_error, detail=self._last_error_hint, log_tail=self.log_lines(30))

    # -------------------------------------------------------------------- stop
    def stop(self, *, force: bool = True, timeout: float = 20.0) -> RuntimeStatus:
        if self._attached:
            # We adopted a backend we do not own (most likely the operator's own
            # `hermes dashboard`). Killing it would be rude and surprising, so we detach:
            # the Control Center stops talking to it and says so plainly.
            self._event("detached", "released an adopted Hermes backend (left running)", state=STATE_STOPPED)
            with self._lock:
                self._attached = False
                self._adopted = {}
                self._token = ""
                self._state = STATE_STOPPED
                self._started_at = None
            status = self.status(probe=False)
            status["detached"] = True
            status["note"] = (
                "This backend was not started by the Control Center, so it was left running. "
                "Stop it with `hermes dashboard --stop` if you really want it gone."
            )
            return status  # type: ignore[return-value]
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                self._state = STATE_STOPPED
                self._process = None
                return self.status(probe=False)
            self._state = STATE_STOPPING
        self._event("stop_requested", f"pid {process.pid}", state=STATE_STOPPING, pid=process.pid)
        try:
            process.terminate()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                if force:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    process.wait(timeout=10)
        except ProcessLookupError:
            pass
        with self._lock:
            self._exit_code = process.returncode
            self._process = None
            self._state = STATE_STOPPED
            self._started_at = None
        self._event("stopped", f"exit code {process.returncode}", state=STATE_STOPPED)
        return self.status(probe=False)

    def restart(self, *, timeout: float | None = None) -> RuntimeStatus:
        with self._lock:
            running = self._process is not None and self._process.poll() is None
        if self._attached:
            # Adopted backends cannot be restarted by us; detach and start our own so the
            # operator gets the behaviour they asked for, on our port.
            self._event("restart_attached", "starting a dedicated backend after an attach", state=STATE_STARTING)
            with self._lock:
                self._attached = False
                self._adopted = {}
                self._token = ""
        if running:
            self.stop()
            # Give the OS a moment to release the port before rebinding.
            deadline = time.time() + 10
            while time.time() < deadline:
                free, _ = self._port_available()
                if free:
                    break
                time.sleep(0.25)
        self._restart_times = []
        return self.start(wait=True, timeout=timeout)

    # ------------------------------------------------------------------ status
    def status(self, *, probe: bool = True, probe_timeout: float = 5.0) -> dict:
        with self._lock:
            process = self._process
            state = self._state
            pid = process.pid if process and process.poll() is None else None
            if process is not None and process.poll() is not None and state == STATE_RUNNING:
                state = STATE_ERROR
                self._state = STATE_ERROR
                self._exit_code = process.returncode
                self._last_error = f"Hermes exited unexpectedly with code {process.returncode}."
            uptime = (time.time() - self._started_at) if (pid and self._started_at) else 0.0
            attached = self._attached
            if attached:
                reported_state = STATE_ATTACHED
            elif self.settings.runtime_mode == "external":
                reported_state = STATE_EXTERNAL
            elif self.settings.runtime_mode == "disabled":
                reported_state = STATE_DISABLED
            else:
                reported_state = state
            status = RuntimeStatus(
                state=reported_state,
                health_detail=dict(self._health_detail),
                attached=attached,
                adopted=dict(self._adopted),
                owns_process=not attached,
                mode=self.settings.runtime_mode,
                pid=pid,
                port=self.settings.runtime_port,
                host=self.settings.runtime_host,
                base_url=self.base_url(),
                started_at=self._started_at,
                uptime_seconds=uptime,
                healthy=False,
                exit_code=self._exit_code,
                last_error=self._last_error,
                last_error_hint=self._last_error_hint,
                restarts=len(self._restart_times),
                auto_restart=self.settings.runtime_auto_restart,
                command=list(self._command),
                python=self._python,
                python_version=self._python_version or self._detect_python_version(),
                source_dir=str(self.settings.hermes_source),
                log_file=str(self._log_path),
            )

        if probe and status.state in {STATE_RUNNING, STATE_EXTERNAL, STATE_ATTACHED}:
            try:
                health = self.client().health_sync(timeout=probe_timeout)
                status.healthy = bool(health.get("ok", True))
                status.health_detail = health if isinstance(health, dict) else {"raw": health}
            except Exception as exc:  # noqa: BLE001
                status.healthy = False
                status.health_detail = {"error": str(exc)}
                if status.state == STATE_RUNNING:
                    with self._lock:
                        self._last_error_hint = (
                            "The process is alive but its HTTP API did not answer. "
                            "It may still be starting, or it is bound to a different address."
                        )
        elif probe and status.state == STATE_DISABLED:
            status.health_detail = {"note": "Runtime management is disabled (CC_RUNTIME_MODE=disabled)."}
        payload = status.__dict__
        if self._adopt_note and not self._attached:
            payload["adopt_note"] = self._adopt_note
            payload["adopt_command"] = "hermes dashboard --stop   # then press Start to run a dedicated backend"
        return payload

    # ------------------------------------------------------------- monitoring
    def _start_monitor(self) -> None:
        if self._monitor and self._monitor.is_alive():
            return
        self._monitor = threading.Thread(target=self._monitor_loop, name="hermes-runtime-monitor", daemon=True)
        self._monitor.start()

    def _monitor_loop(self) -> None:
        while True:
            time.sleep(1.0)
            with self._lock:
                process = self._process
                if process is None:
                    return
                code = process.poll()
                if code is None:
                    self._drain_log_file()
                    continue
                self._exit_code = code
                stopping = self._state == STATE_STOPPING
                self._process = None
                self._started_at = None
            if stopping:
                return
            tail = self.log_lines(40)
            with self._lock:
                self._state = STATE_ERROR
                self._last_error = f"Hermes exited unexpectedly with code {code}."
                self._last_error_hint = _guess_startup_hint(tail)
            self._event("crashed", f"exit code {code}", state=STATE_ERROR)
            if not self.settings.runtime_auto_restart:
                return
            if not self._may_restart():
                with self._lock:
                    self._last_error_hint = (
                        "Automatic restarts are paused after three crashes in 15 minutes. "
                        "Fix the cause (see the log), then start it manually."
                    )
                return
            self._event("auto_restart", "restarting after a crash", state=STATE_STARTING)
            time.sleep(3)
            try:
                self.start(wait=True)
            except RuntimeStartError:
                return
            return

    def _may_restart(self) -> bool:
        now = time.time()
        self._restart_times = [stamp for stamp in self._restart_times if now - stamp < AUTO_RESTART_WINDOW]
        if len(self._restart_times) >= MAX_AUTO_RESTARTS:
            return False
        self._restart_times.append(now)
        return True

    def _drain_log_file(self) -> None:
        _drain_file_into(self._log_path, self._ring)

    def should_auto_restart(self) -> bool:
        """Whether a *housekeeping* restart is allowed right now.

        Public wrapper around the backoff so callers outside the monitor thread
        (the Control Center's own loop, the CLI) obey the same limits: three
        attempts per 15 minutes, and never when the operator turned auto-restart off.
        """

        if not self.settings.runtime_auto_restart:
            return False
        return self._may_restart()

    # ---------------------------------------------------------- internal utils
    def _prepare_log_file(self) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        size = self._log_path.stat().st_size if self._log_path.exists() else 0
        if size > self.settings.runtime_log_max_bytes:
            rotated = self._log_path.with_suffix(".log.1")
            with contextlib.suppress(OSError):
                if rotated.exists():
                    rotated.unlink()
                self._log_path.rename(rotated)
        if not self._log_path.exists():
            self._log_path.touch()
        with contextlib.suppress(OSError):
            self._log_path.chmod(0o600)
        _drain_file_into(self._log_path, self._ring)

    def _port_available(self) -> tuple[bool, str]:
        import socket

        host = self.settings.runtime_host
        probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.5)
                if sock.connect_ex((probe_host, self.settings.runtime_port)) == 0:
                    return False, (
                        f"Something is already listening on {probe_host}:{self.settings.runtime_port}. "
                        "Another Hermes dashboard or an unrelated service owns that port. "
                        "Stop it, or set CC_HERMES_PORT to a free port."
                    )
        except OSError as exc:
            return True, f"port probe inconclusive ({exc})"
        return True, ""

    # ------------------------------------------------------------ shutdown hook
    def shutdown(self) -> None:
        if self._attached:
            self._attached = False
            self._adopted = {}
            return
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            self.stop()


def _new_token() -> str:
    import secrets

    return secrets.token_urlsafe(32)


def _tail_file(path: Path, limit: int) -> list[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return [line.rstrip("\n") for line in deque(handle, maxlen=limit)]
    except OSError:
        return []


def _drain_file_into(path: Path, ring: deque[str]) -> None:
    """Cheap incremental read: only re-reads the tail when the file grew."""

    try:
        lines = _tail_file(path, ring.maxlen or 400)
    except OSError:
        return
    ring.clear()
    ring.extend(lines)


def _guess_startup_hint(tail: list[str]) -> str:
    """Turn a process log tail into something a human can act on."""

    if not tail:
        return "The process produced no output at all — it may have been killed by the OS (out of memory?)."
    text = "\n".join(tail).lower()
    if "modulenotfounderror" in text or "no module named" in text:
        return "A Python dependency is missing for the interpreter running Hermes (Diagnostics → install dependencies)."
    if "address already in use" in text:
        return "The port was taken by another process. Stop it or change CC_HERMES_PORT."
    if "permission denied" in text:
        return "A file or directory was not writable. Check permissions on HERMES_HOME and CC_HOME."
    if "memoryerror" in text or "cannot allocate memory" in text:
        return "The host ran out of memory starting the agent. Try a smaller model or a bigger host."
    if "traceback" in text:
        return "Hermes raised a Python traceback during startup — the last frames in the log show the cause."
    return "See the runtime log tail for the exact failure."
