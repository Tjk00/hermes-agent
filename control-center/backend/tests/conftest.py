"""Shared fixtures for the Control Center test suite.

Every test runs against a **real** application object with a throwaway state
directory, a real SQLite database and real cryptography — nothing is mocked except
the Hermes runtime itself, which is switched off (``CC_RUNTIME_MODE=disabled``) so
the suite is fast and never spawns the agent. The live, end-to-end checks live in
``test_live_end_to_end.py`` and only run when ``CC_LIVE=1``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

ADMIN_USER = "tester"
# A throwaway fixture value for an isolated CC_HOME — not a credential, and never
# shipped, stored or used outside the test process.
ADMIN_PASSWORD = "test-only-not-a-real-password"


def _purge_app_modules() -> None:
    for name in [module for module in list(sys.modules) if module == "app" or module.startswith("app.")]:
        del sys.modules[name]


@pytest.fixture()
def cc_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "cc-home"
    monkeypatch.setenv("CC_HOME", str(home))
    monkeypatch.setenv("CC_DATABASE_URL", "")
    monkeypatch.setenv("CC_RUNTIME_MODE", "disabled")
    monkeypatch.setenv("CC_RUNTIME_AUTOSTART", "0")
    monkeypatch.setenv("CC_HERMES_PORT", "9419")  # never the real runtime's port
    monkeypatch.setenv("CC_CHAT_BRIDGE_PORT", "9420")
    monkeypatch.setenv("CC_LIVE", os.environ.get("CC_LIVE", ""))
    _purge_app_modules()
    yield home
    _purge_app_modules()


@pytest.fixture()
def app(cc_home):
    from app.config import get_settings
    from app.main import create_app

    get_settings()
    application = create_app()
    with TestClient(application) as client:
        yield client, application


@pytest.fixture()
def admin(app):
    """A signed-in administrator, with the CSRF token attached to the client."""

    client, _ = app
    response = client.post("/api/cc/auth/setup", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
    assert response.status_code == 200, response.text
    token = response.json()["csrf_token"]
    client.headers.update({"X-CSRF-Token": token})
    return client


@pytest.fixture()
def container(cc_home):
    """The application's container, for tests that exercise services directly."""

    from app.config import get_settings
    from app.container import build_container

    get_settings()
    instance = build_container()
    instance.initialise()
    return instance
