"""Chat with the real agent, plus the bridge that makes it possible.

How the chat works
------------------
Hermes is an *agent*, not a text-completion endpoint, so the Control Center talks
to the OpenAI-compatible surface of its gateway (``platforms.api_server``) — the
same surface the Hermes API documents:

1. ``POST /v1/runs``           start a run with the conversation, get a ``run_id``
2. ``GET  /v1/runs/{id}/events`` stream lifecycle events (deltas, tool calls,
                                approvals, completion)
3. ``POST /v1/runs/{id}/stop``  stop it server-side — you can close the tab and
                                the run really stops
4. ``POST /v1/runs/{id}/approval`` answer a tool-approval prompt (so you can
                                approve a shell command from your phone)

If a build predates ``/v1/runs``, we fall back to streaming
``/v1/chat/completions``; the UI then reports "stop" as a client-side cancel,
which is exactly what it is. Nothing here invents agent output: every token the
browser sees came from Hermes.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Annotated, Any, AsyncIterator

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..container import Container
from ..db import utcnow
from ..deps import ContainerDep, Principal, enforce_rate_limit, mutation_principal, require_admin
from ..errors import classify, human_error
from ..hermes import provider_catalog
from ..hermes.client import HermesAPIError, HermesUnavailable
from ..hermes.gateway import _port_owner
from ..security import new_token

router = APIRouter(prefix="/chat", tags=["chat"])

BRIDGE_KEY_NAME = "API_SERVER_KEY"
MAX_HISTORY_MESSAGES = 40


# --------------------------------------------------------------------- models
class ThreadBody(BaseModel):
    title: str = Field(default="", max_length=200)
    model: str = Field(default="", max_length=200)
    provider: str = Field(default="", max_length=120)


class MessageBody(BaseModel):
    content: str = Field(min_length=1, max_length=200_000)
    model: str = Field(default="", max_length=200)
    provider: str = Field(default="", max_length=120)


class ApprovalBody(BaseModel):
    choice: str = Field(default="deny", max_length=32)
    request_id: str = Field(default="", max_length=120)


class RenameBody(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    archived: bool = False


# --------------------------------------------------------------------- bridge
async def bridge_config(container: Container) -> dict:
    """What the upstream config says about the local API server platform."""

    client = container.supervisor.client()
    try:
        config = await client.get("/api/config", timeout=30)
    except (HermesAPIError, HermesUnavailable) as exc:
        return {"configured": False, "error": _detail(exc), "enabled": False}
    platforms = (config or {}).get("platforms") if isinstance(config, dict) else None
    api_server = (platforms or {}).get("api_server") if isinstance(platforms, dict) else None
    extra = (api_server or {}).get("extra") or {}
    return {
        "configured": bool(api_server),
        "enabled": bool((api_server or {}).get("enabled")),
        "host": str(extra.get("host") or "127.0.0.1"),
        "port": int(extra.get("port") or container.settings.chat_bridge_port),
        "key_set_in_runtime": bool((extra.get("key") or "")),
        "raw": api_server or {},
    }


async def bridge_health(container: Container, config: dict | None = None) -> dict:
    config = config or await bridge_config(container)
    port = config.get("port") or container.settings.chat_bridge_port
    host = config.get("host") or "127.0.0.1"
    base_url = f"http://{host if host not in {'0.0.0.0', '::'} else '127.0.0.1'}:{port}"
    key = container.secrets.reveal_safe(BRIDGE_KEY_NAME)
    supervised_port = int(container.settings.chat_bridge_port)
    result: dict[str, Any] = {
        "base_url": base_url,
        "configured": config.get("configured", False),
        "enabled": config.get("enabled", False),
        "key_present": bool(key),
        "reachable": False,
        "reason": "",
        "models": [],
        "supervised_port": supervised_port,
        "port_aligned": int(port) == supervised_port,
    }
    if not result["port_aligned"]:
        result["reason"] = (
            f"Hermes is configured to publish its API server on port {port}, but the Control Center "
            f"supervises port {supervised_port} — something changed the config outside the Control "
            "Center. Press Enable bridge to write one consistent configuration."
        )
        return result
    if not config.get("enabled"):
        result["reason"] = "The local agent bridge (Hermes' OpenAI-compatible API server) is not enabled."
        return result
    if not key:
        result["reason"] = (
            "No API_SERVER_KEY is stored, so the bridge cannot authenticate this Control Center. "
            "Enable the bridge again to generate one."
        )
        return result
    headers = {"Authorization": f"Bearer {key}"}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(f"{base_url}/v1/models", headers=headers)
    except httpx.HTTPError as exc:
        result["reason"] = f"{base_url} did not answer ({exc.__class__.__name__}). Is the gateway running?"
        return result
    if response.status_code == 401:
        result["reason"] = "The bridge rejected the stored key. Re-enable the bridge to rotate it."
        return result
    if response.status_code >= 400:
        result["reason"] = f"The bridge answered HTTP {response.status_code}."
        return result
    result["reachable"] = True
    result["reason"] = "The bridge is up and authenticated."
    try:
        payload = response.json()
        result["models"] = [item.get("id") for item in (payload.get("data") or []) if item.get("id")]
    except ValueError:
        pass
    return result


@router.get("/bridge")
async def bridge_status(principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep) -> dict:
    config = await bridge_config(container)
    health = await bridge_health(container, config)
    upstream = {}
    try:
        upstream = await container.supervisor.client().get("/api/status", timeout=20)
    except (HermesAPIError, HermesUnavailable):
        upstream = {}
    return {
        "config": config,
        "health": health,
        "gateway": {
            "running": bool(upstream.get("gateway_running")),
            "state": upstream.get("gateway_state"),
            "platforms": upstream.get("gateway_platforms") or {},
        },
        "supervisor": container.gateway.status(),
        "key": {
            "name": BRIDGE_KEY_NAME,
            "in_vault": container.secrets.has(BRIDGE_KEY_NAME),
            "masked": container.secrets.masked(BRIDGE_KEY_NAME),
        },
        "how_it_works": [
            "Hermes' gateway runs an OpenAI-compatible API server on loopback, protected by API_SERVER_KEY.",
            "The Control Center holds that key in its encrypted vault and never exposes it to the browser.",
            "Chat requests go API → gateway → the real agent, with real tools, memory and skills.",
        ],
    }


@router.post("/bridge/enable")
async def bridge_enable(
    principal: Annotated[Principal, Depends(mutation_principal)],
    container: ContainerDep,
    write_env: bool = Query(True, description="mirror the key into $HERMES_HOME/.env so a non-supervised runtime sees it"),
    start_gateway: bool = Query(True),
) -> dict:
    """Turn on the local agent bridge: key, config, gateway, verification."""

    steps: list[dict] = []
    key = container.secrets.reveal_safe(BRIDGE_KEY_NAME) or f"cc_{new_token(24)}"
    if not container.secrets.has(BRIDGE_KEY_NAME):
        try:
            container.secrets.store(BRIDGE_KEY_NAME, key, actor=principal.username)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    steps.append({"step": "key", "ok": True, "detail": f"{BRIDGE_KEY_NAME} stored encrypted (masked {_mask(key)})."})

    host = "127.0.0.1"
    port = container.settings.chat_bridge_port
    payload = {
        "config": {
            "platforms": {
                "api_server": {
                    "enabled": True,
                    "extra": {"host": host, "port": port},
                }
            }
        }
    }
    client = container.supervisor.client()
    try:
        await client.put("/api/config", json_body=payload, timeout=60)
        steps.append({"step": "config", "ok": True, "detail": f"platforms.api_server enabled on {host}:{port}."})
    except (HermesAPIError, HermesUnavailable) as exc:
        raise HTTPException(status_code=502, detail=f"Could not update the Hermes configuration: {_detail(exc)}") from exc

    if write_env:
        client = container.supervisor.client()  # rebuilt: a restart mints a new session token
        try:
            await client.put(
                "/api/env",
                json_body={"key": BRIDGE_KEY_NAME, "value": key, "provider_setup": False},
                timeout=60,
            )
            steps.append({"step": "env", "ok": True, "detail": f"{BRIDGE_KEY_NAME} written to $HERMES_HOME/.env (0600)."})
        except (HermesAPIError, HermesUnavailable) as exc:
            steps.append({"step": "env", "ok": False, "detail": f"Could not write the key to .env: {_detail(exc)}"})

    restart_needed = not container.supervisor.attached
    if container.supervisor.attached:
        steps.append(
            {
                "step": "restart",
                "ok": True,
                "detail": "The backend was not started by the Control Center; the key was written to .env so the next "
                "gateway start picks it up.",
            }
        )
    else:
        try:
            await asyncio.to_thread(container.supervisor.restart)
            steps.append({"step": "restart", "ok": True, "detail": "Runtime restarted so the key is in its environment."})
        except Exception as exc:  # noqa: BLE001
            steps.append({"step": "restart", "ok": False, "detail": f"Restart failed: {exc}"})

    if start_gateway:
        # Wait for the (possibly restarted) runtime to be healthy again, then bring the
        # gateway up — upstream's service manager first, our own supervision if that
        # fails (the normal case in a container).
        for _ in range(20):
            if container.supervisor.status(probe=True).get("healthy"):
                break
            await asyncio.sleep(1.0)

        async def _answered() -> bool:
            """Authenticated reachability — a bare open port is not good enough.

            Another Hermes instance can be squatting the port with a different key;
            that must not be reported as success.
            """

            for _ in range(12):
                probe_health = await bridge_health(container)
                if probe_health.get("reachable"):
                    return True
                await asyncio.sleep(1.0)
            return False

        if await _answered():
            steps.append({"step": "gateway", "ok": True, "detail": "The bridge was already running and answered our key."})
        else:
            client = container.supervisor.client()
            upstream_detail = ""
            try:
                await client.post("/api/gateway/start", json_body={}, timeout=120)
            except (HermesAPIError, HermesUnavailable) as exc:
                upstream_detail = _detail(exc)
            if await _answered():
                steps.append(
                    {
                        "step": "gateway",
                        "ok": True,
                        "detail": "Started through upstream's own service manager (systemd/launchd).",
                    }
                )
            else:
                status = await asyncio.to_thread(container.gateway.start, wait=True, timeout=60)
                # `status` is used below: a restart through upstream can change whether
                # the bridge answers, and the state tells us whether we own the gateway.
                if status.get("reachable") and await _answered():
                    steps.append(
                        {
                            "step": "gateway",
                            "ok": True,
                            "detail": (
                                "Started the gateway as a supervised child process "
                                "(`hermes gateway run --external-supervisor`) because this host has no "
                                "systemd/launchd to hand it to."
                            ),
                        }
                    )
                else:
                    if status.get("state") == "external":
                        # A gateway we do not own already serves this host. Restarting it
                        # through upstream is the sanctioned way to make it pick up the key
                        # and configuration we just wrote.
                        restart_note = ""
                        try:
                            client = container.supervisor.client()
                            await client.post("/api/gateway/restart", json_body={}, timeout=120)
                            await asyncio.sleep(2.0)
                            restart_note = " It was restarted so it reads the new key."
                        except (HermesAPIError, HermesUnavailable) as exc:
                            restart_note = f" Restarting it through upstream failed: {_detail(exc)}"
                            container.logs.warning(f"Gateway restart through upstream failed: {_detail(exc)}", "SYSTEM")
                        answered = await _answered()
                        steps.append(
                            {
                                "step": "gateway",
                                "ok": answered,
                                "detail": (status.get("note") or "Another gateway serves this host.") + restart_note,
                            }
                        )
                        if answered:
                            return _finish_bridge(container, principal, steps, restart_needed)
                    reason = status.get("last_error") or upstream_detail
                    if not status.get("reachable") and _port_owner(container.settings.chat_bridge_port):
                        reason = (
                            f"Port {container.settings.chat_bridge_port} is held by another process "
                            f"({_port_owner(container.settings.chat_bridge_port)}). Another Hermes instance is running with a different "
                            "configuration."
                        )
                    steps.append(
                        {
                            "step": "gateway",
                            "ok": False,
                            "detail": f"The gateway did not come up: {reason or 'unknown reason'}",
                        }
                    )

    return await _finish_bridge(container, principal, steps, restart_needed)


async def _finish_bridge(container: Container, principal: Principal, steps: list[dict], restart_needed: bool) -> dict:
    container.audit("chat_bridge_enabled", actor=principal.username, detail=str([s["step"] for s in steps]))
    container.logs.system("Agent chat bridge enabled (platforms.api_server).")
    await asyncio.sleep(1.0)
    health = await bridge_health(container)
    return {
        "ok": bool(health.get("reachable")),
        "steps": steps,
        "health": health,
        "restart_needed": restart_needed,
        "next": (
            "The bridge is reachable — chat is live."
            if health.get("reachable")
            else "The bridge is configured but not answering yet. Give the gateway a few seconds, then press Refresh; "
            "the gateway state is shown on this card."
        ),
    }


@router.post("/bridge/start")
async def bridge_start(principal: Annotated[Principal, Depends(mutation_principal)], container: ContainerDep) -> dict:
    """Start the gateway that hosts the agent's chat API.

    Upstream's own start button delegates to systemd/launchd. That is the right thing
    on a server and the wrong thing in a container, so we try upstream first and fall
    back to supervising the gateway ourselves — reporting which happened.
    """

    via = "upstream-service-manager"
    detail = ""
    try:
        result = await container.supervisor.client().post("/api/gateway/start", json_body={}, timeout=120)
        detail = str(result)[:400]
    except (HermesAPIError, HermesUnavailable) as exc:
        via = "control-center"
        detail = _detail(exc)
    # Whether or not upstream claimed success, verify the bridge port. If nothing is
    # listening, supervise `hermes gateway run --external-supervisor` ourselves.
    if not container.gateway.probe():
        via = "control-center"
        status = await asyncio.to_thread(container.gateway.start, wait=True, timeout=60)
        detail = status.get("last_error") or "started `hermes gateway run --external-supervisor`"
    status = container.gateway.status()
    container.audit("gateway_started", actor=principal.username, detail=f"via={via}")
    container.logs.system(f"Agent gateway start requested via {via}.")
    return {"ok": bool(status.get("reachable")), "via": via, "detail": detail, "gateway": status, "health": await bridge_health(container)}


@router.post("/bridge/stop")
async def bridge_stop(principal: Annotated[Principal, Depends(mutation_principal)], container: ContainerDep) -> dict:
    our_gateway = container.gateway.running()
    upstream_error = ""
    try:
        await container.supervisor.client().post("/api/gateway/stop", json_body={}, timeout=120)
    except (HermesAPIError, HermesUnavailable) as exc:
        upstream_error = _detail(exc)
    status = await asyncio.to_thread(container.gateway.stop)
    container.audit("gateway_stopped", actor=principal.username, detail=f"supervised={our_gateway}")
    return {
        "ok": True,
        "stopped_our_gateway": our_gateway,
        "upstream_note": upstream_error,
        "gateway": status,
    }


async def bridge_completion(
    container: Container,
    *,
    prompt: str,
    model: str = "hermes-agent",
    timeout: float = 120.0,
    max_tokens: int = 256,
) -> dict:
    """One real, non-streaming agent turn — used by tests and the setup wizard."""

    health = await bridge_health(container)
    if not health.get("reachable"):
        return {"ok": False, "stage": "bridge", "message": health.get("reason")}
    key = container.secrets.reveal_safe(BRIDGE_KEY_NAME)
    payload = {
        "model": model or "hermes-agent",
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    started = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{health['base_url']}/v1/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
            )
    except httpx.HTTPError as exc:
        return {"ok": False, "stage": "transport", "message": f"{exc.__class__.__name__}: {exc}", "health": health}
    latency_ms = round((time.time() - started) * 1000)
    if response.status_code >= 400:
        detail = response.text[:800]
        return {
            "ok": False,
            "stage": "upstream",
            "status": response.status_code,
            "message": detail,
            "human": classify(status=response.status_code, message=detail),
            "latency_ms": latency_ms,
            "health": health,
        }
    payload = response.json()
    choices = payload.get("choices") or []
    text = ""
    if choices:
        message = choices[0].get("message") or {}
        text = str(message.get("content") or "")
    return {
        "ok": True,
        "stage": "complete",
        "model": payload.get("model") or model,
        "text": text,
        "usage": payload.get("usage") or {},
        "latency_ms": latency_ms,
        "session_id": response.headers.get("X-Hermes-Session-Id", ""),
        "raw_meta": {k: v for k, v in payload.items() if k not in {"choices"}},
    }


@router.post("/test")
async def chat_test(
    principal: Annotated[Principal, Depends(mutation_principal)],
    container: ContainerDep,
    body: MessageBody | None = None,
) -> dict:
    from ..hermes.providers import test_provider  # local import: avoids a cycle at import time

    prompt = (body.content if body and body.content else "Reply with the single word: ready")[:500]
    completion = await bridge_completion(container, prompt=prompt, model="hermes-agent", timeout=120.0)
    container.audit("chat_test", actor=principal.username, detail=f"ok={completion.get('ok')}")
    return completion


# -------------------------------------------------------------------- threads
def _thread_row(container: Container, thread_id: str) -> dict:
    row = container.db.query_one("SELECT * FROM chat_threads WHERE thread_id = ?", (thread_id,))
    if not row:
        raise HTTPException(status_code=404, detail="No such conversation.")
    return row


def _thread_payload(row: dict) -> dict:
    meta = {}
    if row.get("meta"):
        try:
            meta = json.loads(row["meta"])
        except (TypeError, ValueError):
            meta = {}
    return {
        "id": row["thread_id"],
        "title": row["title"],
        "model": row["model"] or "",
        "provider": row["provider"] or "",
        "hermes_session_id": row["hermes_session_id"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "archived": bool(row["archived"]),
        "meta": meta,
    }


@router.get("/threads")
async def list_threads(
    principal: Annotated[Principal, Depends(require_admin)],
    container: ContainerDep,
    include_archived: bool = Query(False),
) -> dict:
    rows = container.db.query(
        "SELECT * FROM chat_threads WHERE archived = 0 OR ? = 1 ORDER BY updated_at DESC LIMIT 200",
        (1 if include_archived else 0,),
    )
    threads = []
    for row in rows:
        payload = _thread_payload(row)
        last = container.db.query_one(
            "SELECT content, role, created_at FROM chat_messages WHERE thread_id = ? ORDER BY id DESC LIMIT 1",
            (row["thread_id"],),
        )
        payload["preview"] = (last or {}).get("content", "")[:160]
        payload["message_count"] = (
            container.db.query_one("SELECT COUNT(*) AS n FROM chat_messages WHERE thread_id = ?", (row["thread_id"],)) or {}
        ).get("n", 0)
        threads.append(payload)
    return {"threads": threads, "count": len(threads)}


@router.post("/threads")
async def create_thread(body: ThreadBody, principal: Annotated[Principal, Depends(mutation_principal)], container: ContainerDep) -> dict:
    thread_id = f"th_{new_token(9)}"
    title = body.title.strip() or "New conversation"
    container.db.insert(
        """
        INSERT INTO chat_threads (thread_id, title, model, provider, created_at, updated_at, archived, meta)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (thread_id, title, body.model or None, body.provider or None, utcnow(), utcnow(), json.dumps({})),
    )
    container.audit("chat_thread_created", actor=principal.username, target=thread_id)
    return {"ok": True, "thread": _thread_payload(_thread_row(container, thread_id))}


@router.patch("/threads/{thread_id}")
async def rename_thread(
    thread_id: str,
    body: RenameBody,
    principal: Annotated[Principal, Depends(mutation_principal)],
    container: ContainerDep,
) -> dict:
    _thread_row(container, thread_id)
    container.db.execute(
        "UPDATE chat_threads SET title = ?, archived = ?, updated_at = ? WHERE thread_id = ?",
        (body.title.strip()[:200], 1 if body.archived else 0, utcnow(), thread_id),
    )
    container.audit("chat_thread_renamed", actor=principal.username, target=thread_id)
    return {"ok": True, "thread": _thread_payload(_thread_row(container, thread_id))}


@router.delete("/threads/{thread_id}")
async def delete_thread(
    thread_id: str,
    principal: Annotated[Principal, Depends(mutation_principal)],
    container: ContainerDep,
    delete_hermes_session: bool = Query(False, description="also delete the backing Hermes session (destructive)"),
) -> dict:
    row = _thread_row(container, thread_id)
    container.db.execute("DELETE FROM chat_messages WHERE thread_id = ?", (thread_id,))
    container.db.execute("DELETE FROM chat_threads WHERE thread_id = ?", (thread_id,))
    removed_session = False
    if delete_hermes_session and row["hermes_session_id"]:
        try:
            await container.supervisor.client().delete(f"/api/sessions/{row['hermes_session_id']}", timeout=30)
            removed_session = True
        except (HermesAPIError, HermesUnavailable) as exc:
            container.logs.warning(f"Hermes session {row['hermes_session_id']} could not be deleted: {_detail(exc)}", "SYSTEM")
    container.audit("chat_thread_deleted", actor=principal.username, target=thread_id, detail=f"hermes_session={removed_session}")
    return {"ok": True, "deleted": thread_id, "hermes_session_deleted": removed_session}


@router.get("/threads/{thread_id}/messages")
async def thread_messages(
    thread_id: str,
    principal: Annotated[Principal, Depends(require_admin)],
    container: ContainerDep,
    limit: int = Query(200, ge=1, le=1000),
) -> dict:
    _thread_row(container, thread_id)
    rows = container.db.query(
        "SELECT id, role, content, created_at, model, provider, cost_tier, tokens, meta FROM chat_messages "
        "WHERE thread_id = ? ORDER BY id ASC LIMIT ?",
        (thread_id, limit),
    )
    messages = []
    for row in rows:
        meta = {}
        if row.get("meta"):
            try:
                meta = json.loads(row["meta"])
            except (TypeError, ValueError):
                meta = {}
        messages.append(
            {
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "created_at": row["created_at"],
                "model": row["model"] or "",
                "provider": row["provider"] or "",
                "cost_tier": row["cost_tier"] or "",
                "tokens": row["tokens"],
                "meta": meta,
            }
        )
    return {"messages": messages, "thread": _thread_payload(_thread_row(container, thread_id))}


def _history_for_run(container: Container, thread_id: str) -> list[dict]:
    rows = container.db.query(
        "SELECT role, content FROM chat_messages WHERE thread_id = ? ORDER BY id DESC LIMIT ?",
        (thread_id, MAX_HISTORY_MESSAGES),
    )
    history = [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]
    return [item for item in history if item["content"]]


async def _store_message(
    container: Container,
    thread_id: str,
    role: str,
    content: str,
    *,
    model: str = "",
    provider: str = "",
    cost_tier: str = "",
    tokens: int | None = None,
    meta: dict | None = None,
) -> int:
    message_id = container.db.insert(
        """
        INSERT INTO chat_messages (thread_id, role, content, created_at, model, provider, cost_tier, tokens, meta)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (thread_id, role, content, utcnow(), model or None, provider or None, cost_tier or None, tokens, json.dumps(meta or {})),
    )
    container.db.execute("UPDATE chat_threads SET updated_at = ? WHERE thread_id = ?", (utcnow(), thread_id))
    return message_id


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/threads/{thread_id}/messages")
async def send_message(
    thread_id: str,
    body: MessageBody,
    request: Request,
    principal: Annotated[Principal, Depends(mutation_principal)],
    container: ContainerDep,
) -> StreamingResponse:
    """Streams a real agent turn as Server-Sent Events."""

    enforce_rate_limit(request, "chat")
    row = _thread_row(container, thread_id)
    health = await bridge_health(container)
    if not health.get("reachable"):
        payload = human_error(
            "chat_bridge_unavailable",
            "The agent bridge is not ready",
            health.get("reason") or "Chat needs the local agent bridge.",
            hint="Press Enable on the Chat page. It stores a key, enables Hermes' API server platform, and starts the gateway.",
            actions=[{"label": "Chat settings", "kind": "route", "target": "chat"}],
            status=503,
        )
        raise HTTPException(status_code=503, detail=payload)

    history = _history_for_run(container, thread_id)
    user_message_id = await _store_message(container, thread_id, "user", body.content)
    container.audit("chat_message", actor=principal.username, target=thread_id, detail=f"{len(body.content)} chars")
    key = container.secrets.reveal_safe(BRIDGE_KEY_NAME)
    base_url = health["base_url"]
    model = body.model or row["model"] or "hermes-agent"
    session_id = row["hermes_session_id"] or ""
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "X-Hermes-Session-Key": f"control-center:{thread_id}",
    }
    if session_id:
        headers["X-Hermes-Session-Id"] = session_id

    async def runner() -> AsyncIterator[str]:
        started = time.time()
        assistant_text: list[str] = []
        run_id = ""
        cost_tier = ""
        tools: list[dict] = []
        usage: dict = {}
        error_payload: dict | None = None

        try:
            model_info = await container.supervisor.client().get("/api/model/info", timeout=15)
            provider = str(model_info.get("provider") or "")
            classification = provider_catalog.classify_model(provider, str(model_info.get("model") or ""))
            cost_tier = classification["cost_label"]
        except (HermesAPIError, HermesUnavailable):
            provider = ""

        yield _sse("status", {"stage": "submitting", "model": model, "provider": provider, "cost": cost_tier, "history": len(history)})

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None)) as client:
                # /v1/runs gives us a run id, so "Stop" is a real server-side cancel.
                accept = await client.post(
                    f"{base_url}/v1/runs",
                    json={"input": body.content, "model": model, "conversation_history": history},
                    headers=headers,
                )
                if accept.status_code == 404:
                    async for frame in _stream_chat_completions(client, base_url, headers, model, body.content, history):
                        if frame["event"] == "delta":
                            assistant_text.append(frame["data"].get("text", ""))
                        if frame["event"] == "tool":
                            tools.append(frame["data"])
                        if frame["event"] == "error":
                            error_payload = frame["data"]
                        yield _sse(frame["event"], frame["data"])
                    return
                if accept.status_code >= 400:
                    detail = accept.text[:800]
                    payload = classify(status=accept.status_code, message=detail, model=model, provider=provider)
                    yield _sse("error", payload)
                    error_payload = payload
                else:
                    accepted = accept.json() if accept.content else {}
                    run_id = str(accepted.get("run_id") or accepted.get("id") or "")
                    session_from_run = str(
                        accepted.get("session_id") or accept.headers.get("X-Hermes-Session-Id") or ""
                    )
                    if session_from_run:
                        await _remember_session(container, thread_id, session_from_run)
                    yield _sse("run", {"run_id": run_id, "status": accepted.get("status", "queued")})

                    async with client.stream("GET", f"{base_url}/v1/runs/{run_id}/events", headers=headers) as events:
                        if events.status_code >= 400:
                            payload = classify(status=events.status_code, message=await events.aread(), model=model, provider=provider)
                            yield _sse("error", payload)
                            error_payload = payload
                        else:
                            buffer = ""
                            async for chunk in events.aiter_text():
                                if await request.is_disconnected():
                                    # The browser went away: stop the run rather than let it burn tokens.
                                    await _stop_run(container, run_id)
                                    return
                                buffer += chunk
                                while "\n\n" in buffer:
                                    frame, _, buffer = buffer.partition("\n\n")
                                    data_lines = [line[5:].strip() for line in frame.splitlines() if line.startswith("data:")]
                                    if not data_lines:
                                        continue
                                    try:
                                        event = json.loads("\n".join(data_lines))
                                    except ValueError:
                                        continue
                                    name = str(event.get("event") or event.get("type") or "")
                                    if name == "message.delta" or "delta" in event:
                                        text = str(event.get("delta") or "")
                                        if text:
                                            assistant_text.append(text)
                                            yield _sse("delta", {"text": text})
                                            continue
                                    if name.startswith("tool."):
                                        entry = {
                                            "name": event.get("name") or event.get("tool") or "tool",
                                            "phase": name.split(".", 1)[1],
                                            "detail": event.get("preview") or event.get("detail") or "",
                                        }
                                        if entry["phase"] == "completed":
                                            tools.append(entry)
                                        yield _sse("tool", entry)
                                        continue
                                    if name == "approval.request":
                                        yield _sse(
                                            "approval",
                                            {
                                                "request_id": event.get("request_id") or "",
                                                "command": event.get("command") or "",
                                                "choices": event.get("choices") or ["once", "deny"],
                                            },
                                        )
                                        continue
                                    if name == "reasoning.available":
                                        yield _sse("reasoning", {"text": str(event.get("text") or event.get("reasoning") or "")[:4000]})
                                        continue
                                    if name.startswith("run."):
                                        status = name.split(".", 1)[1]
                                        usage = event.get("usage") or usage
                                        yield _sse("status", {"stage": status, "usage": usage})
                                        if status in {"completed", "failed", "cancelled", "interrupted"}:
                                            if status == "failed":
                                                error_payload = classify(
                                                    status=500, message=str(event.get("error") or "The run failed."), model=model, provider=provider
                                                )
                                                yield _sse("error", error_payload)
                                            continue
                                        continue
                                    if name:
                                        yield _sse("event", {"name": name, "data": event})
        except httpx.HTTPError as exc:
            payload = human_error(
                "chat_transport_error",
                "The connection to the agent dropped",
                f"{exc.__class__.__name__}: {exc}",
                hint="Check that the gateway is still running, then retry.",
                retryable=True,
                status=503,
            )
            yield _sse("error", payload)
            error_payload = payload
        except Exception as exc:  # noqa: BLE001 - never leave the stream hanging
            payload = human_error("chat_internal_error", "The chat request failed", str(exc)[:600], retryable=True, status=500)
            yield _sse("error", payload)
            error_payload = payload
        finally:
            text = "".join(assistant_text)
            if text or error_payload:
                await _store_message(
                    container,
                    thread_id,
                    "assistant",
                    text or (error_payload or {}).get("message", ""),
                    model=model,
                    provider=provider,
                    cost_tier=cost_tier,
                    tokens=(usage or {}).get("total_tokens"),
                    meta={"run_id": run_id, "tools": tools, "error": error_payload, "latency_ms": round((time.time() - started) * 1000)},
                )
            if run_id:
                yield _sse("done", {"run_id": run_id, "chars": len(text), "latency_ms": round((time.time() - started) * 1000), "usage": usage})
            else:
                yield _sse("done", {"chars": len(text), "latency_ms": round((time.time() - started) * 1000), "usage": usage})

    return StreamingResponse(
        runner(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


async def _stream_chat_completions(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict,
    model: str,
    content: str,
    history: list[dict],
) -> AsyncIterator[dict]:
    """Fallback path for builds without /v1/runs (older Hermes)."""

    payload = {
        "model": model,
        "messages": [*history, {"role": "user", "content": content}],
        "stream": True,
    }
    async with client.stream("POST", f"{base_url}/v1/chat/completions", json=payload, headers={**headers, "Accept": "text/event-stream"}) as response:
        if response.status_code >= 400:
            body = (await response.aread()).decode("utf-8", "replace")[:800]
            yield {"event": "error", "data": classify(status=response.status_code, message=body, model=model), "streamed": False}
            return
        async for line in response.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                return
            try:
                payload = json.loads(chunk)
            except ValueError:
                continue
            for choice in payload.get("choices") or []:
                delta = choice.get("delta") or {}
                text = delta.get("content")
                if text:
                    yield {"event": "delta", "data": {"text": text}, "streamed": True}
                reasoning = delta.get("reasoning_content")
                if reasoning:
                    yield {"event": "reasoning", "data": {"text": reasoning}, "streamed": True}
                for call in delta.get("tool_calls") or []:
                    name = ((call.get("function") or {}).get("name")) or "tool"
                    yield {"event": "tool", "data": {"name": name, "phase": "started", "detail": ""}, "streamed": True}


async def _remember_session(container: Container, thread_id: str, session_id: str) -> None:
    container.db.execute(
        "UPDATE chat_threads SET hermes_session_id = ? WHERE thread_id = ?", (session_id, thread_id)
    )


async def _stop_run(container: Container, run_id: str) -> bool:
    if not run_id:
        return False
    key = container.secrets.reveal_safe(BRIDGE_KEY_NAME)
    health = await bridge_health(container)
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                f"{health['base_url']}/v1/runs/{run_id}/stop",
                headers={"Authorization": f"Bearer {key}"},
            )
        return response.status_code < 400
    except httpx.HTTPError:
        return False


@router.post("/threads/{thread_id}/stop")
async def stop_thread_run(
    thread_id: str,
    principal: Annotated[Principal, Depends(mutation_principal)],
    container: ContainerDep,
) -> dict:
    row = _thread_row(container, thread_id)
    last = container.db.query_one(
        "SELECT meta FROM chat_messages WHERE thread_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT 1",
        (thread_id,),
    )
    run_id = ""
    if last and last["meta"]:
        try:
            run_id = str((json.loads(last["meta"]) or {}).get("run_id") or "")
        except (TypeError, ValueError):
            run_id = ""
    if not run_id:
        return {"ok": False, "stopped": False, "message": "No run is recorded for this conversation. If a reply is streaming, the browser is cancelling it."}
    stopped = await _stop_run(container, run_id)
    container.audit("chat_run_stopped", actor=principal.username, target=thread_id, detail=f"run={run_id} stopped={stopped}")
    return {"ok": True, "stopped": stopped, "run_id": run_id}


@router.post("/runs/{run_id}/approval")
async def approve_run(
    run_id: str,
    body: ApprovalBody,
    principal: Annotated[Principal, Depends(mutation_principal)],
    container: ContainerDep,
) -> dict:
    """Answers a tool-approval prompt raised by the agent (shell commands, etc.)."""

    key = container.secrets.reveal_safe(BRIDGE_KEY_NAME)
    health = await bridge_health(container)
    payload: dict[str, Any] = {"choice": body.choice}
    if body.request_id:
        payload["request_id"] = body.request_id
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{health['base_url']}/v1/runs/{run_id}/approval",
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"The agent bridge did not answer: {exc}") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text[:600])
    container.audit(
        "run_approval",
        actor=principal.username,
        target=run_id,
        detail=f"choice={body.choice}",
        level="warning",
    )
    return {"ok": True, "choice": body.choice, "result": response.json() if response.content else {}}


@router.post("/threads/{thread_id}/retry")
async def retry_thread(
    thread_id: str,
    principal: Annotated[Principal, Depends(mutation_principal)],
    container: ContainerDep,
) -> dict:
    """Re-sends the last user message of a conversation."""

    _thread_row(container, thread_id)
    last_user = container.db.query_one(
        "SELECT content FROM chat_messages WHERE thread_id = ? AND role = 'user' ORDER BY id DESC LIMIT 1",
        (thread_id,),
    )
    if not last_user:
        raise HTTPException(status_code=409, detail="There is no user message to retry in this conversation.")
    # Drop the failed assistant reply so the retry does not pile up duplicates.
    container.db.execute(
        "DELETE FROM chat_messages WHERE thread_id = ? AND role = 'assistant' AND id = "
        "(SELECT id FROM chat_messages WHERE thread_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT 1)",
        (thread_id, thread_id),
    )
    container.audit("chat_retry", actor=principal.username, target=thread_id)
    return {"ok": True, "content": last_user["content"], "note": "Re-send this text to /threads/{id}/messages; the failed reply was removed."}


@router.post("/threads/{thread_id}/session")
async def attach_session(
    thread_id: str,
    principal: Annotated[Principal, Depends(mutation_principal)],
    container: ContainerDep,
    session_id: str = Query("", max_length=120),
    clear: bool = Query(False),
) -> dict:
    """Point this conversation at a Hermes session (or detach it)."""

    _thread_row(container, thread_id)
    value = "" if clear else session_id
    container.db.execute("UPDATE chat_threads SET hermes_session_id = ? WHERE thread_id = ?", (value or None, thread_id))
    container.audit("chat_session_attached", actor=principal.username, target=thread_id, detail=value or "cleared")
    return {"ok": True, "hermes_session_id": value}


@router.get("/models")
async def bridge_models(principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep) -> dict:
    health = await bridge_health(container)
    return {"models": health.get("models", []), "reachable": health.get("reachable"), "reason": health.get("reason")}


@router.get("/runs/{run_id}")
async def run_status(run_id: str, principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep) -> dict:
    key = container.secrets.reveal_safe(BRIDGE_KEY_NAME)
    health = await bridge_health(container)
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                f"{health['base_url']}/v1/runs/{run_id}",
                headers={"Authorization": f"Bearer {key}"},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text[:600])
    return response.json()


def _detail(exc: Exception) -> str:
    return exc.human_detail() if isinstance(exc, HermesAPIError) else str(exc)


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}{'•' * 6}{value[-4:]}"
