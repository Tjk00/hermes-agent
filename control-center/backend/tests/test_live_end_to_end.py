"""Live end-to-end checks against a **real** Hermes Agent runtime.

These are the tests that prove the product claim: underneath the Control Center
there is an actual Hermes install, it starts, the API answers with its own session
token, and a chat turn travels Control Center → gateway → agent → back.

They are skipped unless ``CC_LIVE=1`` because they spawn a real Python process and
talk to it over HTTP. Run them with the runtime's own interpreter available:

    CC_LIVE=1 CC_HERMES_SOURCE=/path/to/hermes-agent \\
    CC_HERMES_PYTHON=/path/to/venv/bin/python \\
    CC_HERMES_PORT=9319 CC_CHAT_BRIDGE_PORT=9320 \\
    python -m pytest tests/test_live_end_to_end.py -v

The port numbers above are deliberately *not* the defaults so a live test can never
collide with a runtime you are actually using.
"""

from __future__ import annotations

import json
import os
import time

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    os.environ.get("CC_LIVE", "") not in {"1", "true", "yes"},
    reason="live Hermes runtime tests: set CC_LIVE=1 to run them",
)


@pytest.fixture(scope="module")
def live_app():
    """A real application with a real runtime, using throwaway ports and state."""

    import tempfile
    from pathlib import Path

    home = Path(tempfile.mkdtemp(prefix="cc-live-"))
    os.environ["CC_HOME"] = str(home)
    # A live test must never touch the operator's real Hermes home or the host
    # gateway lock, so it gets a throwaway home and its own lock namespace. Without
    # this the test would rewrite ~/.hermes/config and race the gateway you are
    # actually using.
    os.environ.setdefault("CC_HERMES_HOME", str(home / "hermes-home"))
    os.environ.setdefault("HERMES_GATEWAY_LOCK_DIR", str(home / "gateway-locks"))
    os.environ.setdefault("CC_RUNTIME_MODE", "dashboard")
    os.environ.setdefault("CC_RUNTIME_AUTOSTART", "0")
    os.environ.setdefault("CC_HERMES_PORT", "9319")
    os.environ.setdefault("CC_CHAT_BRIDGE_PORT", "9320")
    os.environ.setdefault("CC_ADOPT_EXISTING", "0")

    for name in [module for module in list(__import__("sys").modules) if module == "app" or module.startswith("app.")]:
        del __import__("sys").modules[name]

    from app.config import get_settings
    from app.main import create_app

    get_settings()
    application = create_app()
    with TestClient(application) as client:
        created = client.post("/api/cc/auth/setup", json={"username": "live", "password": "live-test-passphrase"})
        assert created.status_code == 200, created.text
        client.headers["X-CSRF-Token"] = created.json()["csrf_token"]
        yield client


@pytest.fixture(scope="module")
def live_runtime(live_app):
    response = live_app.post("/api/cc/runtime/start")
    assert response.status_code == 200, response.text
    status = response.json()["runtime"]
    assert status["state"] in {"running", "attached", "external"}, status
    yield status
    live_app.post("/api/cc/runtime/stop")


def test_the_real_agent_reports_itself_through_the_api(live_app, live_runtime):
    detail = live_app.get("/api/cc/status/detail").json()
    assert detail["hermes"]["reachable"] is True
    assert detail["hermes"].get("hermes_release_date"), "the agent must identify its build"
    assert detail["runtime"]["healthy"] is True
    assert detail["runtime"]["python_version"].startswith("3.")


def test_the_runtime_was_started_by_the_control_center(live_app, live_runtime):
    assert live_runtime["command"], "the exact command must be visible for transparency"
    assert "hermes_cli.main" in " ".join(live_runtime["command"])
    assert live_runtime["pid"] and live_runtime["pid"] > 1


def test_passthrough_reads_return_real_agent_state(live_app, live_runtime):
    skills = live_app.get("/api/cc/skills")
    assert skills.status_code == 200, skills.text
    assert isinstance(skills.json(), list)

    toolsets = live_app.get("/api/cc/tools/toolsets")
    assert toolsets.status_code == 200
    assert isinstance(toolsets.json(), list)

    schedules = live_app.get("/api/cc/schedules")
    assert schedules.status_code == 200

    sessions = live_app.get("/api/cc/sessions")
    assert sessions.status_code == 200
    assert "sessions" in sessions.json()


def test_the_agent_bridge_can_be_enabled_and_answers(live_app, live_runtime):
    enabled = live_app.post("/api/cc/chat/bridge/enable")
    assert enabled.status_code == 200, enabled.text
    payload = enabled.json()
    assert payload["steps"], "the UI shows each step"
    assert payload["health"]["reachable"] is True, payload
    assert "hermes-agent" in payload["health"]["models"]

    bridge = live_app.get("/api/cc/chat/bridge").json()
    assert bridge["supervisor"]["pid"], "the gateway must be a supervised process we can see"


def test_a_real_chat_turn_reaches_the_agent(live_app, live_runtime):
    """Either the agent answers with text, or it explains what it is missing.

    Both outcomes are correct; what must never happen is silence, an internal error,
    or a fabricated reply.
    """

    enabled = live_app.post("/api/cc/chat/bridge/enable").json()
    if not enabled["health"]["reachable"]:
        pytest.skip(f"bridge not reachable in this environment: {enabled['health']['reason']}")

    thread = live_app.post("/api/cc/chat/threads", json={"title": "live turn"}).json()["thread"]["id"]
    frames: list[tuple[str, dict]] = []
    with live_app.stream(
        "POST", f"/api/cc/chat/threads/{thread}/messages", json={"content": "Reply with the single word: ready"}
    ) as response:
        assert response.status_code == 200, response.read().decode()[:400]
        event = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: ") and event:
                frames.append((event, json.loads(line[6:])))
                if event == "done":
                    break

    names = [name for name, _ in frames]
    assert "status" in names
    assert ("delta" in names) or ("error" in names), frames

    messages = live_app.get(f"/api/cc/chat/threads/{thread}/messages").json()["messages"]
    roles = [item["role"] for item in messages]
    assert roles[0] == "user"
    assert roles[-1] == "assistant", "every turn is recorded, including a failed one"

    if "error" in names:
        error = next(payload for name, payload in frames if name == "error")
        assert error["title"] and error["message"]
        # A missing key or an unconfigured provider is expected on a fresh install —
        # but it must be one of our named, human-readable failures.
        assert error["error"] in {
            "no_provider_selected",
            "provider_key_missing",
            "provider_key_invalid",
            "rate_limited",
            "paid_blocked",
        }


def test_free_mode_is_the_default_and_needs_no_paid_key(live_app, live_runtime):
    """The acceptance criterion: a working agent path with zero paid credentials."""

    policy = live_app.get("/api/cc/free-mode").json()
    assert policy["free_mode"] is True, "FREE MODE must be on out of the box"
    assert policy["allow_paid_fallback"] is False
    assert policy["nous_free_tier_enabled"] is False  # off until the operator asks for it
    assert policy["routes"], "the free routes must be enumerated with an honest state"

    spend = live_app.get("/api/cc/providers/spend-guard").json()
    assert spend["paid_unlocked"] is False
    assert spend["paid_configured_not_necessarily_active"] == []
    assert "Nothing here can charge you" in spend["statement"]

    # And the guard is enforced, not merely displayed: a paid model is refused.
    blocked = live_app.post("/api/cc/models/select", json={"provider": "anthropic", "model": "claude-3-5-sonnet"})
    assert blocked.status_code == 402, blocked.text
    assert "Nothing was sent" in json.dumps(blocked.json())


def test_stop_leaves_a_runtime_it_owns_stopped(live_app, live_runtime):
    stopped = live_app.post("/api/cc/runtime/stop")
    assert stopped.status_code == 200
    assert stopped.json()["runtime"]["state"] == "stopped"
    restarted = live_app.post("/api/cc/runtime/start")
    assert restarted.status_code == 200
    assert restarted.json()["runtime"]["state"] in {"running", "attached", "external"}


def test_the_control_center_never_serves_a_key_to_the_browser(live_app, live_runtime):
    body = live_app.get("/api/cc/chat/bridge").text
    assert "Bearer" not in body
    assert "API_SERVER_KEY" in body  # the *name* is fine; the value is not
    keys = live_app.get("/api/cc/keys").text
    assert "sk-" not in keys
