"""Provider management and the encrypted key vault.

Two separate concerns live here:

* :class:`SecretVault` — the Control Center's own encrypted store of environment
  variables destined for the Hermes runtime (provider keys above all). Secrets are
  encrypted with a master key that never leaves the server, are never returned to
  the browser in clear, and are injected into the runtime's *environment* rather
  than written into upstream config files.
* Live provider state — which providers upstream can actually use right now, read
  from Hermes itself (``/api/env``, ``/api/model/options``, ``/api/providers/oauth``)
  and merged with our own money classification.

The vault deliberately does *not* try to be a general secret manager: it stores
what the agent needs, hands it to the runtime at launch, and can be emptied.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..db import utcnow
from ..security import (
    EncryptionUnavailable,
    SecurityError,
    get_redactor,
    mask_secret,
    valid_env_key,
)
from . import provider_catalog
from .client import HermesAPIError, HermesUnavailable

HERMES_ENV_FILE_HEADER = (
    "# Written by Hermes Agent Control Center because 'sync_keys_to_hermes_env' is ON.\n"
    "# The Control Center's encrypted vault is the primary store; this file exists so the\n"
    "# Hermes CLI (and anything reading $HERMES_HOME/.env) sees the same values.\n"
    "# Mode 0600. Never commit this file.\n"
)


class VaultError(RuntimeError):
    pass


@dataclass
class VaultEntry:
    name: str
    provider: str
    label: str
    billing: str
    updated_at: str
    created_by: str
    last_used_at: str
    source: str  # vault | runtime-env

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "provider": self.provider,
            "provider_label": self.label,
            "billing": self.billing,
            "updated_at": self.updated_at,
            "created_by": self.created_by,
            "last_used_at": self.last_used_at,
            "source": self.source,
            "masked": mask_secret(self.name),
        }


class SecretVault:
    """Encrypted, server-side storage for runtime environment variables."""

    def __init__(self, container) -> None:
        self.container = container
        self.db = container.db
        self.box = container.secret_box
        self.settings = container.settings

    # ------------------------------------------------------------------ basics
    @property
    def available(self) -> bool:
        return self.box.available

    def list_rows(self) -> list[dict]:
        return self.db.query("SELECT name, created_at, updated_at, created_by, last_used_at FROM secrets ORDER BY name")

    def names(self) -> list[str]:
        return [row["name"] for row in self.list_rows()]

    def has(self, name: str) -> bool:
        return bool(self.db.query_one("SELECT name FROM secrets WHERE name = ?", (name,)))

    def reveal_safe(self, name: str) -> str:
        """Internal use only (redaction registry, runtime env). Never returned by the API."""

        row = self.db.query_one("SELECT ciphertext FROM secrets WHERE name = ?", (name,))
        if not row:
            return ""
        try:
            return self.box.decrypt(row["ciphertext"])
        except (SecurityError, EncryptionUnavailable):
            return ""

    def masked(self, name: str) -> str:
        value = self.reveal_safe(name)
        return mask_secret(value) if value else "(unreadable)"

    def store(self, name: str, value: str, *, actor: str = "system") -> dict:
        if not valid_env_key(name):
            raise VaultError("Environment variable names must be A-Z, 0-9 and _, starting with a letter.")
        if not value:
            raise VaultError("The value is empty.")
        if not self.available:
            raise VaultError(
                "Encrypted key storage is unavailable: the 'cryptography' package is not installed in the "
                "Control Center's interpreter. Install it (pip install 'hermes-control-center[crypto]') and retry."
            )
        ciphertext = self.box.encrypt(value)
        get_redactor().register(value)
        existing = self.has(name)
        if existing:
            self.db.execute(
                "UPDATE secrets SET ciphertext = ?, updated_at = ?, created_by = ? WHERE name = ?",
                (ciphertext, utcnow(), actor, name),
            )
        else:
            self.db.insert(
                "INSERT INTO secrets (name, ciphertext, created_at, updated_at, created_by) VALUES (?, ?, ?, ?, ?)",
                (name, ciphertext, utcnow(), utcnow(), actor),
            )
        self.container.audit("key_stored", actor=actor, target=name)
        self.container.logs.provider(f"Secret '{name}' stored (encrypted at rest).", level="WARNING")
        if self.db.get_setting("sync_keys_to_hermes_env", False):
            with contextlib.suppress(Exception):
                self.sync_to_hermes_env()
        return {"name": name, "created": not existing}

    def remove(self, name: str, *, actor: str = "system") -> bool:
        value = self.reveal_safe(name)
        removed = self.db.execute("DELETE FROM secrets WHERE name = ?", (name,))
        if value:
            get_redactor().forget(value)
        if removed:
            self.container.audit("key_removed", actor=actor, target=name, level="warning")
            self.container.logs.provider(f"Secret '{name}' removed.", level="WARNING")
        return bool(removed)

    def mark_used(self, names: list[str]) -> None:
        if not names:
            return
        for name in names:
            self.db.execute("UPDATE secrets SET last_used_at = ? WHERE name = ?", (utcnow(), name))

    # ------------------------------------------------------------------ runtime
    def runtime_env(self) -> dict[str, str]:
        """Values injected into the runtime process environment at launch."""

        env: dict[str, str] = {}
        for row in self.list_rows():
            value = self.reveal_safe(row["name"])
            if value:
                env[row["name"]] = value
        if env:
            self.mark_used(list(env))
        return env

    # -------------------------------------------------------------- .env mirror
    def env_file_path(self) -> Path:
        return Path(self.settings.hermes_home) / ".env"

    def sync_to_hermes_env(self, *, actor: str = "system", remove: bool = False) -> dict:
        """Mirror stored values into ``$HERMES_HOME/.env`` (0600), or take them back out.

        Off by default. The vault + environment injection is the safer path; this
        exists because the Hermes CLI and some upstream helpers read that file.

        With ``remove=True`` the vault-managed variables are stripped again (leaving
        anything you put there yourself), and the file is deleted when nothing is
        left — a key you removed should not survive in a plaintext file.
        """

        path = self.env_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict[str, str] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, _, value = stripped.partition("=")
                existing[key.strip()] = value.strip().strip('"').strip("'")
        stored = {row["name"]: self.reveal_safe(row["name"]) for row in self.list_rows()}
        if remove:
            remaining = {name: value for name, value in existing.items() if name not in stored}
            if not remaining:
                if path.exists():
                    path.unlink()
                self.container.audit("keys_removed_from_env", actor=actor, target=str(path), level="warning")
                self.container.logs.security(
                    f"Vault-managed variables were removed from {path}; the file had nothing else in it, so it was deleted.",
                    level="INFO",
                )
                return {"ok": True, "path": str(path), "variables": 0, "removed": True, "deleted": True}
            body = [HERMES_ENV_FILE_HEADER]
            for name in sorted(remaining):
                value = remaining[name].replace("\n", " ")
                needs_quotes = any(character in value for character in ' "#\'')
                body.append(f'{name}="{value}"' if needs_quotes else f"{name}={value}")
            path.write_text("\n".join(body) + "\n", encoding="utf-8")
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
            self.container.audit("keys_removed_from_env", actor=actor, target=str(path), level="warning")
            self.container.logs.security(
                f"Vault-managed variables were removed from {path}; {len(remaining)} variable(s) you set yourself remain.",
                level="INFO",
            )
            return {
                "ok": True,
                "path": str(path),
                "variables": len(remaining),
                "removed": True,
                "deleted": False,
            }
        merged = {**existing, **{name: value for name, value in stored.items() if value}}
        body = [HERMES_ENV_FILE_HEADER]
        for name in sorted(merged):
            value = merged[name].replace("\n", " ")
            needs_quotes = any(character in value for character in ' "#\'')
            body.append(f'{name}="{value}"' if needs_quotes else f"{name}={value}")
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover
            pass
        self.container.audit("keys_synced_to_env", actor=actor, target=str(path), level="warning")
        self.container.logs.security(
            f"Stored secrets mirrored into {path} in plaintext at your request (mode 0600).", level="WARNING"
        )
        return {"ok": True, "path": str(path), "variables": len(merged), "new": len(set(stored) - set(existing))}


# --------------------------------------------------------------- live provider


def _env_catalog(container) -> dict[str, dict]:
    """The last snapshot of upstream's environment catalogue."""

    return container.db.get_setting("hermes_env_catalog", {}) or {}


def _store_env_catalog(container, payload: dict) -> None:
    if isinstance(payload, dict) and payload:
        container.db.set_setting("hermes_env_catalog", payload)


async def refresh_env_catalog(container) -> dict:
    client = container.supervisor.client()
    payload = await client.get("/api/env", timeout=45)
    if isinstance(payload, dict):
        _store_env_catalog(container, payload)
    return payload if isinstance(payload, dict) else {}


async def list_providers(container, *, refresh_env: bool = True) -> dict:
    """Merges upstream's live view with our cost classification."""

    client = container.supervisor.client()
    options: dict = {}
    oauth: dict = {}
    env: dict = {}
    errors: list[str] = []

    try:
        options = await client.get("/api/model/options", timeout=30) or {}
    except (HermesAPIError, HermesUnavailable) as exc:
        errors.append(_detail(exc))
    try:
        oauth = await client.get("/api/providers/oauth", timeout=30) or {}
    except (HermesAPIError, HermesUnavailable) as exc:
        errors.append(_detail(exc))
    if refresh_env:
        try:
            env = await refresh_env_catalog(container)
        except (HermesAPIError, HermesUnavailable) as exc:
            errors.append(_detail(exc))
    else:
        env = _env_catalog(container)

    live_providers: dict[str, dict] = {}
    for entry in options.get("providers", []) if isinstance(options, dict) else []:
        slug = entry.get("slug") or entry.get("name") or ""
        identifier = provider_catalog.normalize_id(slug or entry.get("name", ""))
        live_providers[identifier] = {
            "slug": slug,
            "models": entry.get("models") or [],
            "total_models": entry.get("total_models") or len(entry.get("models") or []),
            "authenticated": bool(entry.get("authenticated")),
            "is_current": bool(entry.get("is_current")),
            "source": entry.get("source") or "hermes",
            "capabilities": entry.get("capabilities") or {},
        }

    env_by_provider: dict[str, list[dict]] = {}
    for name, meta in env.items():
        if not isinstance(meta, dict):
            continue
        provider = provider_catalog.normalize_id(str(meta.get("provider") or ""))
        if not provider:
            continue
        category = str(meta.get("category") or "")
        if category not in {"provider", "provider_key", "model", "api_key", "cloud"} and meta.get("provider_primary") is not True:
            continue
        env_by_provider.setdefault(provider, []).append(
            {
                "name": name,
                "description": str(meta.get("description") or ""),
                "is_set": bool(meta.get("is_set")),
                "is_password": bool(meta.get("is_password")),
                "primary": bool(meta.get("provider_primary")),
                "url": str(meta.get("url") or ""),
                "tools": meta.get("tools") or [],
            }
        )

    vault_names = set(container.secrets.names())
    providers: list[dict] = []
    for identifier in provider_catalog.known_provider_ids():
        classification = provider_catalog.classify_provider(identifier)
        live = live_providers.get(identifier, {})
        env_vars = env_by_provider.get(identifier, [])
        registry_vars = [{"name": name, "is_set": False, "is_password": True, "description": "", "primary": True, "url": "", "tools": []}
                         for name in classification.get("env_vars", []) if name not in {item["name"] for item in env_vars}]
        env_vars = env_vars + registry_vars
        vaulted = [item["name"] for item in env_vars if item["name"] in vault_names]
        runtime_set = [item["name"] for item in env_vars if item["is_set"]]
        has_credentials = bool(vaulted or runtime_set) or not classification["requires_key"]
        providers.append(
            {
                **classification,
                "models": live.get("models", []),
                "model_count": live.get("total_models", 0),
                "authenticated": bool(live.get("authenticated")) or has_credentials,
                "reported_authenticated": bool(live.get("authenticated")),
                "is_current": bool(live.get("is_current")),
                "env_vars_live": env_vars,
                "keys_in_vault": vaulted,
                "keys_in_runtime_env": runtime_set,
                "usable": bool(live.get("authenticated")) or has_credentials,
                "blocked_by_free_mode": classification["billing"] == provider_catalog.PAID and not container.paid_unlocked,
                "available_in_upstream": identifier in live_providers,
                "shipped_by_upstream": identifier in provider_catalog.registry(),
            }
        )

    # Providers upstream knows about that we did not classify separately.
    for identifier, live in live_providers.items():
        if any(provider["id"] == identifier for provider in providers):
            continue
        classification = provider_catalog.classify_provider(identifier)
        providers.append(
            {
                **classification,
                "models": live.get("models", []),
                "model_count": live.get("total_models", 0),
                "authenticated": bool(live.get("authenticated")),
                "reported_authenticated": bool(live.get("authenticated")),
                "is_current": bool(live.get("is_current")),
                "env_vars_live": [],
                "keys_in_vault": [],
                "keys_in_runtime_env": [],
                "usable": bool(live.get("authenticated")),
                "blocked_by_free_mode": classification["billing"] == provider_catalog.PAID and not container.paid_unlocked,
                "available_in_upstream": True,
                "shipped_by_upstream": False,
            }
        )

    providers.sort(key=lambda item: (0 if item["usable"] else 1, item["billing"] != provider_catalog.FREE_LOCAL, item["label"].lower()))
    usable_free = [item for item in providers if item["usable"] and provider_catalog.is_free_billing(item["billing"])]
    return {
        "providers": providers,
        "oauth": oauth,
        "errors": errors,
        "summary": {
            "total": len(providers),
            "usable": sum(1 for item in providers if item["usable"]),
            "usable_free": len(usable_free),
            "paid_locked": sum(1 for item in providers if item["blocked_by_free_mode"]),
            "free_mode": container.free_mode,
            "paid_unlocked": container.paid_unlocked,
        },
        "env_catalog_size": len(env),
    }


async def test_provider(container, provider: str, *, model: str = "") -> dict:
    """A real connectivity check, not a green light we invented.

    Local endpoints are probed directly (HTTP ``/models``); everything else is
    tested through the agent's own inference path so a success means the whole
    chain works, and we never send a key anywhere new.
    """

    classification = provider_catalog.classify_provider(provider)
    identifier = classification["id"]
    result: dict[str, Any] = {
        "provider": identifier,
        "label": classification["label"],
        "billing": classification["billing"],
        "cost_label": classification["cost_label"],
    }

    base_url = classification.get("base_url") or ""
    if provider_catalog.is_local_endpoint(base_url) or identifier in {"lmstudio", "custom", "ollama", "llamacpp"}:
        if not base_url:
            result.update(ok=False, stage="config", message="No endpoint URL configured for this local provider.")
            return result
        import httpx

        target = base_url.rstrip("/").removesuffix("/v1")
        probes = [f"{target}/v1/models", f"{target}/api/tags", f"{target}/models"]
        for probe in probes:
            try:
                with httpx.Client(timeout=6.0) as client:
                    response = client.get(probe)
                if response.status_code < 400:
                    payload = response.json()
                    models = payload.get("data") or payload.get("models") or []
                    result.update(ok=True, stage="local-endpoint", probe=probe, models=len(models), message="Local endpoint answered.")
                    return result
            except Exception:  # noqa: BLE001 - keep probing
                continue
        result.update(
            ok=False,
            stage="local-endpoint",
            message=f"Nothing answered on {base_url}. Is the local server (LM Studio, Ollama, vLLM) running?",
        )
        return result

    if container.paid_unlocked is False and classification["billing"] == provider_catalog.PAID:
        result.update(
            ok=False,
            stage="policy",
            message="Paid providers are locked by FREE MODE, so nothing was sent and nothing could be billed.",
            next="Unlock paid usage in Settings if you really want to test this provider.",
        )
        return result

    # Everything else: run one real request through the agent's own bridge.
    from ..routes.chat import bridge_completion

    try:
        completion = await bridge_completion(
            container,
            prompt="Reply with the single word: ready",
            model=model or "hermes-agent",
            timeout=120.0,
        )
    except Exception as exc:  # noqa: BLE001
        result.update(ok=False, stage="bridge", message=str(exc))
        return result
    result.update(
        ok=bool(completion.get("ok")),
        stage="agent-inference",
        model=completion.get("model"),
        latency_ms=completion.get("latency_ms"),
        message=completion.get("text", "")[:400] or completion.get("message", ""),
        usage=completion.get("usage"),
    )
    return result


def _detail(exc: Exception) -> str:
    if isinstance(exc, HermesAPIError):
        return exc.human_detail()
    return str(exc)


def provider_keys_env_names(provider: str) -> list[str]:
    """Env var candidates for a provider, so a pasted key lands in the right place."""

    classification = provider_catalog.classify_provider(provider)
    names = [item for item in classification.get("env_vars", [])]
    identifier = classification["id"]
    if not names:
        names = [f"{identifier.upper().replace('-', '_')}_API_KEY"]
    primary = [name for name in names if name.endswith("_API_KEY")] or names
    return primary + [name for name in names if name not in primary]
