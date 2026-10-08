"""Database, settings, logs and the human-error contract.

Everything here is exercised through the real SQLite file in a throwaway CC_HOME —
including the schema-drift guard that exists because an older database once made
sign-in fail with a raw ``OperationalError`` instead of a sentence a person can act on.
"""

from __future__ import annotations

import json

from tests.conftest import ADMIN_PASSWORD, ADMIN_USER


def test_defaults_are_written_on_first_boot(admin):
    settings = admin.get("/api/cc/settings").json()
    assert settings["values"]["free_mode"] is True
    assert settings["values"]["allow_paid_fallback"] is False
    assert settings["values"]["sync_keys_to_hermes_env"] is False
    assert settings["read_only"]["home"].endswith("cc-home")


def test_editable_settings_round_trip(admin):
    saved = admin.put(
        "/api/cc/settings",
        json={"values": {"log_retention_days": 7, "runtime_auto_restart": False, "theme": "light"}},
    )
    assert saved.status_code == 200
    values = admin.get("/api/cc/settings").json()["values"]
    assert values["log_retention_days"] == 7
    assert values["runtime_auto_restart"] is False
    assert values["theme"] == "light"


def test_read_only_settings_are_rejected_not_silently_ignored(admin):
    response = admin.put("/api/cc/settings", json={"values": {"runtime_port": 1234}})
    assert response.status_code == 400
    assert "runtime_port" in response.text


def test_a_non_numeric_value_for_an_integer_setting_is_refused(admin):
    response = admin.put("/api/cc/settings", json={"values": {"log_retention_days": "soon"}})
    assert response.status_code == 400


def test_logs_are_written_readable_filterable_and_clearable(admin):
    admin.get("/api/cc/logs/categories")
    admin.get("/api/cc/status/detail")
    logs = admin.get("/api/cc/logs", params={"limit": 50}).json()
    entries = logs["sources"]["control-center"]["entries"]
    assert entries, "the app must log what it does"
    assert {"ts", "level", "category", "message"} <= set(entries[0])

    summary = admin.get("/api/cc/logs/summary").json()
    assert summary["total"] >= len(entries)
    assert "retention_days" in summary

    filtered = admin.get("/api/cc/logs", params={"category": "SECURITY"}).json()
    assert all(item["category"] == "SECURITY" for item in filtered["sources"]["control-center"]["entries"])

    cleared = admin.delete("/api/cc/logs", params={"source": "control-center"})
    assert cleared.status_code == 200
    assert admin.get("/api/cc/logs/summary").json()["total"] == 0


def test_logs_export_in_three_formats(admin):
    admin.get("/api/cc/status/detail")
    for fmt in ("json", "csv", "txt"):
        response = admin.get("/api/cc/logs/export", params={"format": fmt})
        assert response.status_code == 200
        assert response.text
        assert f".{fmt}" in response.headers.get("content-disposition", "")


def test_backup_and_restore_round_trip(admin):
    admin.put("/api/cc/settings", json={"values": {"theme": "light", "log_retention_days": 14}})
    thread = admin.post("/api/cc/chat/threads", json={"title": "Kept conversation"})
    assert thread.status_code == 200

    backup = admin.get("/api/cc/settings/backup")
    payload = backup.json()
    assert payload["kind"] == "control-center-backup"
    assert payload["settings"]["theme"] == "light"
    assert payload["chat"]["threads"], "conversations belong in a backup"

    admin.put("/api/cc/settings", json={"values": {"theme": "dark"}})
    admin.delete(f"/api/cc/chat/threads/{thread.json()['thread']['id']}")

    restored = admin.post("/api/cc/settings/restore", files={"file": ("backup.json", backup.content, "application/json")})
    assert restored.status_code == 200
    values = admin.get("/api/cc/settings").json()["values"]
    assert values["theme"] == "light"
    titles = [item["title"] for item in admin.get("/api/cc/chat/threads").json()["threads"]]
    assert "Kept conversation" in titles


def test_restore_refuses_a_foreign_file(admin):
    response = admin.post("/api/cc/settings/restore", files={"file": ("x.json", b'{"hello":"world"}', "application/json")})
    assert response.status_code == 400
    assert "backup" in response.text.lower()


def test_schema_drift_is_reported_in_words(cc_home):
    """An old database must not produce a raw OperationalError mid-request."""

    import sqlite3

    from app.db import Database, DatabaseUnavailable

    home = cc_home
    home.mkdir(parents=True, exist_ok=True)
    path = home / "control-center.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE auth_sessions (id INTEGER PRIMARY KEY, wrong_column TEXT)")
    connection.commit()
    connection.close()

    try:
        Database("", home=home)
    except DatabaseUnavailable as exc:
        message = str(exc)
        assert "schema" in message.lower()
        assert "reset-db" in message, "the message must say how to fix it"
        assert "your data is intact" in message.lower()
    else:  # pragma: no cover - would mean the guard is gone
        raise AssertionError("a drifted database must be refused, not used")


def test_database_health_is_honest(admin):
    health = admin.get("/api/cc/status/detail").json()
    assert health["service"]["version"]
    assert health["runtime"]["state"] == "disabled"  # the test config turns the runtime off
    assert health["free_mode"]["enabled"] is True


def test_human_error_payloads_are_complete(admin):
    """Every failure the UI renders must carry a title, a hint and a retry flag."""

    response = admin.get("/api/cc/definitely-not-a-route")
    payload = response.json()
    for field in ("error", "title", "message", "hint", "actions", "retryable"):
        assert field in payload, field
    assert payload["error"] == "not_found"


def test_error_classifier_covers_the_required_cases():
    from app.errors import classify

    cases = {
        "missing": classify(message="No API key found for provider openai"),
        "invalid": classify(status=401, message="Invalid API key provided"),
        "rate": classify(status=429, message="Rate limit exceeded"),
        "model": classify(message="The model gpt-9 does not exist"),
        "network": classify(message="Connection reset by peer"),
        "memory": classify(message="Cannot allocate memory while loading model"),
        "expired": classify(message="Your credentials have expired, please re-authenticate"),
        "context": classify(message="This model's maximum context length is 128000 tokens"),
        "no-provider": classify(message="Hermes is not connected to any AI provider yet. Run `hermes model`"),
    }
    assert cases["no-provider"]["error"] == "no_provider_selected"
    assert cases["no-provider"]["actions"], "the user needs a next step, not just an apology"
    for name, payload in cases.items():
        assert payload["title"] and payload["message"], name
        assert isinstance(payload["retryable"], bool)


def test_audit_trail_records_sensitive_actions(admin, container):
    admin.put("/api/cc/settings", json={"values": {"theme": "dark"}})
    actions = {row["action"] for row in container.db.query("SELECT action FROM audit_events")}
    assert "settings_updated" in actions


def test_database_stays_sqlite_by_default(admin):
    health = admin.get("/api/cc/status/detail").json()
    assert health["service"]["python"]
    settings = admin.get("/api/cc/settings").json()
    assert "sqlite" in settings["read_only"]["database"]


def test_root_and_spa_fallbacks_answer(admin):
    root = admin.get("/")
    assert root.status_code == 200
    assert "Hermes" in root.text
    # An unknown deep link must still return the app shell, not a 404 page.
    deep = admin.get("/chat")
    assert deep.status_code == 200


def test_public_status_never_leaks_paths_or_keys(app):
    client, _ = app
    payload = client.get("/status").json()
    text = json.dumps(payload)
    assert "token" not in text.lower()
    assert "/home/" not in text
    assert payload["status"] in {"ok", "degraded"}


def test_admin_password_never_appears_in_any_response(admin):
    for path in ("/api/cc/settings", "/api/cc/auth/me", "/api/cc/logs", "/api/cc/status/detail"):
        assert ADMIN_PASSWORD not in admin.get(path).text
    assert ADMIN_USER  # sanity: the fixture really created this account
