"""Authentication, sessions, CSRF and password storage.

These are the checks that decide whether the admin area is actually protected, so
they assert behaviour rather than implementation: a wrong password must not create a
session, a write without a CSRF token must fail even for a valid session, and the
password must never appear in the database in a form we could read back.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import ADMIN_PASSWORD, ADMIN_USER


def test_first_run_offers_setup_then_sign_in(app):
    client, _ = app
    status = client.get("/api/cc/auth/status").json()
    assert status["needs_setup"] is True and status["authenticated"] is False

    created = client.post("/api/cc/auth/setup", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
    assert created.status_code == 200
    assert created.json()["user"]["role"] == "admin"

    # A second setup attempt must not create another administrator.
    again = client.post("/api/cc/auth/setup", json={"username": "someone", "password": ADMIN_PASSWORD})
    assert again.status_code == 409


def test_weak_password_is_refused(app):
    client, _ = app
    response = client.post("/api/cc/auth/setup", json={"username": ADMIN_USER, "password": "password"})
    assert response.status_code == 400
    assert "password" in response.text.lower()


def test_login_rejects_wrong_password_and_records_it(app, admin):
    client, _ = app
    client.post("/api/cc/auth/logout")
    bad = client.post("/api/cc/auth/login", json={"username": ADMIN_USER, "password": "not-the-password"})
    assert bad.status_code == 401
    assert "incorrect" in bad.json()["message"].lower()

    good = client.post("/api/cc/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
    assert good.status_code == 200 and good.json()["csrf_token"]

    container = _container()
    rows = container.db.query("SELECT action FROM audit_events WHERE action = 'login_failed'")
    assert rows, "a failed sign-in must be audited"


def test_authenticated_reads_require_a_session(app):
    client, _ = app
    for path in ("/api/cc/settings", "/api/cc/keys", "/api/cc/logs", "/api/cc/models", "/api/cc/providers"):
        assert client.get(path).status_code == 401, path


def test_writes_require_a_csrf_token(app, admin):
    client, _ = app
    del client.headers["X-CSRF-Token"]
    response = client.put("/api/cc/settings", json={"values": {"theme": "light"}})
    assert response.status_code == 403
    assert "csrf" in response.text.lower()


def test_a_forged_csrf_token_is_rejected(app, admin):
    client, _ = app
    client.headers["X-CSRF-Token"] = "not-the-token"
    response = client.put("/api/cc/settings", json={"values": {"theme": "light"}})
    assert response.status_code == 403


def test_password_is_hashed_not_stored(app, admin):
    container = _container()
    row = container.db.query_one("SELECT password_hash FROM users WHERE username = ?", (ADMIN_USER,))
    stored = row["password_hash"]
    assert ADMIN_PASSWORD not in stored
    assert stored.startswith("scrypt$")

    from app.security import verify_password

    assert verify_password(ADMIN_PASSWORD, stored) is True
    assert verify_password(ADMIN_PASSWORD + "x", stored) is False


def test_password_change_revokes_other_sessions(app, admin):
    client, application = app
    # A second browser with its own session, like a phone left signed in.
    other = TestClient(application)
    second = other.post("/api/cc/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
    assert second.status_code == 200
    assert other.get("/api/cc/auth/me").status_code == 200

    changed = client.post(
        "/api/cc/auth/password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "a-brand-new-long-passphrase"},
    )
    assert changed.status_code == 200
    assert changed.json()["ok"] is True

    assert other.get("/api/cc/auth/me").status_code == 401, "the other session must be signed out"
    assert client.get("/api/cc/auth/me").status_code == 200, "the session that made the change keeps working"

    # And the new password is the one that works now.
    fresh = TestClient(application)
    assert fresh.post("/api/cc/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD}).status_code == 401
    assert (
        fresh.post(
            "/api/cc/auth/login", json={"username": ADMIN_USER, "password": "a-brand-new-long-passphrase"}
        ).status_code
        == 200
    )


def test_logout_clears_the_session(app, admin):
    client, _ = app
    assert client.get("/api/cc/auth/me").status_code == 200
    client.post("/api/cc/auth/logout")
    assert client.get("/api/cc/auth/me").status_code == 401


def test_api_tokens_authenticate_without_a_cookie(app, admin):
    client, _ = app
    created = client.post("/api/cc/auth/tokens", json={"label": "ci"})
    assert created.status_code == 200
    token = created.json()["token"]
    assert token

    plain = TestClient(_application())
    plain.headers.update({"Authorization": f"Bearer {token}"})
    me = plain.get("/api/cc/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["role"] == "admin"

    # A token with a mutated last character must not work.
    forged = TestClient(_application())
    forged.headers.update({"Authorization": f"Bearer {token[:-1]}X"})
    assert forged.get("/api/cc/auth/me").status_code == 401


def test_login_is_rate_limited(app):
    client, _ = app
    client.post("/api/cc/auth/setup", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
    client.post("/api/cc/auth/logout")
    statuses = [
        client.post("/api/cc/auth/login", json={"username": ADMIN_USER, "password": "wrong"}).status_code
        for _ in range(25)
    ]
    assert 429 in statuses, "repeated failures must eventually be throttled"


def _container():
    from app.container import get_container

    return get_container()


def _application():
    from app.main import create_app

    return create_app()
