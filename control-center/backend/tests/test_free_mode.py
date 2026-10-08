"""FREE MODE: the promise that nothing here can quietly cost money.

The product requirement is blunt — the app must start free, must never activate a
paid provider on its own, and must say so in plain language. These tests hold the
three gates that implement it:

1. selecting a paid model while FREE MODE is on is refused before any network call;
2. allowing a paid fallback requires typing an exact confirmation phrase;
3. a fallback chain cannot begin with anything paid.
"""

from __future__ import annotations

from tests.conftest import ADMIN_PASSWORD, ADMIN_USER


def test_free_mode_is_on_by_default(admin):
    policy = admin.get("/api/cc/free-mode").json()
    assert policy["policy"]["free_mode"] is True
    assert policy["policy"]["paid_unlocked"] is False
    assert policy["routes"], "the page must list the free routes it looked for"
    labels = {route["cost_label"] for route in policy["routes"]}
    assert labels <= {"FREE", "LIMITED FREE TIER"}, labels


def test_cost_guard_explains_the_rules(admin):
    guard = admin.get("/api/cc/settings/cost-guard").json()
    assert guard["free_mode"] is True
    assert guard["allow_paid_fallback"] is False
    assert guard["acknowledgement_phrase"] == "I UNDERSTAND PAID"
    assert any("refused" in rule.lower() for rule in guard["rules"])


def test_selecting_a_paid_provider_is_blocked(admin):
    response = admin.post("/api/cc/models/select", json={"provider": "openai", "model": "gpt-5.1"})
    assert response.status_code == 402
    payload = response.json()
    assert payload["error"] == "paid_blocked"
    assert "nothing was charged" in payload["message"].lower()
    assert payload["actions"], "the UI needs somewhere to send the user next"


def test_anthropic_is_blocked_too(admin):
    response = admin.post("/api/cc/models/select", json={"provider": "anthropic", "model": "claude-opus-4.6"})
    assert response.status_code == 402


def test_paid_fallback_requires_the_exact_phrase(admin):
    refused = admin.put("/api/cc/free-mode", json={"allow_paid_fallback": True, "acknowledge": "yes"})
    assert refused.status_code == 400
    assert "I UNDERSTAND PAID" in refused.text
    assert admin.get("/api/cc/free-mode").json()["policy"]["paid_unlocked"] is False

    accepted = admin.put(
        "/api/cc/free-mode", json={"allow_paid_fallback": True, "acknowledge": "I UNDERSTAND PAID"}
    )
    assert accepted.status_code == 200
    assert admin.get("/api/cc/free-mode").json()["policy"]["paid_unlocked"] is True


def test_fallback_chain_cannot_start_with_a_paid_route(admin):
    # FREE MODE locked: the paid entry is refused outright.
    blocked = admin.put(
        "/api/cc/models/chain",
        json={"entries": [{"provider": "openai", "model": "gpt-5.1"}, {"provider": "groq", "model": "llama-3.3-70b"}]},
    )
    assert blocked.status_code in {400, 402}


def test_fallback_chain_field_is_required(admin):
    """A wrong field name must be a 422, not a silent 'saved' that stored nothing."""

    response = admin.put("/api/cc/models/chain", json={"chain": [{"provider": "groq", "model": "llama-3.3-70b"}]})
    assert response.status_code == 422
    assert "entries" in response.text


def test_free_chain_can_be_saved_and_read_back(admin):
    saved = admin.put(
        "/api/cc/models/chain",
        json={
            "entries": [
                {"provider": "groq", "model": "llama-3.3-70b-versatile"},
                {"provider": "google", "model": "gemini-2.5-flash"},
            ]
        },
    )
    assert saved.status_code == 200
    chain = saved.json()["chain"]
    assert [entry["billing"] for entry in chain] == ["free_tier", "free_tier"]
    assert all(entry["auto_activate"] for entry in chain)
    assert admin.get("/api/cc/models/chain").json()["chain"] == chain


def test_applying_a_free_chain_without_a_free_route_says_so(admin):
    response = admin.post("/api/cc/models/free-chain/apply")
    assert response.status_code == 409
    message = response.json()["message"]
    assert "no free route" in message.lower()
    assert "will not fall back to a paid provider" in message.lower()


def test_paid_lock_can_be_turned_back_off(admin):
    assert admin.put("/api/cc/free-mode", json={"free_mode": False}).status_code == 200
    assert admin.get("/api/cc/free-mode").json()["policy"]["free_mode"] is False
    assert admin.put("/api/cc/free-mode", json={"free_mode": True}).status_code == 200
    assert admin.get("/api/cc/free-mode").json()["policy"]["free_mode"] is True


def test_unknown_provider_is_not_silently_treated_as_free(admin):
    response = admin.post("/api/cc/models/select", json={"provider": "definitely-not-a-provider", "model": "x"})
    assert response.status_code in {400, 404, 502, 503}
    assert response.json()["error"] in {"model_unavailable", "provider_unreachable", "http_error", "runtime_unavailable"}


def test_free_routes_never_claim_a_paid_provider_is_free(admin):
    """The honesty rule: no paid provider may be labelled FREE anywhere in the API."""

    providers = admin.get("/api/cc/providers").json()["providers"]
    paid = {"openai", "anthropic", "xai", "deepseek", "copilot", "github-copilot", "bedrock", "vertex"}
    for provider in providers:
        if provider["id"] in paid or "openai" in str(provider.get("raw_id", "")):
            assert provider["cost_label"] in {"PAID", "UNVERIFIED"}, provider
            assert provider["billing"] in {"paid", "unknown"}, provider


def test_admin_bootstrap_still_free(admin):
    """Reaching the admin area must never require a paid key."""

    status = admin.get("/api/cc/status/detail").json()
    assert status["free_mode"]["enabled"] is True
    assert status["free_mode"]["paid_unlocked"] is False
    assert status["free_mode"]["no_cost_so_far"] is True
    assert ADMIN_USER and ADMIN_PASSWORD  # the only credential this app asks for
