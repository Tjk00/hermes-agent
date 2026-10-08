"""Adopt an already-running Hermes backend instead of starting a second one.

Upstream Hermes allows exactly **one** machine-level backend per OS user (its
"multiplex-only" rule) and publishes a rendezvous record so other tools can find
it and authenticate. Two files matter, both owner-only:

    <state>/host-serve.json     role, pid, host, port, profiles, token fingerprint
    <state>/host-serve.token    the live session token of that backend

``<state>`` is ``$HERMES_GATEWAY_LOCK_DIR``, else
``$XDG_STATE_HOME/hermes/gateway-locks``, else ``~/.local/state/hermes/gateway-locks``.

Reading them is deliberate, not a hack: the record exists so "an attaching client
of the same OS user can authenticate". The Control Center therefore:

1. tries to **adopt** a live backend, so a user who already runs
   ``hermes dashboard`` keeps exactly one process and no surprise restarts;
2. otherwise **spawns its own** with a token it generated.

We never import Hermes to read these files, and we never trust a record blindly:
the port must answer ``/api/host/identity`` with the recorded pid and role, and
the on-disk token must match the record's fingerprint.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

ROLE_SERVE = "serve"
ROLE_DESKTOP_SERVE = "desktop-serve"
ROLE_GATEWAY = "gateway"
IDENTITY_PATH = "/api/host/identity"
PROTOCOL_VERSION = 1


@dataclass
class HostRecord:
    role: str
    pid: int
    port: int
    host: str = "127.0.0.1"
    home: str = ""
    create_time: float = 0.0
    start_time: float = 0.0
    protocol_version: int = 0
    token_fingerprint: str = ""
    profiles: list[str] = field(default_factory=list)
    updated_at: str = ""
    path: str = ""

    @classmethod
    def from_json(cls, payload: dict, *, path: Path) -> "HostRecord":
        return cls(
            role=str(payload.get("role") or ""),
            pid=int(payload.get("pid") or 0),
            port=int(payload.get("port") or 0),
            host=str(payload.get("host") or "127.0.0.1"),
            home=str(payload.get("home") or ""),
            create_time=float(payload.get("createTime") or 0.0),
            start_time=float(payload.get("startTime") or 0.0),
            protocol_version=int(payload.get("protocolVersion") or 0),
            token_fingerprint=str(payload.get("tokenFingerprint") or ""),
            profiles=list(payload.get("profiles") or []),
            updated_at=str(payload.get("updatedAt") or ""),
            path=str(path),
        )

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "pid": self.pid,
            "port": self.port,
            "host": self.host,
            "home": self.home,
            "protocol_version": self.protocol_version,
            "profiles": self.profiles,
            "updated_at": self.updated_at,
            "record_path": self.path,
        }


def state_dir() -> Path:
    override = os.environ.get("HERMES_GATEWAY_LOCK_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "hermes" / "gateway-locks"
    return Path.home() / ".local" / "state" / "hermes" / "gateway-locks"


def _is_own_file(path: Path) -> bool:
    """Same OS user, and not writable by anyone else."""

    try:
        info = path.stat()
    except OSError:
        return False
    if info.st_mode & 0o022:  # group- or world-writable: refuse, as upstream does
        return False
    getuid = getattr(os, "geteuid", None)
    if getuid is not None and info.st_uid != getuid():
        return False
    return True


def record_path(role: str) -> Path:
    return state_dir() / f"host-{role}.json"


def token_path(role: str) -> Path:
    return state_dir() / f"host-{role}.token"


def read_record(role: str) -> HostRecord | None:
    path = record_path(role)
    if not _is_own_file(path):
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    try:
        record = HostRecord.from_json(payload, path=path)
    except (TypeError, ValueError):
        return None
    if record.protocol_version and record.protocol_version > PROTOCOL_VERSION:
        # A newer handshake than we understand: do not guess.
        return None
    if record.pid <= 0:
        return None
    if record.role in {ROLE_SERVE, ROLE_DESKTOP_SERVE} and record.port <= 0:
        # A serve record without a port cannot be dialled.
        return None
    # The *gateway* role publishes no port on purpose (it hosts platforms, not an
    # HTTP surface), so a missing port there is normal — and its lock is exactly
    # what tells us whether some other gateway already owns this host.
    return record


def read_token(role: str) -> str:
    path = token_path(role)
    if not _is_own_file(path):
        return ""
    try:
        return path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""


def token_fingerprint(token: str) -> str:
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()[:16]


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def probe_identity(base_url: str, token: str, *, timeout: float = 5.0) -> dict | None:
    """Ask the recorded backend to identify itself with its published token."""

    headers = {"Accept": "application/json"}
    if token:
        headers["X-Hermes-Token"] = token
        headers["X-Hermes-Session-Token"] = token
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(f"{base_url.rstrip('/')}{IDENTITY_PATH}", headers=headers)
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def dial_host(record: HostRecord) -> str:
    host = record.host
    if host in {"", "0.0.0.0", "::", "[::]"}:
        return "127.0.0.1"
    return host


@dataclass
class AttachAttempt:
    adopted: bool
    record: HostRecord | None = None
    token: str = ""
    base_url: str = ""
    reason: str = ""
    identity: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "adopted": self.adopted,
            "base_url": self.base_url,
            "reason": self.reason,
            "identity": self.identity,
            "record": self.record.to_dict() if self.record else None,
        }


def attempt_adopt(*, timeout: float = 5.0) -> AttachAttempt:
    """Find a live upstream backend owned by this OS user, if there is one."""

    for role in (ROLE_SERVE, ROLE_DESKTOP_SERVE):
        record = read_record(role)
        if record is None:
            continue
        if not process_alive(record.pid):
            return AttachAttempt(False, record=record, reason=f"the recorded {role} process (pid {record.pid}) is not running")
        token = read_token(role)
        if not token:
            return AttachAttempt(False, record=record, reason="the record exists but its token file is missing or not readable")
        if record.token_fingerprint and token_fingerprint(token) != record.token_fingerprint:
            return AttachAttempt(
                False,
                record=record,
                reason="the published token does not match the record's fingerprint (the file may have been replaced)",
            )
        base_url = f"http://{dial_host(record)}:{record.port}"
        identity = probe_identity(base_url, token, timeout=timeout)
        if identity is None:
            return AttachAttempt(False, record=record, reason=f"{base_url} did not answer the identity probe")
        if int(identity.get("pid") or 0) not in {record.pid, 0}:
            return AttachAttempt(
                False,
                record=record,
                reason=f"the process behind {base_url} reported pid {identity.get('pid')} instead of {record.pid}",
            )
        return AttachAttempt(True, record=record, token=token, base_url=base_url, reason="live owner verified", identity=identity)
    return AttachAttempt(False, reason="no published Hermes host record for this user")


def describe(record: HostRecord | None) -> str:
    if record is None:
        return "no record"
    return f"{record.role} backend pid {record.pid} on {record.host}:{record.port}"


def forget_stale(role: str = ROLE_SERVE) -> bool:
    """Removes a record whose process is gone. Never touches a live owner."""

    record = read_record(role)
    if record is None:
        return False
    if process_alive(record.pid):
        return False
    with contextlib.suppress(OSError):
        record_path(role).unlink()
        token_path(role).unlink()
        return True
    return False


def wait_for_record(role: str = ROLE_SERVE, *, timeout: float = 20.0) -> HostRecord | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = read_record(role)
        if record is not None and process_alive(record.pid):
            return record
        time.sleep(0.4)
    return None
