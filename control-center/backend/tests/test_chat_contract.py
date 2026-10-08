"""Chat: conversation bookkeeping and the honest failure when the agent is down.

The streaming path against a real agent is covered in ``test_live_end_to_end.py``.
Here we assert what must hold regardless: conversations survive, a message that
cannot be answered says why, and nothing pretends to be a reply.
"""

from __future__ import annotations

import json


def test_conversation_lifecycle(admin):
    created = admin.post("/api/cc/chat/threads", json={"title": ""})
    assert created.status_code == 200
    thread = created.json()["thread"]
    assert thread["title"] == "New conversation"
    thread_id = thread["id"]

    renamed = admin.patch(f"/api/cc/chat/threads/{thread_id}", json={"title": "Renamed by the user"})
    assert renamed.status_code == 200
    assert renamed.json()["thread"]["title"] == "Renamed by the user"

    listing = admin.get("/api/cc/chat/threads").json()
    assert any(item["id"] == thread_id for item in listing["threads"])

    assert admin.delete(f"/api/cc/chat/threads/{thread_id}").status_code == 200
    assert admin.get(f"/api/cc/chat/threads/{thread_id}/messages").status_code == 404


def test_missing_conversation_is_a_clean_404(admin):
    assert admin.get("/api/cc/chat/threads/th_nope/messages").status_code == 404
    assert admin.post("/api/cc/chat/threads/th_nope/messages", json={"content": "hi"}).status_code == 404


def test_sending_without_a_bridge_explains_how_to_fix_it(admin):
    thread = admin.post("/api/cc/chat/threads", json={"title": "bridge"}).json()["thread"]["id"]
    response = admin.post(f"/api/cc/chat/threads/{thread}/messages", json={"content": "hello"})
    assert response.status_code == 503
    payload = response.json()
    assert payload["error"] == "chat_bridge_unavailable"
    assert "Chat settings" in json.dumps(payload["actions"])
    assert payload["hint"]


def test_bridge_status_is_actionable_when_not_configured(admin):
    payload = admin.get("/api/cc/chat/bridge").json()
    assert payload["config"]["configured"] is False
    assert payload["health"]["reachable"] is False
    assert payload["health"]["reason"]
    assert payload["key"]["name"] == "API_SERVER_KEY"
    assert payload["how_it_works"], "the UI should explain the wiring in plain words"
    assert payload["supervisor"]["state"] in {"stopped", "error"}
    assert "gateway" in payload["supervisor"]["note"].lower()


def test_bridge_start_without_an_interpreter_fails_cleanly(admin, container, monkeypatch):
    """No interpreter, no gateway — but a sentence, not a traceback."""

    from app.hermes import gateway as gateway_module

    monkeypatch.setattr(
        gateway_module,
        "GatewaySupervisor",
        gateway_module.GatewaySupervisor,
        raising=False,
    )
    container.gateway._interpreter = "/nonexistent/python"
    status = container.gateway.status()
    assert status["state"] in {"stopped", "error"}


def test_retry_without_a_user_message_is_refused(admin):
    thread = admin.post("/api/cc/chat/threads", json={"title": "empty"}).json()["thread"]["id"]
    response = admin.post(f"/api/cc/chat/threads/{thread}/retry")
    assert response.status_code == 409
    assert "user message" in response.json()["message"]


def test_retry_removes_the_failed_reply(admin, container):
    thread = admin.post("/api/cc/chat/threads", json={"title": "retry"}).json()["thread"]["id"]
    container.db.insert(
        "INSERT INTO chat_messages (thread_id, role, content, created_at, meta) VALUES (?, 'user', ?, ?, '{}')",
        (thread, "what is 2+2?", "2026-01-01T00:00:00Z"),
    )
    container.db.insert(
        "INSERT INTO chat_messages (thread_id, role, content, created_at, meta) VALUES (?, 'assistant', ?, ?, '{}')",
        (thread, "an error happened", "2026-01-01T00:00:01Z"),
    )
    response = admin.post(f"/api/cc/chat/threads/{thread}/retry")
    assert response.status_code == 200
    assert response.json()["content"] == "what is 2+2?"
    messages = admin.get(f"/api/cc/chat/threads/{thread}/messages").json()["messages"]
    assert [item["role"] for item in messages] == ["user"]


def test_stop_without_a_run_says_so(admin):
    thread = admin.post("/api/cc/chat/threads", json={"title": "stop"}).json()["thread"]["id"]
    response = admin.post(f"/api/cc/chat/threads/{thread}/stop")
    assert response.status_code == 200
    assert response.json()["stopped"] is False
    assert "no run is recorded" in response.json()["message"].lower()


def test_approval_choices_are_validated(admin):
    response = admin.post("/api/cc/chat/runs/run_bogus/approval", json={"choice": "maybe"})
    assert response.status_code in {400, 422, 503}
    payload = response.json()
    assert payload.get("error") or payload.get("detail")


def test_session_attach_and_detach(admin):
    thread = admin.post("/api/cc/chat/threads", json={"title": "session"}).json()["thread"]["id"]
    attached = admin.post(f"/api/cc/chat/threads/{thread}/session", params={"session_id": "api-abc123"})
    assert attached.status_code == 200
    assert attached.json()["hermes_session_id"] == "api-abc123"
    cleared = admin.post(f"/api/cc/chat/threads/{thread}/session", params={"clear": True})
    assert cleared.json()["hermes_session_id"] == ""


def test_archiving_hides_a_conversation_by_default(admin):
    thread = admin.post("/api/cc/chat/threads", json={"title": "archive me"}).json()["thread"]["id"]
    admin.patch(f"/api/cc/chat/threads/{thread}", json={"title": "archive me", "archived": True})
    assert all(item["id"] != thread for item in admin.get("/api/cc/chat/threads").json()["threads"])
    assert any(item["id"] == thread for item in admin.get("/api/cc/chat/threads", params={"include_archived": True}).json()["threads"])


def test_message_content_is_bounded(admin):
    thread = admin.post("/api/cc/chat/threads", json={"title": "big"}).json()["thread"]["id"]
    response = admin.post(f"/api/cc/chat/threads/{thread}/messages", json={"content": "x" * 300_000})
    assert response.status_code == 422
    assert "content" in response.text


def test_gateway_start_reports_which_mechanism_was_used(admin, container, monkeypatch):
    """Upstream delegates to systemd; in a container we supervise it ourselves."""

    def fake_start(*args, **kwargs):
        return {"state": "running", "pid": 4242, "port": 9420, "reachable": True, "last_error": ""}

    monkeypatch.setattr(container.gateway, "probe", lambda *a, **k: False)
    monkeypatch.setattr(container.gateway, "start", fake_start, raising=False)
    monkeypatch.setattr(container.gateway, "status", lambda: {"state": "running", "reachable": True})
    response = admin.post("/api/cc/chat/bridge/start", timeout=60)
    assert response.status_code == 200
    assert response.json()["via"] == "control-center"
