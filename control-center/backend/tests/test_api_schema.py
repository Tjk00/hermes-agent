"""Guards against a class of bug that is invisible until a user hits it.

FastAPI turns *any* unannotated-as-dependency parameter into part of the request
body — including a dataclass such as ``Settings`` appearing on a dependency
callable. That mistake once made every POST answer 422 ("field 'settings' is
required") while the OpenAPI document still looked plausible. These tests walk the
generated schema and assert the request bodies are only what the endpoints intend.
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

#: Body-model properties that are legitimate wrappers, never stray parameters.
ALLOWED_SINGLE_FIELDS = {"body", "file"}


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CC_RUNTIME_MODE", "disabled")  # never spawn the agent in tests
    monkeypatch.setenv("CC_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("CC_RUNTIME_AUTOSTART", "0")
    for module in [name for name in list(sys.modules) if name == "app" or name.startswith("app.")]:
        del sys.modules[module]
    from app.config import get_settings

    get_settings()
    from app.main import create_app

    application = create_app()
    with TestClient(application) as client:
        yield client, application


def test_no_stray_body_parameters(app):
    """Every request body must be exactly the model the endpoint declares."""

    client, application = app
    spec = application.openapi()
    offenders: list[str] = []
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            body = operation.get("requestBody")
            if not body:
                continue
            for media, content in body.get("content", {}).items():
                schema = content.get("schema") or {}
                ref = schema.get("$ref", "")
                if not ref.startswith("#/components/schemas/Body_"):
                    continue
                name = ref.rsplit("/", 1)[-1]
                model = spec["components"]["schemas"][name]
                extra = set(model.get("properties", {})) - ALLOWED_SINGLE_FIELDS
                if extra:
                    offenders.append(f"{method.upper()} {path}: unexpected body field(s) {sorted(extra)}")
    assert not offenders, "Endpoints grew stray body parameters:\n" + "\n".join(offenders)


def test_public_endpoints_answer_without_auth(app):
    client, _ = app
    assert client.get("/status").status_code == 200
    assert client.get("/api/status").status_code == 200
    health = client.get("/health")
    assert health.status_code == 200
    assert {"status", "checks", "uptime_seconds"} <= set(health.json())
    auth = client.get("/api/cc/auth/status")
    assert auth.status_code == 200
    assert auth.json()["needs_setup"] is True  # fresh CC_HOME, no users yet


def test_protected_endpoints_require_auth(app):
    client, _ = app
    for path in ("/api/cc/runtime", "/api/cc/models", "/api/cc/providers", "/api/cc/keys", "/api/cc/logs", "/api/cc/status/detail"):
        response = client.get(path)
        assert response.status_code == 401, f"{path} answered {response.status_code} without a session"


def test_login_rejects_a_missing_body_with_a_human_message(app):
    client, _ = app
    response = client.post("/api/cc/auth/login", json={})
    assert response.status_code == 422
    payload = response.json()
    assert payload["error"] == "invalid_request"
    assert "username" in payload["message"] or "password" in payload["message"]


def test_public_health_reports_a_real_database_check(app):
    """/health is what a container probe calls, so it must not lie about the database.

    It once read ``database['ok']``/``['kind']`` while ``Database.health()`` returns
    ``available``/``dialect``/``latency_ms``/``schema_version`` — so a perfectly
    healthy SQLite file was reported as a critical failure.
    """

    client, _ = app
    payload = client.get("/health").json()
    checks = {check["name"]: check for check in payload["checks"]}

    database = checks["database"]
    assert database["ok"] is True, database["detail"]
    assert "schema" in database["detail"], database["detail"]
    assert "None" not in database["detail"].split("schema")[0], f"dialect missing: {database['detail']}"

    assert payload["status"] in {"ok", "degraded", "unhealthy"}


def test_public_health_names_the_runtime_mode_and_state(app):
    client, _ = app
    checks = {check["name"]: check for check in client.get("/health").json()["checks"]}
    assert "mode=" in checks["hermes_runtime"]["detail"], checks["hermes_runtime"]["detail"]
    assert "state=" in checks["hermes_runtime"]["detail"], checks["hermes_runtime"]["detail"]
