"""Model manager: what is available, what it costs, and the free-first chain.

Selection always goes through the same two gates:

1. **FREE MODE / paid lock** — a paid provider is refused unless the operator
   explicitly unlocked paid usage (the check lives in ``Container.guard_paid``).
2. **The real upstream API** — the assignment is applied with
   ``POST /api/model/set`` (``scope='main'``), exactly like the Hermes dashboard
   does, and re-read from ``/api/model/info`` so the UI shows upstream's answer
   rather than our intention.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..container import Container
from ..db import utcnow
from ..deps import ContainerDep, Principal, mutation_principal, require_admin
from ..hermes import provider_catalog
from ..hermes.client import HermesAPIError, HermesUnavailable

router = APIRouter(tags=["models"])

CHAIN_SETTING = "fallback_chain"


class SelectBody(BaseModel):
    provider: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=200)
    scope: str = Field(default="main", max_length=40)
    base_url: str = Field(default="", max_length=400)
    confirm_expensive: bool = False


class ChainEntry(BaseModel):
    provider: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=200)


class ChainBody(BaseModel):
    """The fallback chain, most preferred first.

    ``entries`` is required (an empty list clears the chain) so a client that sends
    the wrong field name gets a 422 naming the field, instead of a cheerful "saved"
    that quietly stored nothing.
    """

    entries: list[ChainEntry] = Field(max_length=8)


async def _call(container: Container, method: str, path: str, **kwargs: Any) -> Any:
    client = container.supervisor.client()
    try:
        return await client.request(method, path, **kwargs)
    except HermesAPIError as exc:
        raise HTTPException(status_code=exc.status if 400 <= exc.status < 600 else 502, detail=exc.human_detail()) from exc
    except HermesUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def snapshot(container: Container) -> dict:
    """Everything the model page needs, in one place."""

    client = container.supervisor.client()
    options: dict = {"providers": [], "model": "", "provider": ""}
    info: dict = {}
    errors: list[str] = []
    try:
        options = await client.get("/api/model/options", timeout=30) or options
    except (HermesAPIError, HermesUnavailable) as exc:
        errors.append(exc.human_detail() if isinstance(exc, HermesAPIError) else str(exc))
    try:
        info = await client.get("/api/model/info", timeout=20) or {}
    except (HermesAPIError, HermesUnavailable) as exc:
        errors.append(exc.human_detail() if isinstance(exc, HermesAPIError) else str(exc))

    current_provider = str(info.get("provider") or options.get("provider") or "")
    current_model = str(info.get("model") or options.get("model") or "")

    providers: list[dict] = []
    for entry in options.get("providers", []) if isinstance(options, dict) else []:
        raw_slug = str(entry.get("slug") or entry.get("name") or "")
        classification = provider_catalog.classify_provider(raw_slug)
        models = [
            {
                "id": model,
                "classification": provider_catalog.classify_model(raw_slug, model),
                "is_current": model == current_model and provider_catalog.normalize_id(raw_slug) == provider_catalog.normalize_id(current_provider),
            }
            for model in (entry.get("models") or [])
        ]
        providers.append(
            {
                **classification,
                "slug": raw_slug,
                "upstream_name": entry.get("name") or raw_slug,
                "authenticated": bool(entry.get("authenticated")),
                "is_current": bool(entry.get("is_current"))
                or provider_catalog.normalize_id(raw_slug) == provider_catalog.normalize_id(current_provider),
                "models": models,
                "model_count": len(models),
                "source": entry.get("source") or "hermes",
                "blocked_by_free_mode": classification["billing"] == provider_catalog.PAID and not container.paid_unlocked,
            }
        )

    providers.sort(key=lambda item: (0 if item["is_current"] else 1, not item["authenticated"], item["label"].lower()))
    classified_current = provider_catalog.classify_model(current_provider, current_model) if current_provider or current_model else None
    chain = container.db.get_setting(CHAIN_SETTING, []) or []
    return {
        "current": {
            "provider": current_provider,
            "model": current_model,
            "classification": classified_current,
            "context_length": info.get("effective_context_length") or info.get("config_context_length") or 0,
            "capabilities": info.get("capabilities") or {},
            "info": info,
        },
        "providers": providers,
        "chain": chain,
        "policy": {
            "free_mode": container.free_mode,
            "paid_unlocked": container.paid_unlocked,
            "free_billing_values": [provider_catalog.FREE_LOCAL, provider_catalog.FREE_TIER],
        },
        "errors": errors,
        "tiers": provider_catalog.cost_tiers(),
    }


@router.get("/models")
async def list_models(principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep) -> dict:
    return await snapshot(container)


@router.post("/models/select")
async def select_model(body: SelectBody, principal: Annotated[Principal, Depends(mutation_principal)], container: ContainerDep) -> dict:
    classification = provider_catalog.classify_model(body.provider, body.model, base_url=body.base_url or None)
    if classification["billing"] == provider_catalog.PAID:
        container.guard_paid(provider=body.provider, model=body.model, actor=principal.username, action="route model traffic to it")
        if not body.confirm_expensive:
            raise HTTPException(
                status_code=428,
                detail=(
                    "This is a paid model. Paid usage is unlocked, but the selection still needs explicit "
                    "confirmation (confirm_expensive=true), because requests will be billed to your provider account."
                ),
            )
    payload = {
        "scope": body.scope or "main",
        "provider": body.provider,
        "model": body.model,
        "confirm_expensive_model": bool(body.confirm_expensive),
    }
    if body.base_url:
        payload["base_url"] = body.base_url
    result = await _call(container, "POST", "/api/model/set", json_body=payload, timeout=90)
    container.db.set_setting(
        "active_model",
        {
            "provider": provider_catalog.normalize_id(body.provider),
            "provider_label": classification["label"],
            "model": body.model,
            "billing": classification["billing"],
            "cost_label": classification["cost_label"],
            "context_length": (
                (result or {}).get("effective_context_length")
                if isinstance(result, dict)
                else None
            ),
            "changed_at": utcnow(),
            "changed_by": principal.username,
        },
    )
    container.logs.model(
        f"Model set to {body.model} on {classification['label']} ({classification['cost_label']}) by {principal.username}.",
        {"provider": body.provider, "model": body.model, "billing": classification["billing"]},
    )
    container.audit("model_selected", actor=principal.username, target=f"{body.provider}/{body.model}", detail=classification["cost_label"])
    return {"ok": True, "result": result, "classification": classification}


@router.post("/models/test")
async def test_model(body: SelectBody, principal: Annotated[Principal, Depends(mutation_principal)], container: ContainerDep) -> dict:
    from ..hermes.providers import test_provider

    result = await test_provider(container, body.provider, model=body.model)
    container.audit("model_tested", actor=principal.username, target=f"{body.provider}/{body.model}", detail=f"ok={result.get('ok')}")
    return result


@router.get("/models/recommended")
async def recommended(principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep) -> dict:
    """Free-first recommendations, built from what is actually usable right now."""

    data = await snapshot(container)
    recommendations: list[dict] = []

    for provider in data["providers"]:
        if not provider["authenticated"]:
            continue
        if provider["billing"] not in {provider_catalog.FREE_LOCAL, provider_catalog.FREE_TIER, provider_catalog.MIXED}:
            continue
        free_models = [
            item for item in provider["models"] if provider_catalog.is_free_billing(item["classification"]["billing"])
        ]
        if not free_models:
            free_models = provider["models"][:3] if provider["billing"] == provider_catalog.FREE_LOCAL else []
        if not free_models:
            continue
        recommendations.append(
            {
                "provider": provider["id"],
                "provider_label": provider["label"],
                "billing": provider["billing"],
                "cost_label": provider["cost_label"],
                "models": [item["id"] for item in free_models[:6]],
                "why": (
                    "Runs on this machine: no account, no key, no per-request cost."
                    if provider["billing"] == provider_catalog.FREE_LOCAL
                    else f"{provider['cost_label']} — free within the provider's limits, no card required."
                ),
            }
        )

    recommendations.sort(key=lambda item: 0 if item["billing"] == provider_catalog.FREE_LOCAL else 1)
    return {
        "recommendations": recommendations,
        "capability": container.db.get_setting("local_capability_summary", {}),
        "statement": (
            "These are the free routes this install can use right now. Nothing here can bill you."
            if recommendations
            else "No free route is configured yet. The cheapest honest options are a local model (if this host can "
            "run one) or a provider free tier with your own free key."
        ),
    }


@router.get("/models/chain")
async def get_chain(principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep) -> dict:
    chain = container.db.get_setting(CHAIN_SETTING, []) or []
    return {
        "chain": chain,
        "paid_unlocked": container.paid_unlocked,
        "rules": [
            "Primary is always a free route (local model or a provider free tier).",
            "Secondary is a second free route — a different provider, so one outage does not stop you.",
            "A paid entry is optional, never activated automatically, and requires the explicit unlock.",
        ],
    }


@router.put("/models/chain")
async def set_chain(body: ChainBody, principal: Annotated[Principal, Depends(mutation_principal)], container: ContainerDep) -> dict:
    cleaned: list[dict] = []
    seen_free = 0
    for index, entry in enumerate(body.entries):
        provider = str(entry.provider or "")
        model = str(entry.model or "")
        if not provider or not model:
            raise HTTPException(status_code=400, detail=f"Chain entry {index + 1} needs both provider and model.")
        classification = provider_catalog.classify_model(provider, model)
        paid = classification["billing"] == provider_catalog.PAID
        if paid:
            if not container.paid_unlocked:
                container.guard_paid(provider=provider, model=model, actor=principal.username, action="add it to the fallback chain")
            if index < 2:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "The first two chain entries must be free routes. A paid provider can only be the last "
                        "resort, and only after unlocking paid usage."
                    ),
                )
        else:
            seen_free += 1
        cleaned.append(
            {
                "provider": provider,
                "provider_label": classification["label"],
                "model": model,
                "billing": classification["billing"],
                "cost_label": classification["cost_label"],
                "auto_activate": False if paid else True,
                "position": index + 1,
            }
        )
    container.db.set_setting(CHAIN_SETTING, cleaned)
    container.audit("fallback_chain_updated", actor=principal.username, detail=str([f"{e['provider']}/{e['model']}" for e in cleaned])[:400])
    return {
        "ok": True,
        "chain": cleaned,
        "free_entries": seen_free,
        "note": (
            "Hermes itself routes with its own provider configuration; this chain is the Control Center's policy for "
            "what it will select and in which order. Nothing paid is activated without the unlock."
        ),
    }


@router.post("/models/free-chain/apply")
async def apply_free_chain(principal: Annotated[Principal, Depends(mutation_principal)], container: ContainerDep) -> dict:
    """Picks the best free route available and selects it for real."""

    data = await snapshot(container)
    free = [
        provider
        for provider in data["providers"]
        if provider["authenticated"]
        and provider["billing"] in {provider_catalog.FREE_LOCAL, provider_catalog.FREE_TIER}
        and provider["models"]
    ]
    if not free:
        raise HTTPException(
            status_code=409,
            detail=(
                "No free route is ready: no local model is running and no provider with a free tier has usable "
                "credentials. The app will not fall back to a paid provider on its own."
            ),
        )
    free.sort(key=lambda item: 0 if item["billing"] == provider_catalog.FREE_LOCAL else 1)
    chosen = free[0]
    model = chosen["models"][0]["id"]
    result = await _call(
        container,
        "POST",
        "/api/model/set",
        json_body={"scope": "main", "provider": chosen["slug"], "model": model, "confirm_expensive_model": False},
        timeout=90,
    )
    container.db.set_setting(
        "active_model",
        {
            "provider": chosen["id"],
            "provider_label": chosen["label"],
            "model": model,
            "billing": chosen["billing"],
            "cost_label": chosen["cost_label"],
            "changed_at": utcnow(),
            "changed_by": principal.username,
        },
    )
    container.logs.model(f"Free-first selection applied: {model} on {chosen['label']}.", {"provider": chosen["id"]})
    container.audit("free_chain_applied", actor=principal.username, target=f"{chosen['id']}/{model}")
    return {"ok": True, "applied": {"provider": chosen["id"], "model": model, "billing": chosen["billing"]}, "result": result}


@router.get("/models/auxiliary")
async def auxiliary(principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep, refresh: bool = Query(False)) -> dict:
    return await _call(container, "GET", "/api/model/auxiliary", timeout=30)
