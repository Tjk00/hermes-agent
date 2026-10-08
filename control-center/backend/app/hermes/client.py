"""HTTP client for the upstream Hermes Agent backend.

The Control Center speaks to Hermes exactly the way its own web UI does: over
loopback HTTP with the ``X-Hermes-Session-Token`` header that the desktop shell
injects (``HERMES_DASHBOARD_SESSION_TOKEN``). Nothing here is private API — these
are the same documented routes the dashboard uses.

Errors are normalised into two types so callers can render something useful:

* :class:`HermesUnavailable` — the runtime is not running/reachable at all;
* :class:`HermesAPIError` — the runtime answered with an error (4xx/5xx).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

TOKEN_HEADER = "X-Hermes-Session-Token"


class HermesError(RuntimeError):
    """Base class for Hermes integration failures."""


class HermesUnavailable(HermesError):
    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def human_detail(self) -> str:
        return self.detail or self.message


class HermesAPIError(HermesError):
    def __init__(self, status: int, detail: str, *, path: str = "", body: Any = None) -> None:
        super().__init__(f"{status} {detail}")
        self.status = status
        self.detail = detail
        self.path = path
        self.body = body

    def human_detail(self) -> str:
        base = self.detail.strip() or f"HTTP {self.status}"
        if self.status == 401:
            return (
                f"{base} — the runtime rejected the Control Center session token. "
                "This normally means the Hermes process was restarted by something else; "
                "press Restart on the Dashboard to re-synchronise."
            )
        if self.status == 404:
            return (
                f"{base} — this route does not exist in the running Hermes version. "
                "Update the checkout, or check that the Control Center supports this upstream version."
            )
        return base


def _extract_detail(response: httpx.Response) -> tuple[str, Any]:
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        text = response.text.strip()
        return (text[:600] if text else f"HTTP {response.status_code}"), None
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("message") or payload.get("error")
        if isinstance(detail, list):
            detail = "; ".join(str(item) for item in detail)
        return str(detail or json.dumps(payload)[:600]), payload
    return str(payload)[:600], payload


class HermesClient:
    """Thin async wrapper with sane timeouts and normalised errors."""

    def __init__(self, base_url: str, token: str = "", *, default_timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.default_timeout = default_timeout

    # ---------------------------------------------------------------- plumbing
    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers[TOKEN_HEADER] = self.token
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: Any = None,
        timeout: float | None = None,
        allow_status: tuple[int, ...] = (),
    ) -> Any:
        try:
            async with httpx.AsyncClient(timeout=timeout or self.default_timeout) as client:
                response = await client.request(
                    method.upper(),
                    self._url(path),
                    params=params,
                    json=json_body,
                    headers=self._headers(),
                )
        except httpx.ConnectError as exc:
            raise HermesUnavailable(
                "The Hermes runtime is not accepting connections.",
                detail=f"{self.base_url} refused the connection ({exc.__class__.__name__}).",
            ) from exc
        except httpx.TimeoutException as exc:
            raise HermesUnavailable(
                "The Hermes runtime did not answer in time.",
                detail=f"{method.upper()} {path} timed out after {timeout or self.default_timeout:.0f}s.",
            ) from exc
        except httpx.HTTPError as exc:  # pragma: no cover - transport level
            raise HermesUnavailable("The Hermes runtime request failed.", detail=str(exc)) from exc

        if response.status_code in allow_status:
            return _safe_json(response)
        if response.status_code >= 400:
            detail, payload = _extract_detail(response)
            raise HermesAPIError(response.status_code, detail, path=path, body=payload)
        return _safe_json(response)

    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, json_body: Any = None, **kwargs: Any) -> Any:
        return await self.request("POST", path, json_body=json_body if json_body is not None else {}, **kwargs)

    async def put(self, path: str, json_body: Any = None, **kwargs: Any) -> Any:
        return await self.request("PUT", path, json_body=json_body if json_body is not None else {}, **kwargs)

    async def patch(self, path: str, json_body: Any = None, **kwargs: Any) -> Any:
        return await self.request("PATCH", path, json_body=json_body if json_body is not None else {}, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> Any:
        return await self.request("DELETE", path, **kwargs)

    # ------------------------------------------------- synchronous (supervisor)
    def request_sync(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: Any = None,
        timeout: float = 15.0,
    ) -> Any:
        """Blocking variant used from supervisor threads, never from request handlers."""

        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.request(
                    method.upper(),
                    self._url(path),
                    params=params,
                    json=json_body,
                    headers=self._headers(),
                )
        except httpx.ConnectError as exc:
            raise HermesUnavailable(
                "The Hermes runtime is not accepting connections.",
                detail=f"{self.base_url} refused the connection.",
            ) from exc
        except httpx.TimeoutException as exc:
            raise HermesUnavailable("The Hermes runtime did not answer in time.", detail=str(exc)) from exc
        except httpx.HTTPError as exc:  # pragma: no cover
            raise HermesUnavailable("The Hermes runtime request failed.", detail=str(exc)) from exc
        if response.status_code >= 400:
            detail, payload = _extract_detail(response)
            raise HermesAPIError(response.status_code, detail, path=path, body=payload)
        return _safe_json(response)

    def health_sync(self, *, timeout: float = 5.0) -> dict:
        payload = self.request_sync("GET", "/api/health", timeout=timeout)
        return payload if isinstance(payload, dict) else {"raw": payload}

    def status_sync(self, *, timeout: float = 15.0) -> dict:
        payload = self.request_sync("GET", "/api/status", timeout=timeout)
        return payload if isinstance(payload, dict) else {"raw": payload}

    # ------------------------------------------------------------------ health
    async def health(self, *, timeout: float = 5.0) -> dict:
        payload = await self.get("/api/health", timeout=timeout)
        return payload if isinstance(payload, dict) else {"raw": payload}

    async def status(self, *, timeout: float = 15.0) -> dict:
        payload = await self.get("/api/status", timeout=timeout)
        return payload if isinstance(payload, dict) else {"raw": payload}

    async def version_hint(self) -> dict:
        """Everything the UI shows in the "upstream" card."""

        out: dict[str, Any] = {}
        for path, key in (("/api/status", "status"), ("/api/model/info", "model_info")):
            try:
                out[key] = await self.get(path, timeout=20)
            except (HermesAPIError, HermesUnavailable) as exc:
                out[f"{key}_error"] = exc.human_detail() if isinstance(exc, HermesAPIError) else str(exc)
        return out

    # ------------------------------------------------------------- SSE passthru
    async def stream(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        timeout: float | None = None,
    ) -> AsyncIterator[bytes]:
        """Yield raw response chunks (used to proxy OpenAI-style SSE)."""

        headers = self._headers()
        headers["Accept"] = "text/event-stream"
        timeout_config = httpx.Timeout(timeout or self.default_timeout, connect=10.0, read=None)
        client = httpx.AsyncClient(timeout=timeout_config)
        try:
            async with client.stream(
                method.upper(), self._url(path), json=json_body, headers=headers
            ) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    detail = body.decode("utf-8", "replace")[:600]
                    raise HermesAPIError(response.status_code, detail, path=path)
                async for chunk in response.aiter_bytes():
                    yield chunk
        except httpx.ConnectError as exc:
            raise HermesUnavailable(
                "The Hermes runtime is not accepting connections.",
                detail=f"{self.base_url} refused the connection.",
            ) from exc
        finally:
            await client.aclose()


def _safe_json(response: httpx.Response) -> Any:
    if response.status_code == 204 or not response.content:
        return {}
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError):
            return {"raw": response.text[:2000]}
    return {"raw": response.text[:2000]}
