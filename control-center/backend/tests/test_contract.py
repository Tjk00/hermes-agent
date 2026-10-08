"""Frontend ↔ backend contract, and the endpoints the UI depends on.

The most expensive class of bug in a project like this is a page calling a path that
does not exist: it fails only in the browser, only when a user clicks. This module
reads the frontend sources and asserts that every ``/api/cc/...`` they mention is a
real route, then exercises the endpoints that were written to satisfy them.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

import pytest

FRONTEND_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"
PATH_PATTERN = re.compile(r'["`](/api/cc/[^"`$\s]*)["`]')


def _frontend_paths() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    if not FRONTEND_SRC.exists():  # pragma: no cover - source checkout only
        return found
    for source in list(FRONTEND_SRC.rglob("*.ts")) + list(FRONTEND_SRC.rglob("*.tsx")):
        for match in PATH_PATTERN.finditer(source.read_text(encoding="utf-8")):
            path = match.group(1).split("?")[0]
            if path:
                found.setdefault(path, set()).add(source.name)
    return found


def _normalise(path: str) -> str:
    path = re.sub(r"\$\{[^}]*\}", "{param}", path)  # template literals
    path = re.sub(r"\{[^}]*\}", "{param}", path)  # FastAPI parameters
    return path.rstrip("/") or "/"


def test_every_api_path_the_frontend_calls_exists(app):
    """No page may call an endpoint this backend does not serve."""

    _client, application = app
    known = {_normalise(path) for path in application.openapi()["paths"]}
    missing: list[str] = []
    for path, files in _frontend_paths().items():
        if _normalise(path) not in known:
            missing.append(f"{path} (used by {', '.join(sorted(files))})")
    assert not missing, "frontend calls endpoints that do not exist:\n  " + "\n  ".join(sorted(missing))


BROWSER_CALL = re.compile(
    r"(?:fetch|request|streamSse|api\.(?:get|post|put|patch|del))\s*\(\s*[`\"'](https?://[^`\"']+)",
)
LOCAL_HOSTS = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0)", re.I)


def test_the_frontend_never_calls_a_host_directly(app):
    """The browser talks to whatever origin served it — relative URLs only.

    Literal addresses are allowed as *values* (a suggested Ollama endpoint, a sample
    URL in a sentence); what must never happen is browser code contacting a host
    itself, because that breaks the moment the app is served from a phone, a tunnel
    or a container.
    """

    offenders: list[str] = []
    for source in list(FRONTEND_SRC.rglob("*.ts")) + list(FRONTEND_SRC.rglob("*.tsx")):
        text = source.read_text(encoding="utf-8")
        for match in BROWSER_CALL.finditer(text):
            if LOCAL_HOSTS.match(match.group(1)):
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{source.name}:{line} → {match.group(1)}")
    assert not offenders, "browser code must use relative URLs:\n  " + "\n  ".join(offenders)


def test_the_ui_embeds_no_secret_values(app):
    """No literal key may be baked into the UI bundle."""

    suspicious: list[str] = []
    for source in FRONTEND_SRC.rglob("*.tsx"):
        text = source.read_text(encoding="utf-8")
        if re.search(r"(api[_-]?key|secret|token)\s*[:=]\s*[\"'][A-Za-z0-9_\-]{16,}[\"']", text, re.I):
            suspicious.append(source.name)
    assert not suspicious, f"a literal secret in the UI is a leak: {suspicious}"


def test_key_values_are_never_returned_by_the_listing(admin, container):
    """Reading the key list must give masked values only."""

    container.secrets.store("OPENROUTER_API_KEY", "sk-or-v1-do-not-return-me", actor="test")
    body = admin.get("/api/cc/keys").text
    assert "do-not-return-me" not in body
    assert "sk-or-v1" not in body
    assert "API_KEY" in body  # the variable *name* is useful; the value is not


# --------------------------------------------------------------- new endpoints
def test_diagnostics_reports_real_checks(admin):
    payload = admin.get("/api/cc/diagnostics").json()
    assert payload["checks"], "diagnostics must actually check something"
    assert payload["total"] == len(payload["checks"])
    assert payload["passed"] <= payload["total"]
    for check in payload["checks"]:
        assert check["id"] and check["label"]
        assert check["detail"], "a check without a measured detail is a guess"
        if not check["ok"] and check["severity"] != "info":
            assert check["hint"], f"{check['id']} fails without telling the user how to fix it"
    assert "runtime" in payload and "install" in payload


def test_setup_wizard_reads_and_records_progress(admin):
    state = admin.get("/api/cc/settings/setup").json()
    assert state["step_order"] == ["deployment", "model", "providers", "admin", "test", "launch"]
    assert set(state["steps"]) >= set(state["step_order"])
    assert state["free_options"], "the wizard must offer the real free routes"

    saved = admin.post("/api/cc/settings/setup", json={"step": "deployment", "completed": True, "data": {"mode": "own"}})
    assert saved.status_code == 200
    assert saved.json()["completed_count"] == 1

    again = admin.get("/api/cc/settings/setup").json()
    assert again["steps"]["deployment"]["completed"] is True
    assert again["next_step"] == "model"

    finished = admin.post("/api/cc/settings/setup", json={"step": "launch", "completed": True})
    assert finished.json()["setup_completed"] is True


def test_about_credits_the_upstream_project(admin):
    payload = admin.get("/api/cc/settings/about").json()
    assert payload["upstream"]["project"] == "Hermes Agent"
    assert payload["upstream"]["author"] == "Nous Research"
    assert payload["upstream"]["license"] == "MIT"
    assert "NousResearch/hermes-agent" in payload["upstream"]["source"]
    assert payload["control_center"]["license"] == "MIT"
    assert payload["strategy"]["order"][0] == "UPSTREAM HERMES"
    assert payload["notices"], "attribution must include the third-party reality"


def test_export_import_round_trip_never_carries_readable_keys(admin, container):
    container.secrets.store("OPENROUTER_API_KEY", "sk-or-v1-THIS-MUST-NEVER-LEAK", actor="test")

    exported = admin.get("/api/cc/settings/export", params={"include_secrets": True}).json()
    body = json.dumps(exported)
    assert "THIS-MUST-NEVER-LEAK" not in body, "an export must never contain a readable key"
    assert exported["kind"] == "control-center-backup"
    assert "master key" in exported["secrets_policy"]

    without = admin.get("/api/cc/settings/export").json()
    assert without["secrets"] == []

    response = admin.post("/api/cc/settings/import", json=without)
    assert response.status_code == 200
    assert "re-enter" in response.json()["message"]


def test_import_rejects_foreign_json(admin):
    response = admin.post("/api/cc/settings/import", json={"hello": "world"})
    assert response.status_code == 400
    assert "kind" in response.text


def test_backups_are_created_listed_and_downloadable(admin):
    created = admin.post("/api/cc/settings/backups")
    assert created.status_code == 200
    name = created.json()["name"]
    assert name.endswith(".zip")

    listing = admin.get("/api/cc/settings/backups").json()
    assert any(item["name"] == name for item in listing["backups"])
    assert "master key" in listing["note"]

    download = admin.get(f"/api/cc/settings/backups/{name}")
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert "config.json" in archive.namelist()
        assert "README.txt" in archive.namelist()
        assert "MASTER KEY" in archive.read("README.txt").decode().upper()


def test_backup_download_refuses_traversal(admin):
    for name in ("../../etc/passwd", "..%2f..%2fetc%2fpasswd", "not-a-backup.zip"):
        response = admin.get(f"/api/cc/settings/backups/{name}")
        assert response.status_code in {400, 404}, name


def test_spend_guard_names_everything_that_could_cost_money(admin, container):
    payload = admin.get("/api/cc/providers/spend-guard").json()
    assert payload["free_mode"] is True
    assert payload["paid_unlocked"] is False
    assert "tiers" in payload
    assert payload["statement"]

    container.secrets.store("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key", actor="test")
    after = admin.get("/api/cc/providers/spend-guard").json()
    assert "anthropic" in after["paid_configured_not_necessarily_active"]
    entry = next(item for item in after["could_cost_money"] if item["provider"] == "anthropic")
    assert "only used if you select it" in entry["why"]
    assert after["paid_unlocked"] is False, "storing a key must never unlock paid usage"


def test_free_mode_view_carries_the_fields_the_ui_renders(admin):
    payload = admin.get("/api/cc/free-mode").json()
    for key in ("free_mode", "allow_paid_fallback", "nous_free_tier_enabled", "status", "current_cost", "policy"):
        assert key in payload, key
    assert payload["status"]["honest_note"], "the cost posture must be stated in words"
    assert payload["routes"], "the free routes must be listed"


def test_nous_free_tier_toggle_is_honest_about_the_runtime(admin):
    response = admin.put("/api/cc/free-mode/nous-free-tier", json={"enabled": True})
    assert response.status_code in {200, 502}
    payload = response.json()
    assert "third-party" in payload["honesty"]
    assert admin.get("/api/cc/free-mode").json()["nous_free_tier_enabled"] is True


def test_env_sync_policy_warns_about_plaintext(admin):
    on = admin.put("/api/cc/keys/policy/sync-to-hermes-env", json={"enabled": True})
    assert on.status_code == 200
    assert "plaintext" in on.json()["warning"]
    off = admin.put("/api/cc/keys/policy/sync-to-hermes-env", json={"enabled": False})
    assert "nothing is written to disk in plaintext" in off.json()["warning"]


def test_bootstrap_plan_lists_the_exact_commands(admin):
    payload = admin.get("/api/cc/runtime/bootstrap/plan").json()
    assert payload["commands"], "the plan must show what would run"
    assert any("pip install" in command for command in payload["commands"])
    assert payload["what_the_button_does"]


def test_the_spa_is_served_when_it_has_been_built(app):
    client, _application = app
    index = client.get("/")
    assert index.status_code == 200
    body = index.text
    if "has not been built" in body:
        pytest.skip("the SPA has not been built in this checkout")
    assert "<div id=\"root\">" in body or "<div id='root'>" in body
    assert "/assets/" in body, "the built page must reference its hashed assets"
