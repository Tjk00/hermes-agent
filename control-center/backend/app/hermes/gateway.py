"""Supervise the Hermes **gateway** — the process that hosts chat platforms.

Why this exists
---------------
The agent's OpenAI-compatible API (`platforms.api_server`) is a *gateway* platform,
so "chat with the agent" needs a running gateway, not just the dashboard. Upstream's
``POST /api/gateway/start`` normally hands that job to systemd/launchd; in a
container, on a free tier, or on a machine without user D-Bus that fails with
"User systemd not reachable".

Upstream provides the escape hatch for exactly this situation::

    hermes gateway run --external-supervisor

— a foreground gateway that declares *us* as its process manager. This class does
that part: spawn it, keep its output, stop it, and report honestly whether the
bridge port answers afterwards.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import rendezvous

STATE_STOPPED = "stopped"
STATE_RUNNING = "running"
STATE_ERROR = "error"
STATE_EXTERNAL = "external"


@dataclass
class GatewayStatus:
    state: str = STATE_STOPPED
    pid: int | None = None
    port: int = 0
    reachable: bool = False
    uptime_seconds: float = 0.0
    exit_code: int | None = None
    last_error: str = ""
    command: list[str] = field(default_factory=list)
    log_file: str = ""
    owns_process: bool = True
    note: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class GatewaySupervisor:
    """Runs ``hermes gateway run --external-supervisor`` as a child process."""

    def __init__(
        self,
        settings,
        *,
        interpreter: str = "",
        secrets_provider: Callable[[], dict[str, str]] | None = None,
        on_event: Callable[..., None] | None = None,
    ) -> None:
        self.settings = settings
        self._interpreter = interpreter
        self._secrets_provider = secrets_provider or (lambda: {})
        self._on_event = on_event
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._state = STATE_STOPPED
        self._started_at: float | None = None
        self._exit_code: int | None = None
        self._last_error = ""
        self._command: list[str] = []
        self._note = ""
        self._external_pid: int | None = None
        self._force_retry = False
        self._log_path = settings.logs_dir / "hermes-gateway.log"
        self._ring: deque[str] = deque(maxlen=300)

    # ------------------------------------------------------------------ basics
    @property
    def log_file(self) -> Path:
        return self._log_path

    @property
    def port(self) -> int:
        return int(self.settings.chat_bridge_port)

    def log_lines(self, limit: int = 200) -> list[str]:
        with self._lock:
            if self._ring:
                return list(self._ring)[-limit:]
        return _tail(self._log_path, limit)

    def _event(self, event: str, detail: str = "", **kwargs) -> None:
        if self._on_event:
            with contextlib.suppress(Exception):
                self._on_event(event, detail, **kwargs)

    def running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def external_gateway(self) -> dict:
        """Who owns the host gateway lock right now, and is that owner alive?

        ``hermes gateway run`` refuses to start while the lock is held — exit code
        75, "one gateway per host". A lock left behind by a gateway that is already
        dead is very common (a crashed process, a killed container), and the fix is
        upstream's own ``--force`` flag. Distinguishing the two cases is the
        difference between "chat works" and "chat is mysteriously broken".
        """

        try:
            record = rendezvous.read_record(rendezvous.ROLE_GATEWAY)
        except Exception:  # pragma: no cover - defensive, lock files are best effort
            record = None
        if record is None:
            return {"present": False, "alive": False, "stale": False, "ours": False, "pid": None, "detail": ""}
        own_pid = self._process.pid if self._process is not None else None
        alive = rendezvous.process_alive(record.pid) if record.pid else False
        ours = bool(own_pid) and record.pid == own_pid
        stale = not alive
        detail = (
            f"Another Hermes gateway holds this host's gateway lock (pid {record.pid})."
            if alive
            else f"A previous Hermes gateway left its lock behind (pid {record.pid}, no longer running)."
        )
        return {"present": True, "alive": alive, "stale": stale, "ours": ours, "pid": record.pid, "detail": detail}

    def port_free(self) -> bool:
        """Can something bind our bridge port right now?"""

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", self.port))
                return True
        except OSError:
            return False

    # ------------------------------------------------------------------- start
    def start(
        self, *, wait: bool = True, timeout: float = 45.0, interpreter: str = "", _retry: bool = False
    ) -> dict:
        with self._lock:
            if self.running():
                return self.status()
        if not _retry:
            self._force_retry = False
        interpreter = interpreter or self._interpreter
        if not interpreter:
            from .locate import detect_install

            interpreter = detect_install(self.settings).selected_python
        if not interpreter:
            self._last_error = "No Python interpreter with the Hermes dependencies was found."
            self._state = STATE_ERROR
            return self.status()

        # One gateway per host: upstream keeps a lock so two gateways never fight
        # over the same platforms. If another *live* gateway already serves this
        # host, do not take it over — adopt it instead. If the lock is a leftover
        # from a gateway that is already gone, `--force` is the documented way to
        # reclaim it (otherwise the child exits 75 and chat looks broken).
        lock = self.external_gateway()
        if lock["alive"] and not lock["ours"] and self.probe():
            self._state = STATE_RUNNING
            self._process = None
            self._external_pid = lock["pid"]
            self._exit_code = None
            self._last_error = ""
            self._command = []
            self._note = (
                f"A Hermes gateway started outside the Control Center (pid {lock['pid']}) is already "
                "serving this host, and the chat bridge answers. It will not be taken over."
            )
            self._event("gateway_attached", self._note)
            return self.status()

        self._command = [
            interpreter,
            "-m",
            "hermes_cli.main",
            "gateway",
            "run",
            "--external-supervisor",
            "--no-supervise",
        ]
        if lock["stale"] or self._force_retry:
            self._command.append("--force")
        env = dict(os.environ)
        env.update(
            {
                "HERMES_HOME": str(self.settings.hermes_home),
                "HERMES_NONINTERACTIVE": "1",
                "HERMES_GATEWAY_EXTERNAL_SUPERVISOR": "1",
                "PYTHONUNBUFFERED": "1",
                "NO_COLOR": "1",
            }
        )
        env.update({k: v for k, v in self._secrets_provider().items() if v})
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._log_path.exists():
            self._log_path.touch()
        with contextlib.suppress(OSError):
            self._log_path.chmod(0o600)
        _tail_into(self._log_path, self._ring)

        if not self.port_free():
            # Something already listens where our gateway must bind. Spawning anyway
            # would produce a child that dies with a bind error and a log line nobody
            # reads; say what is actually happening instead.
            owner = _port_owner(self.port) or "unknown process"
            self._command = []
            self._external_pid = None
            self._state = STATE_ERROR
            self._last_error = (
                f"Port {self.port} is already in use by {owner}. Another Hermes gateway is probably running "
                "with a different configuration — stop it, or change this Control Center's bridge port."
            )
            self._event("gateway_port_busy", self._last_error)
            return self.status()

        try:
            handle = self._log_path.open("a", encoding="utf-8", buffering=1)
            self._process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                self._command,
                cwd=str(self.settings.hermes_source),
                env=env,
                stdout=handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            self._state = STATE_ERROR
            self._last_error = f"The gateway process could not be launched: {exc}"
            self._event("gateway_start_failed", self._last_error)
            return self.status()
        self._state = STATE_RUNNING
        self._started_at = time.time()
        self._exit_code = None
        self._last_error = ""
        self._note = ""
        self._external_pid = None
        self._event("gateway_started", f"pid {self._process.pid}")
        threading.Thread(target=self._monitor, name="cc-gateway-monitor", daemon=True).start()
        if wait:
            deadline = time.time() + timeout
            while time.time() < deadline:
                if self.probe():
                    break
                if not self.running():
                    break
                time.sleep(0.7)
        status = self.status()
        adopted = self._adopt_if_taken()
        if adopted is not None:
            return adopted
        if status["state"] == STATE_ERROR and status.get("exit_code") == 75 and not self._force_retry:
            # The lock conflict happened even though we believed it was clear (a
            # gateway may have started between the check and the spawn). Retry once
            # with the flag upstream documents for exactly this case.
            self._last_error = ""
            self._note = ""
            self._force_retry = True
            self._event("gateway_retry_force", "retrying with --force after a host-lock conflict")
            return self.start(wait=wait, timeout=timeout, interpreter=interpreter, _retry=True)
        return status

    def _adopt_if_taken(self) -> dict | None:
        """If our child died because another gateway owns the host, adopt that one."""

        if self._state != STATE_ERROR:
            return None
        found = existing_gateway(self.log_lines(60))
        if not found["taken"]:
            return None
        with self._lock:
            self._external_pid = found["pid"] if found["pid"] and rendezvous.process_alive(found["pid"]) else None
            self._state = STATE_EXTERNAL
            self._last_error = ""
            self._exit_code = None
            self._command = []
            self._note = (
                "Another Hermes gateway already serves this host"
                + (f" (pid {self._external_pid})" if self._external_pid else "")
                + ", so the Control Center did not start a second one. It answers the chat bridge on the port that "
                "gateway is configured with — press Enable again after stopping the other gateway if you want the "
                "Control Center to own it."
            )
        self._event("gateway_adopted", self._note)
        return self.status()

    # -------------------------------------------------------------------- stop
    def stop(self, *, timeout: float = 15.0) -> dict:
        with self._lock:
            process = self._process
            self._external_pid = None
            if process is None or process.poll() is not None:
                self._process = None
                self._state = STATE_STOPPED
                self._note = ""
                return self.status()
        self._event("gateway_stopping", f"pid {process.pid}")
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
        with self._lock:
            self._exit_code = process.returncode
            self._process = None
            self._state = STATE_STOPPED
            self._started_at = None
            self._note = ""
            self._external_pid = None
        self._event("gateway_stopped", f"exit code {self._exit_code}")
        return self.status()

    def restart(self, *, interpreter: str = "") -> dict:
        self.stop()
        return self.start(wait=True, interpreter=interpreter)

    # ------------------------------------------------------------------ status
    def probe(self, *, timeout: float = 4.0) -> bool:
        import socket

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                return sock.connect_ex(("127.0.0.1", self.port)) == 0
        except OSError:
            return False

    def status(self) -> dict:
        with self._lock:
            process = self._process
            pid = process.pid if process and process.poll() is None else self._external_pid
            if self._state == STATE_EXTERNAL and self._external_pid is None:
                # Adopted without knowing the pid: keep the state, it is still true
                # that some other gateway owns the host.
                pass
            if self._external_pid and not rendezvous.process_alive(self._external_pid):
                # The gateway we adopted is gone: stop claiming it is ours.
                self._external_pid = None
                self._note = ""
                self._state = STATE_STOPPED
                pid = None
            if process is not None and process.poll() is not None:
                if self._state == STATE_RUNNING:
                    self._exit_code = process.returncode
                    self._last_error = f"The gateway exited with code {process.returncode}."
                    if process.returncode == 75:
                        self._last_error += " (exit 75 is Hermes' 'another gateway already serves this host' code)"
                    self._state = STATE_ERROR
                self._process = None
                pid = None
        status = GatewayStatus(
            state=self._state,
            pid=pid,
            port=self.port,
            reachable=self.probe(),
            uptime_seconds=(
                (time.time() - self._started_at)
                if (pid and self._started_at and self._external_pid is None)
                else 0.0
            ),
            exit_code=self._exit_code,
            last_error=self._last_error,
            command=list(self._command),
            log_file=str(self._log_path),
            owns_process=not bool(self._external_pid),
            note=(
                self._note
                or (
                    "This gateway is a child process of the Control Center "
                    "(hermes gateway run --external-supervisor), so chat works on hosts without systemd."
                )
            ),
        )
        if status.state == STATE_ERROR and not status.last_error:
            status.last_error = "The gateway is not running. Start it from the Chat page."
        return status.to_dict()

    # --------------------------------------------------------------- internals
    def _monitor(self) -> None:
        while True:
            time.sleep(1.0)
            with self._lock:
                process = self._process
                if process is None:
                    return
                code = process.poll()
                if code is None:
                    _tail_into(self._log_path, self._ring)
                    continue
                self._exit_code = code
                self._process = None
                self._started_at = None
                self._state = STATE_ERROR
            self._last_error = f"The gateway exited with code {code}."
            if code == 75:
                self._last_error += " (exit 75 is Hermes' 'another gateway already serves this host' code)"
            self._event("gateway_crashed", self._last_error, tail=self.log_lines(12))
            return

    def shutdown(self) -> None:
        if self.running():
            self.stop()


GATEWAY_TAKEN = re.compile(r"already serves profile|nothing to start", re.I)
GATEWAY_PID = re.compile(r"PID\s+(\d+)")


def existing_gateway(lines: list[str]) -> dict:
    """Read upstream's refusal message and report the gateway it points at.

    ``hermes gateway run`` exits 75 when another gateway already serves this host and
    prints which process that is. Failing here would leave chat dead while a perfectly
    usable gateway is running, so we adopt it and say so.
    """

    text = "\n".join(lines[-40:])
    if not GATEWAY_TAKEN.search(text):
        return {"taken": False, "pid": None}
    pid_match = GATEWAY_PID.search(text)
    return {"taken": True, "pid": int(pid_match.group(1)) if pid_match else None}


def _tail(path: Path, limit: int) -> list[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return [line.rstrip("\n") for line in deque(handle, maxlen=limit)]
    except OSError:
        return []


def _tail_into(path: Path, ring: deque) -> None:
    lines = _tail(path, ring.maxlen or 300)
    ring.clear()
    ring.extend(lines)


def _port_owner(port: int) -> str:
    """Describe whatever is listening on ``port`` — best effort, Linux-first.

    Uses ``/proc/net/tcp`` to find the socket's inode and then matches it against
    ``/proc/<pid>/fd``. Returns an empty string when it cannot tell.
    """

    try:
        inode = ""
        hex_port = f"{port:04X}"
        for table in ("/proc/net/tcp", "/proc/net/tcp6"):
            with open(table, encoding="utf-8") as handle:
                next(handle, None)
                for line in handle:
                    parts = line.split()
                    if len(parts) < 10 or parts[3] != "0A":
                        continue
                    if parts[1].split(":")[-1].upper() == hex_port:
                        inode = parts[9]
                        break
            if inode:
                break
        if not inode:
            return ""
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            fd_dir = f"/proc/{entry}/fd"
            try:
                for name in os.listdir(fd_dir):
                    if os.readlink(f"{fd_dir}/{name}") == f"socket:[{inode}]":
                        cmdline = Path(f"/proc/{entry}/cmdline").read_bytes().replace(b"\x00", b" ").decode(
                            "utf-8", "replace"
                        )
                        return f"pid {entry} ({cmdline.strip()[:120]})"
            except OSError:
                continue
    except OSError:
        return ""
    return ""
