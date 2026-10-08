"""Gateway supervision: the difference between "chat works" and "chat is broken".

Hermes allows one gateway per host and records the owner in a lock file. A lock
left behind by a gateway that has already died is the single most common way chat
silently stops working on a free host, so the supervisor has to tell the three
cases apart: nobody owns the lock, a *dead* process owns it (reclaim with
``--force``), or a *live* gateway owns it (adopt it, never fight it).
"""

from __future__ import annotations

import json
import os
import socket

import pytest

from app.hermes import gateway as gateway_module
from app.hermes import rendezvous


@pytest.fixture()
def locks(tmp_path, monkeypatch):
    """An isolated gateway lock directory — never the operator's real one."""

    path = tmp_path / "gateway-locks"
    path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(path))
    return path


def _write_record(directory, pid: int, role: str = rendezvous.ROLE_GATEWAY) -> None:
    (directory / f"host-{role}.json").write_text(
        json.dumps(
            {
                "role": role,
                "home": "/tmp/hermes",
                "pid": pid,
                "createTime": 0,
                "startTime": 0,
                "host": "",
                "port": None,
                "protocolVersion": 1,
                "tokenFingerprint": "",
                "profiles": ["default"],
                "updatedAt": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )


class _FakeProcess:
    def __init__(self, pid: int = 31337) -> None:
        self.pid = pid
        self.returncode = None

    def poll(self):
        return None

    def terminate(self) -> None:  # pragma: no cover - not exercised here
        self.returncode = 0

    def wait(self, timeout=None):  # pragma: no cover
        return 0


def test_no_lock_means_a_clean_start(container, locks, monkeypatch):
    supervisor = container.gateway
    assert supervisor.external_gateway()["present"] is False

    started: list[list[str]] = []

    def fake_popen(argv, **kwargs):
        started.append(list(argv))
        return _FakeProcess()

    monkeypatch.setattr(gateway_module.subprocess, "Popen", fake_popen)
    status = supervisor.start(wait=False, interpreter="/usr/bin/python3")
    assert started, "the gateway child must be launched"
    assert "--force" not in started[0]
    assert status["state"] == "running"
    assert status["owns_process"] is True


def test_a_dead_gateway_lock_is_reclaimed_with_force(container, locks, monkeypatch):
    """Exit 75 in the log, chat down, user confused: this is the fix."""

    _write_record(locks, pid=999_999)  # nobody owns this pid

    supervisor = container.gateway
    lock = supervisor.external_gateway()
    assert lock["present"] is True
    assert lock["stale"] is True, "a lock owned by a dead process is stale"
    assert lock["alive"] is False

    started: list[list[str]] = []
    monkeypatch.setattr(gateway_module.subprocess, "Popen", lambda argv, **kw: (started.append(list(argv)), _FakeProcess())[1])
    supervisor.start(wait=False, interpreter="/usr/bin/python3")
    assert "--force" in started[0], "a stale host lock must be reclaimed with --force"


def test_a_live_foreign_gateway_is_adopted_not_fought(container, locks, monkeypatch):
    """Two gateways on one host fight over platforms; adoption is the only safe answer."""

    _write_record(locks, pid=os.getpid())  # alive: this test process

    supervisor = container.gateway
    monkeypatch.setattr(supervisor, "probe", lambda **kwargs: True)

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("the supervisor must not spawn a second gateway while a live one serves the host")

    monkeypatch.setattr(gateway_module.subprocess, "Popen", explode)
    status = supervisor.start(wait=False, interpreter="/usr/bin/python3")
    assert status["state"] == "running"
    assert status["pid"] == os.getpid()
    assert status["owns_process"] is False
    assert "will not be taken over" in status["note"]


def test_a_busy_port_is_reported_instead_of_a_dead_child(container, locks, monkeypatch):
    """If something else holds the port, say who — do not spawn a corpse."""

    supervisor = container.gateway
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", supervisor.port))
    holder.listen(1)
    try:
        def explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("no child should be spawned onto a busy port")

        monkeypatch.setattr(gateway_module.subprocess, "Popen", explode)
        status = supervisor.start(wait=False, interpreter="/usr/bin/python3")
    finally:
        holder.close()
    assert status["state"] == "error"
    assert f"Port {supervisor.port} is already in use" in status["last_error"]
    assert "different configuration" in status["last_error"]


def test_exit_75_says_what_it_means(container, locks, monkeypatch):
    """The raw exit code is not a user-facing message."""

    class _Exited(_FakeProcess):
        def __init__(self) -> None:
            super().__init__()
            self.returncode = 75

        def poll(self):
            return 75

    supervisor = container.gateway
    monkeypatch.setattr(gateway_module.subprocess, "Popen", lambda argv, **kw: _Exited())
    monkeypatch.setattr(supervisor, "probe", lambda **kwargs: False)
    status = supervisor.start(wait=False, interpreter="/usr/bin/python3")
    if status["state"] == "error":
        assert "another gateway already serves this host" in status["last_error"]


def test_the_supervisor_never_reports_itself_as_ready_without_a_listener(container, locks, monkeypatch):
    supervisor = container.gateway
    monkeypatch.setattr(gateway_module.subprocess, "Popen", lambda argv, **kw: _FakeProcess())
    monkeypatch.setattr(supervisor, "probe", lambda **kwargs: False)
    status = supervisor.start(wait=False, interpreter="/usr/bin/python3")
    assert status["reachable"] is False
