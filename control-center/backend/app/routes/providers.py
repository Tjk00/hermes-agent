"""Providers, the encrypted key vault, and FREE MODE policy."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..container import Container
from ..db import utcnow
from ..deps import ContainerDep, Principal, client_ip, enforce_rate_limit, mutation_principal, require_admin
from ..hermes import provider_catalog, providers as provider_service
from ..hermes.client import HermesAPIError, HermesUnavailable

router = APIRouter(tags=["providers"])

PAID_ACK = provider_catalog.PAID_ACK  # single definition lives with the catalogue


class KeyBody(BaseModel):
    value: str = Field(min_length=1, max_length=8192)
    provider: str = Field(default="", max_length=120)


class PolicyBody(BaseModel):
    free_mode: bool | None = None
    lock_paid_providers: bool | None = None
    allow_paid_fallback: bool | None = None
    acknowledge: str = Field(default="", max_length=120)
    nous_free_tier_enabled: bool | None = None


def _variable_to_provider(variable: str) -> str:
    """``ANTHROPIC_API_KEY`` → ``anthropic``.

    Operators store keys under their documented environment-variable names, so the
    spend guard has to recognise those as the provider they belong to.
    """

    cleaned = (variable or "").strip().upper()
    for suffix in ("_API_KEY", "_APIKEY", "_AUTH_TOKEN", "_TOKEN", "_KEY"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    return provider_catalog.normalize_id(cleaned)


class SetFreeTierBody(BaseModel):
    enabled: bool


class TestBody(BaseModel):
    provider: str = Field(min_length=1, max_length=120)
    model: str = Field(default="", max_length=200)


async def _call(container: Container, method: str, path: str, **kwargs: Any) -> Any:
    client = container.supervisor.client()
    try:
        return await client.request(method, path, **kwargs)
    except HermesAPIError as exc:
        raise HTTPException(status_code=exc.status if 400 <= exc.status < 600 else 502, detail=exc.human_detail()) from exc
    except HermesUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# ------------------------------------------------------------------ providers
@router.get("/providers")
async def list_providers(
    principal: Annotated[Principal, Depends(require_admin)],
    container: ContainerDep,
    refresh: bool = Query(True, description="re-read the environment catalogue from Hermes"),
) -> dict:
    payload = await provider_service.list_providers(container, refresh_env=refresh)
    payload["tiers"] = provider_catalog.cost_tiers()
    payload["policy"] = {
        "free_mode": container.free_mode,
        "paid_unlocked": container.paid_unlocked,
        "lock_paid_providers": bool(container.db.get_setting("lock_paid_providers", True)),
        "nous_free_tier_enabled": bool(container.db.get_setting("nous_free_tier_enabled", False)),
    }
    return payload


@router.get("/providers/catalog")
async def provider_catalog_static(principal: Annotated[Principal, Depends(require_admin)]) -> dict:
    """The curated cost table, with no live state — safe to cache."""

    return provider_catalog.catalog_snapshot()


@router.post("/providers/refresh")
async def refresh_providers(principal: Annotated[Principal, Depends(mutation_principal)], container: ContainerDep) -> dict:
    env = await provider_service.refresh_env_catalog(container)
    container.logs.provider(f"Provider catalogue refreshed from Hermes ({len(env)} environment variables).")
    return {"ok": True, "variables": len(env)}


@router.post("/providers/test")
async def test_provider(body: TestBody, principal: Annotated[Principal, Depends(mutation_principal)], container: ContainerDep) -> dict:
    result = await provider_service.test_provider(container, body.provider, model=body.model)
    container.audit(
        "provider_tested",
        actor=principal.username,
        target=body.provider,
        detail=f"ok={result.get('ok')} stage={result.get('stage')}",
        level="info" if result.get("ok") else "warning",
    )
    return result


# -------------------------------------------------------------- key vault
@router.get("/keys")
async def list_keys(principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep) -> dict:
    """Everything a key can be set in: our vault first, then the runtime env."""

    catalog = provider_service._env_catalog(container)  # noqa: SLF001 - same package by design
    rows = {row["name"]: row for row in container.secrets.list_rows()}
    groups: dict[str, list[dict]] = {}
    for name, meta in (catalog or {}).items():
        if not isinstance(meta, dict):
            continue
        provider = str(meta.get("provider") or meta.get("category") or "other")
        label = str(meta.get("provider_label") or "")
        classification = provider_catalog.classify_provider(str(meta.get("provider") or ""))
        row = rows.get(name)
        groups.setdefault(provider, []).append(
            {
                "name": name,
                "description": str(meta.get("description") or ""),
                "is_set_in_runtime": bool(meta.get("is_set")),
                "is_password": bool(meta.get("is_password")),
                "url": str(meta.get("url") or ""),
                "tools": meta.get("tools") or [],
                "provider": provider,
                "provider_label": label,
                "billing": classification["billing"],
                "cost_label": classification["cost_label"],
                "in_vault": bool(row),
                "masked": container.secrets.masked(name) if row else "",
                "updated_at": (row or {}).get("updated_at") or "",
                "stored_by": (row or {}).get("created_by") or "",
            }
        )
    for name, row in rows.items():
        if any(item["name"] == name for group in groups.values() for item in group):
            continue
        classification = provider_catalog.classify_provider("")
        groups.setdefault("custom", []).append(
            {
                "name": name,
                "description": "Stored by you in the Control Center vault.",
                "is_set_in_runtime": False,
                "is_password": True,
                "url": "",
                "tools": [],
                "provider": "custom",
                "provider_label": "Custom environment variable",
                "billing": classification["billing"],
                "cost_label": classification["cost_label"],
                "in_vault": True,
                "masked": container.secrets.masked(name),
                "updated_at": row["updated_at"],
                "stored_by": row["created_by"] or "",
            }
        )

    return {
        "groups": [
            {"provider": key, "variables": sorted(items, key=lambda item: (not item["in_vault"], item["name"]))}
            for key, items in sorted(groups.items())
        ],
        "vault": {
            "available": container.secrets.available,
            "backend": container.secret_box.backend_name,
            "count": len(rows),
            "sync_to_hermes_env": bool(container.db.get_setting("sync_keys_to_hermes_env", False)),
            "env_file": str(container.secrets.env_file_path()),
        },
        "notes": [
            "Keys you store here are encrypted with a master key kept in CC_HOME (mode 0600) and injected into the "
            "Hermes runtime environment at launch.",
            "They are never returned in full to the browser: the UI only ever shows a mask, unless you press Reveal.",
            "Variables already set in the runtime environment are marked 'set in runtime' and can be left alone.",
        ],
    }


@router.put("/keys/{name}")
async def store_key(
    name: str,
    body: KeyBody,
    request: Request,
    principal: Annotated[Principal, Depends(mutation_principal)],
    container: ContainerDep,
) -> dict:
    enforce_rate_limit(request, "sensitive")
    try:
        result = container.secrets.store(name.upper(), body.value, actor=principal.username)
    except provider_service.VaultError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    container.audit("key_stored", actor=principal.username, target=name.upper(), ip=client_ip(request), level="warning")
    return {
        **result,
        "provider": provider_catalog.classify_provider(body.provider)["label"] if body.provider else "",
        "masked": container.secrets.masked(name.upper()),
        "note": "Stored encrypted. Restart the runtime to hand it to a fresh process.",
    }


@router.post("/keys/{name}/reveal")
async def reveal_key(
    name: str,
    request: Request,
    principal: Annotated[Principal, Depends(mutation_principal)],
    container: ContainerDep,
) -> dict:
    """Returns the decrypted value to an authenticated admin, and records that."""

    enforce_rate_limit(request, "sensitive")
    value = container.secrets.reveal_safe(name.upper())
    if not value:
        raise HTTPException(status_code=404, detail="No such key in the vault.")
    container.audit("key_revealed", actor=principal.username, target=name.upper(), ip=client_ip(request), level="warning")
    container.logs.security(f"Secret '{name.upper()}' was revealed to {principal.username}.", level="WARNING")
    return {"name": name.upper(), "value": value, "warning": "Revealed values are never logged, but treat this page as sensitive."}


@router.delete("/keys/{name}")
async def delete_key(
    name: str,
    principal: Annotated[Principal, Depends(mutation_principal)],
    container: ContainerDep,
) -> dict:
    removed = container.secrets.remove(name.upper(), actor=principal.username)
    if not removed:
        raise HTTPException(status_code=404, detail="No such key in the vault.")
    return {"ok": True, "removed": name.upper(), "note": "Restart the runtime so the old value stops being injected."}


@router.post("/keys/sync-to-hermes-env")
async def sync_keys(principal: Annotated[Principal, Depends(mutation_principal)], container: ContainerDep) -> dict:
    try:
        result = container.secrets.sync_to_hermes_env(actor=principal.username)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Could not write $HERMES_HOME/.env: {exc}") from exc
    container.db.set_setting("sync_keys_to_hermes_env", True)
    return result


@router.delete("/keys/sync-to-hermes-env")
async def stop_sync_keys(
    principal: Annotated[Principal, Depends(mutation_principal)],
    container: ContainerDep,
    remove_from_env: bool = Query(True, description="also strip the vault-managed values out of $HERMES_HOME/.env"),
) -> dict:
    container.db.set_setting("sync_keys_to_hermes_env", False)
    result: dict = {}
    if remove_from_env:
        try:
            result = container.secrets.sync_to_hermes_env(actor=principal.username, remove=True)
        except OSError as exc:
            result = {"ok": False, "error": str(exc)}
    container.audit("keys_sync_disabled", actor=principal.username, detail=f"removed={remove_from_env}")
    return {
        "ok": True,
        "env_cleanup": result,
        "note": (
            "Mirroring is off. Vault-managed values were removed from $HERMES_HOME/.env; anything you wrote there "
            "yourself is untouched."
            if remove_from_env
            else "Mirroring is off. Existing values in $HERMES_HOME/.env were left as they are."
        ),
    }


# --------------------------------------------------------------- free mode
@router.get("/free-mode")
async def free_mode_state(principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep) -> dict:
    summary = provider_catalog.free_route_summary(container)
    model = container.db.get_setting("active_model", {}) or {}
    provider = str(model.get("provider") or "")
    billing = str(model.get("billing") or provider_catalog.UNKNOWN)
    return {
        **summary,
        # Flattened for the UI: same values as the policy block below, one place to read.
        "allow_paid_fallback": bool(container.db.get_setting("allow_paid_fallback", False)),
        "nous_free_tier_enabled": bool(container.db.get_setting("nous_free_tier_enabled", False)),
        "active_model": model,
        "current_cost": {
            "billing": billing,
            "label": provider_catalog.BILLING_LABELS.get(billing, billing),
            "cost_note": provider_catalog.classify_provider(provider)["cost_note"] if provider else "",
        },
        "status": {
            "label": provider_catalog.BILLING_LABELS.get(billing, billing),
            "provider": provider,
            "model": str(model.get("model") or ""),
            "cost": (
                "No cost: this route does not bill per request."
                if billing in {provider_catalog.FREE_LOCAL, provider_catalog.FREE_TIER}
                else ("Billed to your own provider account." if billing == provider_catalog.PAID else "Cost not verified.")
            ),
            "honest_note": (
                "Costs are the provider's to change; this app reports the tier it can verify and never calls a paid "
                "service free."
            ),
        },
        "policy": {
            "free_mode": container.free_mode,
            "lock_paid_providers": bool(container.db.get_setting("lock_paid_providers", True)),
            "paid_unlocked": container.paid_unlocked,
            "paid_unlock_ack": container.db.get_setting("paid_unlock_ack", ""),
            "nous_free_tier_enabled": bool(container.db.get_setting("nous_free_tier_enabled", False)),
        },
        "statement": (
            "FREE MODE is on: paid providers are refused, paid fallback never activates on its own, and the app tells "
            "you when a free route is rate limited."
            if container.free_mode
            else "FREE MODE is off. Paid providers can be selected, but still only with a key you stored yourself."
        ),
    }


@router.put("/free-mode")
async def update_free_mode(
    body: PolicyBody,
    request: Request,
    principal: Annotated[Principal, Depends(mutation_principal)],
    container: ContainerDep,
) -> dict:
    applied: dict[str, Any] = {}
    if body.free_mode is not None:
        container.db.set_setting("free_mode", body.free_mode)
        applied["free_mode"] = body.free_mode
    if body.lock_paid_providers is not None:
        container.db.set_setting("lock_paid_providers", body.lock_paid_providers)
        applied["lock_paid_providers"] = body.lock_paid_providers
    if body.nous_free_tier_enabled is not None:
        container.db.set_setting("nous_free_tier_enabled", body.nous_free_tier_enabled)
        applied["nous_free_tier_enabled"] = body.nous_free_tier_enabled
    if body.allow_paid_fallback is not None:
        if body.allow_paid_fallback:
            if body.acknowledge.strip() != PAID_ACK:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Paid usage is not enabled. To acknowledge that paid providers can charge your own accounts, "
                        f'send acknowledge="{PAID_ACK}".'
                    ),
                )
            container.db.set_setting("allow_paid_fallback", True)
            container.db.set_setting("paid_unlock_ack", body.acknowledge.strip())
            container.db.set_setting("paid_unlock_at", utcnow())
            container.logs.warning(
                f"Paid providers unlocked by {principal.username}. Requests routed to paid providers will be billed "
                "to the account that owns the key.",
                "SECURITY",
            )
        else:
            container.db.set_setting("allow_paid_fallback", False)
            container.db.set_setting("paid_unlock_ack", "")
        applied["allow_paid_fallback"] = body.allow_paid_fallback
    container.audit("free_mode_updated", actor=principal.username, detail=str(applied)[:300], ip=client_ip(request))
    return {
        "ok": True,
        "applied": applied,
        "policy": {
            "free_mode": container.free_mode,
            "paid_unlocked": container.paid_unlocked,
            "nous_free_tier_enabled": bool(container.db.get_setting("nous_free_tier_enabled", False)),
        },
        "next": "Restart the runtime if you changed the free-tier setting, so the new environment applies.",
    }


# --------------------------------------------------- provider passthroughs
@router.post("/providers/validate")
async def validate_provider(body: dict, principal: Annotated[Principal, Depends(mutation_principal)], container: ContainerDep) -> dict:
    return await _call(container, "POST", "/api/providers/validate", json_body=body, timeout=60)


@router.get("/providers/custom-endpoints")
async def custom_endpoints(principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep) -> dict:
    return await _call(container, "GET", "/api/providers/custom-endpoints", timeout=30)


@router.post("/providers/custom-endpoints")
async def add_custom_endpoint(body: dict, principal: Annotated[Principal, Depends(mutation_principal)], container: ContainerDep) -> dict:
    result = await _call(container, "POST", "/api/providers/custom-endpoints", json_body=body, timeout=60)
    container.audit("custom_endpoint_added", actor=principal.username, detail=str(body.get("name") or "")[:120])
    return result


@router.post("/providers/custom-endpoints/{endpoint_id}/activate")
async def activate_custom_endpoint(endpoint_id: str, principal: Annotated[Principal, Depends(mutation_principal)], container: ContainerDep) -> dict:
    result = await _call(container, "POST", f"/api/providers/custom-endpoints/{endpoint_id}/activate", json_body={}, timeout=60)
    container.audit("custom_endpoint_activated", actor=principal.username, target=endpoint_id)
    return result


@router.delete("/providers/custom-endpoints/{endpoint_id}")
async def delete_custom_endpoint(endpoint_id: str, principal: Annotated[Principal, Depends(mutation_principal)], container: ContainerDep) -> dict:
    result = await _call(container, "DELETE", f"/api/providers/custom-endpoints/{endpoint_id}", timeout=30)
    container.audit("custom_endpoint_removed", actor=principal.username, target=endpoint_id)
    return result


# OAuth (device flow) — upstream implements it over HTTP so its own SPA can use it.
# We pass it through, which means provider sign-in works from a phone too.
@router.post("/providers/oauth/{provider_id}/start")
async def oauth_start(
    provider_id: str,
    principal: Annotated[Principal, Depends(mutation_principal)],
    container: ContainerDep,
    body: dict | None = None,
) -> dict:
    result = await _call(container, "POST", f"/api/providers/oauth/{provider_id}/start", json_body=body or {}, timeout=60)
    container.audit("provider_oauth_started", actor=principal.username, target=provider_id)
    return result


@router.get("/providers/oauth/{provider_id}/poll/{session_id}")
async def oauth_poll(provider_id: str, session_id: str, principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep) -> dict:
    return await _call(container, "GET", f"/api/providers/oauth/{provider_id}/poll/{session_id}", timeout=60)


@router.post("/providers/oauth/{provider_id}/submit")
async def oauth_submit(provider_id: str, body: dict, principal: Annotated[Principal, Depends(mutation_principal)], container: ContainerDep) -> dict:
    result = await _call(container, "POST", f"/api/providers/oauth/{provider_id}/submit", json_body=body, timeout=120)
    container.audit("provider_oauth_submitted", actor=principal.username, target=provider_id)
    return result


@router.delete("/providers/oauth/{provider_id}")
async def oauth_signout(provider_id: str, principal: Annotated[Principal, Depends(mutation_principal)], container: ContainerDep) -> dict:
    result = await _call(container, "DELETE", f"/api/providers/oauth/{provider_id}", timeout=60)
    container.audit("provider_oauth_removed", actor=principal.username, target=provider_id, level="warning")
    return result


# ------------------------------------------------------------------ policy views
@router.get("/providers/spend-guard")
async def spend_guard(principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep) -> dict:
    """What is configured that *could* cost money — and whether it can actually run."""

    rows = container.secrets.list_rows()
    paid: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        provider_id = _variable_to_provider(row["name"])
        if not provider_id or provider_id in seen:
            continue
        seen.add(provider_id)
        classification = provider_catalog.classify_provider(provider_id)
        if classification["billing"] == provider_catalog.PAID:
            paid.append(
                {
                    "provider": provider_id,
                    "label": f"{classification['label']} (key stored as {row['name']})",
                    "why": (
                        "A key is stored for a paid provider. It is only used if you select it with FREE MODE off or "
                        "unlock paid fallback — nothing activates it on its own."
                    ),
                    "verify": "",
                }
            )
    unlocked = bool(container.db.get_setting("allow_paid_fallback", False))
    active = container.db.get_setting("active_model", {}) or {}
    return {
        "free_mode": container.free_mode,
        "paid_unlocked": unlocked,
        "paid_configured_not_necessarily_active": [item["provider"] for item in paid],
        "could_cost_money": paid,
        "active_route": {
            "provider": active.get("provider", ""),
            "model": active.get("model", ""),
            "billing": active.get("billing", provider_catalog.UNKNOWN),
        },
        "statement": (
            "Nothing here can charge you: FREE MODE is on and no paid provider is unlocked."
            if container.free_mode and not unlocked
            else "Paid providers are reachable with your own keys. Charges would be billed by the provider to your account."
        ),
        "tiers": provider_catalog.tier_legend(),
    }


@router.put("/free-mode/nous-free-tier")
async def set_nous_free_tier(
    body: SetFreeTierBody,
    request: Request,
    principal: Annotated[Principal, Depends(mutation_principal)],
    container: ContainerDep,
) -> dict:
    """Turn the keyless Nous free tier on or off for the runtime.

    The switch is real: it sets ``HERMES_GUEST_ONBOARDING=1`` (and the runtime's
    ``nous.guest`` config) so the agent asks the free tier first. Availability is
    controlled upstream and can change — we say so rather than promising it.
    """

    container.db.set_setting("nous_free_tier_enabled", bool(body.enabled))
    applied: list[str] = [f"setting nous_free_tier_enabled={bool(body.enabled)}"]
    config_note = ""
    try:
        await _call(
            container,
            "PUT",
            "/api/config",
            json_body={
                "config": {
                    "nous": {"guest": bool(body.enabled)},
                    "free_tier": {"prefer": bool(body.enabled)},
                }
            },
            timeout=45,
        )
        applied.append("runtime config nous.guest updated")
        config_note = "The runtime will ask the free tier first on its next request."
    except HTTPException as exc:
        config_note = (
            "The setting is stored; the runtime config could not be updated right now "
            f"({exc.detail}). Start the runtime, then toggle again."
        )
    container.audit("nous_free_tier", actor=principal.username, detail=str(applied), ip=client_ip(request))
    return {
        "ok": True,
        "enabled": bool(body.enabled),
        "applied": applied,
        "note": config_note,
        "honesty": (
            "The Nous free tier needs no API key, but it is a third-party service: its availability, rate limits and "
            "model choice are controlled upstream and can change without notice."
        ),
    }


@router.put("/keys/policy/sync-to-hermes-env")
async def set_env_sync_policy(
    body: SetFreeTierBody,
    request: Request,
    principal: Annotated[Principal, Depends(mutation_principal)],
    container: ContainerDep,
) -> dict:
    """Whether stored keys are mirrored into ``$HERMES_HOME/.env`` (plaintext, 0600)."""

    container.db.set_setting("sync_keys_to_hermes_env", bool(body.enabled))
    container.audit("key_env_policy", actor=principal.username, detail=f"enabled={bool(body.enabled)}", ip=client_ip(request))
    if body.enabled:
        warning = (
            "Keys will be written to $HERMES_HOME/.env so the Hermes CLI can see them. That file is plaintext on disk "
            "(mode 0600) — anyone who can read your account can read the keys."
        )
        container.logs.warning(warning, "SECURITY")
    else:
        warning = "Keys are injected into the runtime environment only; nothing is written to disk in plaintext."
    return {"ok": True, "enabled": bool(body.enabled), "warning": warning}
