"""Model manager: catalogue, default selection, fallback chain and free-first policy.

Everything routes through the real Hermes API:

* ``GET  /api/model/options`` — providers/models the runtime can serve now.
* ``POST /api/model/set``     — switch the active model (``scope`` required).
* ``GET  /api/model/info``    — what is active, plus context/limits.
* ``PUT  /api/config``        — the ``fallback_providers`` chain.

The free-first policy is enforced here, in code, not just in the UI:

* The fallback chain is stored with an explicit ``allow_paid`` flag.
* :func:`apply_fallback_chain` refuses to write a paid provider into the chain
  unless the operator has turned paid fallback on. A paid model is never
  activated silently — not by a retry, not by a fallback, not by a default.
"""

from __future__ import annotations

from typing import Any

from ..db import Database
from .client import HermesAPIError, HermesClient, HermesUnavailable
from .providers import classify_model_entry, fetch_current_model, fetch_model_options, provider_is_local

PAID_GATE_KEY = "allow_paid_fallback"
FREE_MODE_KEY = "free_mode"
CHAIN_KEY = "fallback_chain"


class PaidFallbackBlocked(RuntimeError):
    """Raised when a caller tries to put a paid model into the fallback chain."""


async def list_models(client: HermesClient, db: Database) -> dict:
    options = await fetch_model_options(client)
    current = await fetch_current_model(client)
    allow_paid = bool(db.get_setting(PAID_GATE_KEY, False))

    providers = []
    flat: list[dict] = []
    for entry in options.get("providers", []) or []:
        if not isinstance(entry, dict):
            continue
        provider_id = str(entry.get("name") or entry.get("slug") or "")
        if not provider_id:
            continue
        meta = classify_model_entry(provider_id, entry)
        models = []
        for model in entry.get("models") or []:
            if not isinstance(model, str):
                continue
            models.append(
                {
                    "id": model,
                    "provider": provider_id,
                    "provider_label": entry.get("display_name") or provider_id,
                    "billing": meta["billing"],
                    "cost_label": meta["label"],
                    "free": meta["free"],
                    "local": meta["local"],
                    "notes": meta["notes"],
                    "verify_url": meta["verify_url"],
                    "as_of": meta["as_of"],
                    "capabilities": (entry.get("capabilities") or {}).get(model) or {},
                    "is_current": bool(entry.get("is_current")) and current.get("model") == model,
                }
            )
        flat.extend(models)
        providers.append(
            {
                "id": provider_id,
                "label": entry.get("display_name") or provider_id,
                "authenticated": bool(entry.get("authenticated")),
                "is_current": bool(entry.get("is_current")),
                "model_count": len(models),
                "billing": meta["billing"],
                "cost_label": meta["label"],
                "local": meta["local"],
                "models": models,
            }
        )

    free_models = [m for m in flat if m["free"]]
    paid_models = [m for m in flat if not m["free"]]
    return {
        "providers": providers,
        "models": flat,
        "summary": {
            "total": len(flat),
            "free": len(free_models),
            "paid": len(paid_models),
            "free_local": len([m for m in flat if m["billing"] == "free_local"]),
            "free_tier": len([m for m in flat if m["billing"] == "free_tier"]),
        },
        "current": {
            "provider": current.get("provider") or options.get("provider") or "",
            "model": current.get("model") or options.get("model") or "",
            "info": current,
        },
        "policy": {
            "allow_paid_fallback": allow_paid,
            "free_mode": bool(db.get_setting(FREE_MODE_KEY, True)),
        },
        "chain": db.get_setting(CHAIN_KEY, []),
    }


async def set_model(
    client: HermesClient,
    db: Database,
    *,
    provider: str,
    model: str,
    scope: str = "global",
    base_url: str = "",
    api_key: str = "",
    reasoning_effort: str | None = None,
    confirm_expensive: bool = False,
    allow_paid: bool | None = None,
) -> dict:
    """Switch the active model. Paid models require an explicit confirmation."""
    entry = (await fetch_model_options(client)).get("providers", []) or []
    payload_entry = next(
        (
            e
            for e in entry
            if isinstance(e, dict) and provider in {str(e.get("name") or ""), str(e.get("slug") or "")}
        ),
        None,
    )
    meta = classify_model_entry(provider, payload_entry)
    paid = meta["billing"] == "paid"
    effective_allow = bool(db.get_setting(PAID_GATE_KEY, False)) if allow_paid is None else allow_paid
    if paid and not (effective_allow and confirm_expensive):
        raise PaidFallbackBlocked(
            f"{provider} is a PAID provider. Turn on 'Allow paid models' in Free Mode settings and "
            "confirm the switch before using it — the Control Center will not enable paid inference "
            "on its own."
        )
    body = {
        "scope": scope,
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "reasoning_effort": reasoning_effort,
        "confirm_expensive_model": bool(confirm_expensive or paid),
    }
    result = await client.post("/api/model/set", body, timeout=90)
    return {"ok": True, "result": result, "classification": meta}


async def get_model_info(client: HermesClient) -> dict:
    return await fetch_current_model(client)


def _provider_is_paid(provider_id: str) -> bool:
    return classify_model_entry(provider_id, None)["billing"] == "paid"


def _provider_is_free(provider_id: str) -> bool:
    meta = classify_model_entry(provider_id, None)
    return meta["billing"] in {"free_local", "free_tier"}


async def apply_fallback_chain(client: HermesClient, db: Database, chain: list[dict]) -> dict:
    """Write the free-first fallback chain into Hermes' own config.

    ``chain`` is a list of ``{"provider": ..., "model": ...}`` entries ordered
    primary → secondary → … . Unknown providers are rejected; paid providers are
    rejected unless the operator enabled paid fallback.
    """
    allow_paid = bool(db.get_setting(PAID_GATE_KEY, False))
    cleaned: list[dict[str, str]] = []
    rejected: list[str] = []
    for item in chain or []:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or "").strip()
        model = str(item.get("model") or "").strip()
        if not provider or not model:
            continue
        if _provider_is_paid(provider) and not allow_paid:
            rejected.append(f"{provider}/{model} (paid — blocked by Free Mode)")
            continue
        cleaned.append({"provider": provider, "model": model})

    current = await fetch_current_model(client)
    existing_config = await _read_config(client)
    config = dict(existing_config or {})
    config["fallback_providers"] = cleaned
    await client.put("/api/config", {"config": config}, timeout=60)
    db.set_setting(CHAIN_KEY, cleaned)
    return {
        "ok": True,
        "chain": cleaned,
        "rejected": rejected,
        "allow_paid": allow_paid,
        "current": current,
    }


async def _read_config(client: HermesClient) -> dict:
    try:
        payload = await client.get("/api/config", timeout=30)
        return payload if isinstance(payload, dict) else {}
    except (HermesAPIError, HermesUnavailable):
        return {}


async def build_free_chain(client: HermesClient, db: Database) -> dict:
    """Recommend a free-first chain from what the runtime can actually serve.

    Order: local/free-local endpoints first, then providers with a documented
    free tier that are already configured. Nothing paid is included, ever.
    """
    options = await fetch_model_options(client)
    candidates: list[dict] = []
    for entry in options.get("providers", []) or []:
        if not isinstance(entry, dict):
            continue
        provider_id = str(entry.get("name") or entry.get("slug") or "")
        if not provider_id:
            continue
        meta = classify_model_entry(provider_id, entry)
        models = [m for m in (entry.get("models") or []) if isinstance(m, str)]
        if not models:
            continue
        if meta["billing"] not in {"free_local", "free_tier"}:
            # Paid *and* unverified providers stay out of a chain that the UI
            # presents as "free": we only recommend routes we can vouch for.
            continue
        authenticated = bool(entry.get("authenticated")) or provider_is_local(provider_id)
        if not authenticated:
            continue
        candidates.append(
            {
                "provider": provider_id,
                "model": models[0],
                "billing": meta["billing"],
                "cost_label": meta["label"],
                "local": meta["local"],
                "models_available": len(models),
            }
        )
    order = {"free_local": 0, "free_tier": 1}
    candidates.sort(key=lambda item: order.get(item["billing"], 9))
    candidates = candidates[:4]
    return {
        "recommended": candidates[:4],
        "available": candidates,
        "note": (
            "Free-first ordering: local models first (no third party, no cost), then providers with a "
            "documented free tier that you have already configured. Paid providers are excluded."
        ),
    }


async def test_model(client: HermesClient, provider: str, model: str) -> dict:
    """Report whether the runtime considers this (provider, model) usable.

    This is a *capability* test against Hermes' own model registry — it does not
    perform inference. End-to-end inference testing lives in
    ``/api/cc/chat/test``, which sends a real prompt through the agent bridge.
    """
    options = await fetch_model_options(client)
    for entry in options.get("providers", []) or []:
        if not isinstance(entry, dict):
            continue
        if provider in {str(entry.get("name") or ""), str(entry.get("slug") or "")}:
            models = [m for m in (entry.get("models") or []) if isinstance(m, str)]
            meta = classify_model_entry(provider, entry)
            return {
                "available": model in models,
                "provider_authenticated": bool(entry.get("authenticated")),
                "models_available": len(models),
                "classification": meta,
                "detail": (
                    f"'{model}' is offered by provider '{provider}'."
                    if model in models
                    else f"Provider '{provider}' is configured but does not list '{model}'. "
                    f"Available: {', '.join(models[:8]) or 'none'}"
                ),
            }
    return {
        "available": False,
        "provider_authenticated": False,
        "models_available": 0,
        "classification": classify_model_entry(provider, None),
        "detail": (
            f"Provider '{provider}' is not available in this runtime. Configure credentials first "
            "(Providers → add key) or pick another provider."
        ),
    }


def chain_summary(chain: list[dict[str, Any]]) -> list[dict]:
    """Annotate a chain for display (cost label per hop)."""
    out = []
    for index, item in enumerate(chain or []):
        provider = str(item.get("provider") or "")
        meta = classify_model_entry(provider, None)
        out.append(
            {
                "position": index + 1,
                "provider": provider,
                "model": item.get("model"),
                "billing": meta["billing"],
                "cost_label": meta["label"],
                "local": meta["local"],
            }
        )
    return out
