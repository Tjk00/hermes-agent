"""The key vault: encrypted at rest, masked in the UI, never in a response or a log.

The rule the product promises is stronger than "we hash it": a key must be *usable*
by the server (so it can be handed to the agent) yet invisible everywhere a human or
a browser could look. These tests try to see it and fail.
"""

from __future__ import annotations

import json

import pytest


def test_stored_key_is_encrypted_at_rest(container):
    secret = "sk-or-v1-abcdef0123456789abcdef0123456789"
    container.secrets.store("OPENROUTER_API_KEY", secret, actor="test")

    row = container.db.query_one("SELECT ciphertext FROM secrets WHERE name = ?", ("OPENROUTER_API_KEY",))
    assert row is not None
    assert secret not in row["ciphertext"], "the vault must not store the key in readable form"
    assert container.secrets.reveal_safe("OPENROUTER_API_KEY") == secret


def test_key_list_masks_every_value(admin, container):
    secret = "sk-or-v1-abcdef0123456789abcdef0123456789"
    stored = admin.put("/api/cc/keys/OPENROUTER_API_KEY", json={"value": secret, "provider": "openrouter"})
    assert stored.status_code in {200, 201}, stored.text
    body = stored.text
    assert secret not in body, "the value must never travel back to the browser"

    listed = admin.get("/api/cc/keys")
    assert listed.status_code == 200
    assert secret not in listed.text
    entries = [item for group in listed.json()["groups"] for item in group["variables"]]
    entry = next(item for item in entries if item["name"] == "OPENROUTER_API_KEY")
    assert entry["in_vault"] is True
    assert entry["masked"] and "•" in entry["masked"]
    assert secret[-6:] not in listed.text  # not even a long tail of the key
    assert entry["billing"] in {"mixed", "free_tier", "paid", "unknown"}


def test_reveal_requires_an_explicit_call_and_is_audited(admin, container):
    secret = "sk-or-v1-abcdef0123456789abcdef0123456789"
    admin.put("/api/cc/keys/OPENROUTER_API_KEY", json={"value": secret, "provider": "openrouter"})
    revealed = admin.post("/api/cc/keys/OPENROUTER_API_KEY/reveal")
    assert revealed.status_code == 200
    assert revealed.json()["value"] == secret

    audits = container.db.query("SELECT action FROM audit_events WHERE action LIKE '%reveal%'")
    assert audits, "revealing a secret must leave a trace"


def test_removing_a_key_really_removes_it(admin, container):
    admin.put("/api/cc/keys/GROQ_API_KEY", json={"value": "gsk_" + "a" * 40, "provider": "groq"})
    assert admin.delete("/api/cc/keys/GROQ_API_KEY").status_code == 200
    assert container.secrets.has("GROQ_API_KEY") is False
    assert admin.post("/api/cc/keys/GROQ_API_KEY/reveal").status_code == 404


def test_keys_are_redacted_from_logs_and_exports(admin, container):
    secret = "sk-ant-api03-" + "b" * 40
    admin.put("/api/cc/keys/ANTHROPIC_API_KEY", json={"value": secret, "provider": "anthropic"})
    container.logs.info(f"Operator mentioned the key {secret} in a log line", "SECURITY")

    logs = admin.get("/api/cc/logs", params={"source": "control-center"})
    assert secret not in logs.text

    exported = admin.get("/api/cc/logs/export", params={"format": "json"})
    assert secret not in exported.text

    backup = admin.get("/api/cc/settings/backup")
    assert secret not in backup.text
    assert backup.json()["kind"] == "control-center-backup"


def test_env_mirror_is_opt_in_and_private(cc_home, monkeypatch, tmp_path):
    secret = "gsk_" + "c" * 40
    # Settings is frozen and read once, so point a *fresh* install at a temporary
    # HERMES_HOME before building it — the same thing an operator does with CC_HERMES_HOME.
    monkeypatch.setenv("CC_HERMES_HOME", str(tmp_path / "hermes"))
    from app import config as config_module
    from app import container as container_module

    config_module.reset_settings_for_tests()
    container_module.reset_container_for_tests()
    container = container_module.build_container()
    container.initialise()
    container.secrets.store("GROQ_API_KEY", secret, actor="test")

    result = container.secrets.sync_to_hermes_env(actor="test")
    path = container.secrets.env_file_path()
    assert result["ok"] and path.exists()
    assert path.stat().st_mode & 0o777 == 0o600, "the mirrored .env must not be world readable"
    assert f"GROQ_API_KEY={secret}" in path.read_text()

    removed = container.secrets.sync_to_hermes_env(actor="test", remove=True)
    assert removed["ok"] and removed["removed"] is True
    # The file held nothing but vault-managed values, so it is deleted outright
    # rather than left on disk with a plaintext key inside it.
    assert removed["deleted"] is True
    assert not path.exists()
    assert secret not in (path.read_text() if path.exists() else "")


def test_keys_never_reach_the_runtime_environment_by_default(container):
    container.secrets.store("MISTRAL_API_KEY", "sk-" + "d" * 32, actor="test")
    env = container.secrets.runtime_env()
    # The vault may hand keys to the agent's process environment — that is the point —
    # but only through this one function, and never into our own os.environ.
    assert env.get("MISTRAL_API_KEY", "").startswith("sk-")
    import os

    assert "MISTRAL_API_KEY" not in os.environ


def test_invalid_variable_names_are_rejected(admin):
    response = admin.put("/api/cc/keys/bad name; drop table", json={"value": "x"})
    assert response.status_code in {400, 404, 422}


def test_empty_value_is_rejected(admin):
    response = admin.put("/api/cc/keys/OPENAI_API_KEY", json={"value": ""})
    assert response.status_code == 422


def test_provider_test_never_sends_a_key_to_a_paid_provider_while_locked(admin):
    """With FREE MODE on, even 'test connection' must refuse a paid provider."""

    admin.put("/api/cc/keys/OPENAI_API_KEY", json={"value": "sk-" + "e" * 40, "provider": "openai"})
    result = admin.post("/api/cc/providers/test", json={"provider": "openai"})
    assert result.status_code in {200, 402, 503}
    payload = result.json()
    if result.status_code == 200:
        assert payload.get("ok") is False
        assert payload.get("stage") == "policy"
        assert "nothing was sent" in payload.get("message", "").lower()
    else:
        assert payload["error"] in {"paid_blocked", "runtime_unavailable", "provider_unreachable"}


@pytest.mark.parametrize("name", ["API_SERVER_KEY", "OPENROUTER_API_KEY", "CUSTOM_ENDPOINT_BASE"])
def test_env_var_name_validation(container, name):
    from app.security import valid_env_key

    assert valid_env_key(name) is True
    assert valid_env_key("not a var") is False
