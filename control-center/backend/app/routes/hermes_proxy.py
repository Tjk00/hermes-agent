"""Allow-listed passthroughs to the real Hermes API.

Everything the Control Center shows about the agent — memory, skills, toolsets,
sessions, cron jobs, environment variables, configuration, the gateway, ops and
analytics — comes from Hermes itself through this table. Two rules keep it honest:

* **an explicit allowlist**, so a crafted request can never reach an upstream
  route we did not consider (and never the dashboard's own file endpoints);
* **policy hooks** on writes, so FREE MODE can refuse a paid model before the
  request goes upstream, and so every write lands in the audit trail.

Reading through the passthrough is safe by construction: responses are redacted of
secret-shaped strings before they leave this process.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..container import Container, get_container
from ..deps import ContainerDep, Principal, current_principal, mutation_principal, require_admin
from ..hermes import provider_catalog
from ..hermes.client import HermesAPIError, HermesUnavailable
from ..security import get_redactor

router = APIRouter(tags=["hermes"])

# (public path, upstream path, timeout seconds)
READ_ROUTES: list[tuple[str, str, float]] = [
    ("/memory", "/api/memory", 90),
    ("/memory/providers/{name}/config", "/api/memory/providers/{name}/config", 60),
    ("/skills", "/api/skills", 90),
    ("/skills/content", "/api/skills/content", 60),
    ("/skills/hub/search", "/api/skills/hub/search", 90),
    ("/skills/hub/official", "/api/skills/hub/official", 60),
    ("/tools/toolsets", "/api/tools/toolsets", 90),
    ("/tools/toolsets/{name}/config", "/api/tools/toolsets/{name}/config", 60),
    ("/tools/toolsets/{name}/models", "/api/tools/toolsets/{name}/models", 60),
    ("/tools/terminal/backends", "/api/tools/terminal/backends", 60),
    ("/tools/computer-use/status", "/api/tools/computer-use/status", 60),
    ("/sessions", "/api/sessions", 90),
    ("/sessions/stats", "/api/sessions/stats", 60),
    ("/sessions/search", "/api/sessions/search", 90),
    ("/sessions/{session_id}", "/api/sessions/{session_id}", 60),
    ("/sessions/{session_id}/messages", "/api/sessions/{session_id}/messages", 90),
    ("/sessions/{session_id}/export", "/api/sessions/{session_id}/export", 90),
    ("/schedules", "/api/cron/jobs", 60),
    ("/schedules/blueprints", "/api/cron/blueprints", 60),
    ("/schedules/delivery-targets", "/api/cron/delivery-targets", 60),
    ("/schedules/{job_id}", "/api/cron/jobs/{job_id}", 60),
    ("/schedules/{job_id}/runs", "/api/cron/jobs/{job_id}/runs", 60),
    ("/env", "/api/env", 60),
    ("/config", "/api/config", 60),
    ("/config/raw", "/api/config/raw", 60),
    ("/analytics/models", "/api/analytics/models", 90),
    ("/analytics/usage", "/api/analytics/usage", 90),
    ("/messaging/platforms", "/api/messaging/platforms", 90),
    ("/local-models/status", "/api/local-models/status", 60),
    ("/local-models/hardware", "/api/local-models/hardware", 60),
    ("/local-models/catalog", "/api/local-models/catalog", 90),
    ("/local-models/search", "/api/local-models/search", 90),
    ("/local-models/jobs", "/api/local-models/jobs", 60),
    ("/local-models/jobs/{job_id}", "/api/local-models/jobs/{job_id}", 60),
    ("/ops/checkpoints", "/api/ops/checkpoints", 60),
    ("/update/status", "/api/hermes/update/check", 60),
    ("/gateway/status", "/api/status", 30),
]

WRITE_ROUTES: list[tuple[str, str, str, float]] = [
    # (method, public path, upstream path, timeout)
    ("PUT", "/memory/provider", "/api/memory/provider", 60),
    ("POST", "/memory/reset", "/api/memory/reset", 120),
    ("PUT", "/memory/providers/{name}/config", "/api/memory/providers/{name}/config", 60),
    ("POST", "/memory/providers/{name}/setup", "/api/memory/providers/{name}/setup", 180),
    ("POST", "/memory/providers/{provider}/oauth/start", "/api/memory/providers/{provider}/oauth/start", 60),
    ("POST", "/skills", "/api/skills", 60),
    ("PUT", "/skills/content", "/api/skills/content", 60),
    ("PUT", "/skills/toggle", "/api/skills/toggle", 60),
    ("POST", "/skills/hub/install", "/api/skills/hub/install", 180),
    ("POST", "/skills/hub/uninstall", "/api/skills/hub/uninstall", 120),
    ("POST", "/skills/hub/update", "/api/skills/hub/update", 180),
    # Upstream owns updating itself; the Control Center only asks it to.
    ("POST", "/update/apply", "/api/hermes/update", 300),
    ("PUT", "/tools/toolsets/{name}", "/api/tools/toolsets/{name}", 60),
    ("PUT", "/tools/toolsets/{name}/model", "/api/tools/toolsets/{name}/model", 60),
    ("PUT", "/tools/toolsets/{name}/provider", "/api/tools/toolsets/{name}/provider", 60),
    ("PUT", "/tools/toolsets/{name}/env", "/api/tools/toolsets/{name}/env", 60),
    ("PUT", "/tools/terminal/backend", "/api/tools/terminal/backend", 60),
    ("POST", "/tools/computer-use/permissions/grant", "/api/tools/computer-use/permissions/grant", 60),
    ("POST", "/schedules", "/api/cron/jobs", 60),
    ("PUT", "/schedules/{job_id}", "/api/cron/jobs/{job_id}", 60),
    ("DELETE", "/schedules/{job_id}", "/api/cron/jobs/{job_id}", 60),
    ("POST", "/schedules/{job_id}/pause", "/api/cron/jobs/{job_id}/pause", 60),
    ("POST", "/schedules/{job_id}/resume", "/api/cron/jobs/{job_id}/resume", 60),
    ("POST", "/schedules/{job_id}/trigger", "/api/cron/jobs/{job_id}/trigger", 180),
    ("DELETE", "/sessions/{session_id}", "/api/sessions/{session_id}", 60),
    ("POST", "/sessions/prune", "/api/sessions/prune", 180),
    ("POST", "/sessions/import", "/api/sessions/import", 180),
    ("PUT", "/messaging/platforms/{platform_id}", "/api/messaging/platforms/{platform_id}", 60),
    ("POST", "/messaging/platforms/{platform_id}/test", "/api/messaging/platforms/{platform_id}/test", 120),
    ("POST", "/local-models/quickstart", "/api/local-models/quickstart", 120),
    ("POST", "/local-models/runtime/install", "/api/local-models/runtime/install", 900),
    ("POST", "/local-models/download", "/api/local-models/download", 120),
    ("POST", "/local-models/activate", "/api/local-models/activate", 120),
    ("POST", "/local-models/server", "/api/local-models/server", 120),
    ("POST", "/local-models/eject", "/api/local-models/eject", 60),
    ("DELETE", "/local-models/models/{model_id}", "/api/local-models/models/{model_id}", 60),
    ("POST", "/gateway/start", "/api/gateway/start", 180),
    ("POST", "/gateway/stop", "/api/gateway/stop", 180),
    ("POST", "/gateway/restart", "/api/gateway/restart", 240),
    ("POST", "/gateway/drain", "/api/gateway/drain", 180),
    ("POST", "/ops/backup", "/api/ops/backup", 600),
    ("POST", "/ops/doctor", "/api/ops/doctor", 300),
    ("POST", "/ops/security-audit", "/api/ops/security-audit", 300),
    ("POST", "/ops/dump", "/api/ops/dump", 300),
    ("POST", "/ops/prompt-size", "/api/ops/prompt-size", 120),
    ("POST", "/ops/config-migrate", "/api/ops/config-migrate", 300),
    ("PUT", "/config", "/api/config", 120),
    ("PUT", "/config/raw", "/api/config/raw", 120),
]


async def call(container: Container, method: str, path: str, **kwargs: Any) -> Any:
    client = container.supervisor.client()
    try:
        result = await client.request(method, path, **kwargs)
    except HermesAPIError as exc:
        raise HTTPException(status_code=exc.status if 400 <= exc.status < 600 else 502, detail=exc.human_detail()) from exc
    except HermesUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _redact(result)


def _redact(payload: Any) -> Any:
    redactor = get_redactor()
    if isinstance(payload, dict):
        out = {}
        for key, value in payload.items():
            if isinstance(value, str):
                out[key] = redactor.redact(value)
            else:
                out[key] = _redact(value)
        return out
    if isinstance(payload, list):
        return [_redact(item) for item in payload]
    return payload


def _register_reads() -> None:
    for public_path, upstream, timeout in READ_ROUTES:
        async def handler(
            request: Request,
            c: Annotated[Container, Depends(get_container)],
            principal: Annotated[Principal, Depends(current_principal)],
            _upstream: str = upstream,
            _timeout: float = timeout,
        ) -> Any:
            path = _upstream
            for name, value in request.path_params.items():
                path = path.replace(f"{{{name}}}", str(value))
            params = dict(request.query_params)
            return await call(c, "GET", path, params=params, timeout=_timeout)

        handler.__name__ = "read_" + public_path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
        router.add_api_route(public_path, handler, methods=["GET"], name=handler.__name__)


_register_reads()


def _register_writes() -> None:
    for method, public_path, upstream, timeout in WRITE_ROUTES:
        async def handler(
            request: Request,
            c: Annotated[Container, Depends(get_container)],
            principal: Annotated[Principal, Depends(mutation_principal)],
            _upstream: str = upstream,
            _timeout: float = timeout,
            _method: str = method,
        ) -> Any:
            path = _upstream
            for name, value in request.path_params.items():
                path = path.replace(f"{{{name}}}", str(value))
            body: Any = None
            if _method in {"POST", "PUT", "PATCH"}:
                try:
                    body = await request.json()
                except Exception:  # noqa: BLE001 - empty bodies are legitimate
                    body = {}
            guard(c, _upstream, body, principal)
            result = await call(c, _method, path, json_body=body, params=dict(request.query_params), timeout=_timeout)
            c.audit(f"hermes_{_method.lower()}", actor=principal.username, target=_upstream)
            return result

        handler.__name__ = "write_" + method.lower() + "_" + public_path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
        router.add_api_route(public_path, handler, methods=[method], name=handler.__name__)


def guard(container: Container, upstream: str, body: Any, principal: Principal) -> None:
    """FREE MODE enforcement on anything that can route traffic to a paid provider."""

    if not isinstance(body, dict):
        return
    provider = str(body.get("provider") or "")
    model = str(body.get("model") or "")
    if not provider and upstream.endswith("/api/cron/jobs") and (body.get("provider") or body.get("model")):
        provider = str(body.get("provider") or "")
        model = str(body.get("model") or "")
    if not provider and not model:
        return
    classification = provider_catalog.classify_model(provider, model)
    if classification["billing"] == provider_catalog.PAID:
        container.guard_paid(provider=provider, model=model, actor=principal.username, action=f"send work to it ({upstream})")


_register_writes()


class EnvBody(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    value: str = Field(default="", max_length=16384)
    provider_setup: bool = False


@router.put("/env")
async def put_env(
    body: EnvBody,
    principal: Annotated[Principal, Depends(mutation_principal)],
    container: ContainerDep,
) -> Any:
    result = await call(
        container,
        "PUT",
        "/api/env",
        json_body={"key": body.key, "value": body.value, "api_key": body.value, "provider_setup": body.provider_setup},
        timeout=60,
    )
    container.audit("env_written", actor=principal.username, target=body.key, level="warning")
    container.logs.provider(f"Environment variable {body.key} written through Hermes.", level="WARNING")
    return result


@router.delete("/env")
async def delete_env(
    principal: Annotated[Principal, Depends(mutation_principal)],
    container: ContainerDep,
    key: str = Query(..., min_length=1, max_length=128),
) -> Any:
    result = await call(container, "DELETE", "/api/env", params={"key": key}, timeout=60)
    container.audit("env_deleted", actor=principal.username, target=key, level="warning")
    return result


@router.post("/memory/providers/{name}/oauth/status")
async def memory_oauth_status(name: str, principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep) -> Any:
    return await call(container, "GET", f"/api/memory/providers/{name}/oauth/status", timeout=60)
