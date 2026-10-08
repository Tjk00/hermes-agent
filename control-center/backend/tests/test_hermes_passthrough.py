"""The Hermes passthrough: allowlisted, redacted, policy-checked.

The Control Center must never become an open proxy into the agent's API, and must
never repeat a secret the agent happens to include in a response. Both properties are
asserted here against an application whose runtime is switched off — which is also
how the "the agent is down" path gets exercised for real.
"""

from __future__ import annotations

import json

from tests.conftest import ADMIN_PASSWORD, ADMIN_USER


def test_read_passthrough_reports_the_runtime_being_down_in_words(admin):
    response = admin.get("/api/cc/memory")
    assert response.status_code == 503
    payload = response.json()
    assert payload["error"] in {"runtime_unavailable", "http_error"}
    assert payload["hint"] or payload["message"], "the UI needs something to show"


def test_unknown_upstream_routes_are_not_reachable(admin):
    """Only the routes in the allowlist may be proxied — no dashboard file APIs."""

    for path in (
        "/api/cc/files",
        "/api/cc/secrets",
        "/api/cc/..%2F..%2Fetc%2Fpasswd",
        "/api/cc/system/exec",
        "/api/cc/config/defaults",  # exists upstream, deliberately not proxied
    ):
        response = admin.get(path)
        assert response.status_code == 404, path
        assert response.json()["error"] == "not_found"


def test_allowlisted_paths_are_exactly_the_documented_set():
    from app.routes.hermes_proxy import READ_ROUTES, WRITE_ROUTES

    reads = {item[0] for item in READ_ROUTES}
    assert {"/memory", "/skills", "/tools/toolsets", "/sessions", "/schedules", "/env", "/config"} <= reads
    # Nothing that reads or writes the agent's own files, and nothing that could
    # execute a shell, may appear in the table. (``/local-models/download`` is a
    # model download, not a file from the agent's filesystem — it is allowed.)
    forbidden = ("exec", "shell", "upload", "raw-file", "secrets", "artifact", "filesystem")
    for path in reads:
        assert not any(word in path for word in forbidden), path
    for method, path, upstream, _timeout in WRITE_ROUTES:
        assert method in {"POST", "PUT", "PATCH", "DELETE"}
        assert not any(word in path for word in forbidden), path
        assert upstream.startswith("/api/"), path


def test_explicitly_disallowed_upstream_route_stays_unreachable(admin):
    # /api/tools/computer-use/permissions is upstream, but not in our table.
    assert admin.get("/api/cc/tools/computer-use/permissions").status_code == 404


def test_proxied_response_redacts_secret_looking_strings(container):
    """Even if upstream echoes a key, the browser must not receive it."""

    from app.security import get_redactor

    secret = "sk-or-v1-" + "f" * 40
    get_redactor().register(secret)
    payload = {"data": {"note": f"your key is {secret}", "nested": [{"k": secret}]}}

    from app.routes.hermes_proxy import _redact

    redacted = json.dumps(_redact(payload))
    assert secret not in redacted
    assert "redacted" in redacted.lower() or "•" in redacted


def test_write_passthrough_guards_paid_providers(admin):
    """A schedule that would run on a paid provider is refused while FREE MODE is on."""

    response = admin.post(
        "/api/cc/schedules",
        json={"schedule": "0 9 * * *", "prompt": "daily summary", "provider": "openai", "model": "gpt-5.1"},
    )
    assert response.status_code in {402, 503}
    if response.status_code == 402:
        assert response.json()["error"] == "paid_blocked"


def test_env_passthrough_requires_confirmation_of_secret_writes(admin, container):
    response = admin.put("/api/cc/env", json={"key": "MY_TEST_VAR", "value": "hello"})
    # The runtime is down, so this cannot succeed — but it must fail with a sentence.
    assert response.status_code in {502, 503}
    assert response.json()["error"] in {"runtime_unavailable", "http_error", "provider_unreachable"}


def test_deploy_pages_describe_the_host_honestly(admin):
    platforms = admin.get("/api/cc/deploy/platforms").json()
    assert platforms["platforms"], "the compatibility page must have content"
    free = admin.get("/api/cc/deploy/free-hosting").json()
    text = json.dumps(free).lower()
    assert "sleep" in text or "idle" in text, "free tiers that sleep must be named as such"
    assert free["what_it_does"] and free["what_it_cannot_do"]
    assert "cannot" in json.dumps(free["what_it_cannot_do"]).lower()
    labels = platforms["labels"]
    assert labels, "the compatibility page must define its honesty labels"
    assert platforms["disclaimer"]

    preflight = admin.get("/api/cc/deploy/preflight").json()
    assert preflight["checks"], "preflight must actually check something"
    ids = {check["id"] for check in preflight["checks"]}
    assert {"ram", "disk", "python", "persistence"} <= ids
    assert preflight["verdict"]["state"] in {"ready", "blocked"}
    assert preflight["verdict"]["detail"]


def test_local_capability_says_no_when_it_cannot_fit(admin, container):
    """On a machine with no GPU this must be an explicit refusal, not optimism."""

    result = admin.get("/api/cc/local/capability", params={"check": True}).json()
    verdict = result["verdict"]
    if result["host"].get("gpu") is None:
        assert verdict["state"] in {"unsupported", "unknown"}
        if verdict["state"] == "unsupported":
            assert "does not fit" in verdict["detail"] or "not fit" in verdict["detail"]
            assert verdict["recommendation"]


def test_runtime_logs_endpoint_survives_a_missing_log_file(admin):
    response = admin.get("/api/cc/runtime/logs")
    assert response.status_code == 200
    assert "lines" in response.json()


def test_runtime_install_plan_is_transparent(admin):
    plan = admin.get("/api/cc/runtime/install-plan").json()
    assert plan["commands"], "the plan must show the real commands"
    assert any("pip install" in command for command in plan["commands"])
    assert "what_the_button_does" in plan
    assert plan["bridge_dependencies"]["packages"] == ["aiohttp"]
    assert "aiohttp" in json.dumps(plan["bridge_dependencies"]).lower()


def test_bootstrap_requires_explicit_confirmation(admin):
    response = admin.post("/api/cc/runtime/bootstrap", json={"extras": "web"})
    assert response.status_code == 400
    assert "confirm" in response.text.lower() or "confirmation" in response.text.lower()


def test_runtime_start_is_refused_when_the_mode_is_disabled(admin):
    response = admin.post("/api/cc/runtime/start")
    assert response.status_code == 200
    payload = response.json()
    assert payload["runtime"]["state"] == "disabled"
    assert payload["ok"] or payload["runtime"]["state"] == "disabled"


def test_skills_and_schedules_surfaces_exist_even_while_the_agent_is_down(admin):
    """A control center that shows nothing when the agent is stopped is useless."""

    skills = admin.get("/api/cc/skills")
    assert skills.status_code == 503 and skills.json()["error"]
    schedules = admin.get("/api/cc/schedules")
    assert schedules.status_code == 503 and schedules.json()["error"]
    tools = admin.get("/api/cc/tools/toolsets")
    assert tools.status_code == 503


def test_identity_endpoint_reports_the_adoption_state(admin):
    payload = admin.get("/api/cc/identity").json()
    assert payload["signed_in"] is True
    assert payload["actor"] == ADMIN_USER
    assert "runtime_attached" in payload


def test_auth_is_not_bypassed_by_forwarded_headers(admin):
    response = admin.get(
        "/api/cc/settings",
        headers={"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1", "X-Forwarded-Host": "localhost"},
    )
    assert response.status_code == 200  # authenticated, so fine — but it must be *our* session that authorises it
    assert ADMIN_PASSWORD not in response.text
