"""FastAPI dependencies: identity, CSRF, rate limiting, client IP.

Authentication model
--------------------
* Browser: HttpOnly + SameSite=Lax session cookie (Secure when served over HTTPS),
  carrying an opaque session id and an HMAC signature. Writes additionally need the
  session's CSRF token in ``X-CSRF-Token`` (double submit against the database).
* Automation: ``Authorization: Bearer <token>`` created from the Settings page.
  Bearer requests skip CSRF (there is no ambient cookie to abuse) but are still
  rate limited and audited.

Everything token-shaped is stored as a hash, so a database dump cannot be replayed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response

from .config import get_settings
from .container import Container, get_container
from .db import utcnow
from .security import (
    SecurityError,
    constant_time_equals,
    get_rate_limiter,
    new_token,
    sign_value,
    token_fingerprint,
    unsign_value,
)

COOKIE_NAME = "cc_session"
CSRF_HEADER = "X-CSRF-Token"


@dataclass
class Principal:
    user_id: int
    username: str
    role: str
    session_id: str = ""
    kind: str = "cookie"

    def to_dict(self) -> dict:
        return {"username": self.username, "role": self.role, "kind": self.kind}


# ---------------------------------------------------------------------- IP


def client_ip(request: Request) -> str:
    settings = get_settings()
    peer = request.client.host if request.client else ""
    if settings.trusted_proxies and peer in settings.trusted_proxies:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return peer


def request_is_secure(request: Request) -> bool:
    settings = get_settings()
    if settings.secure_cookies == "always":
        return True
    if settings.secure_cookies == "never":
        return False
    if request.url.scheme == "https":
        return True
    peer = request.client.host if request.client else ""
    if settings.trusted_proxies and peer in settings.trusted_proxies:
        return request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
    return False


# ---------------------------------------------------------------- rate limit


def enforce_rate_limit(request: Request, bucket: str) -> None:
    settings = get_settings()
    limits = {
        "login": (10, 900),
        "sensitive": (30, 300),
        "write": (240, 60),
        "read": (1200, 60),
        "chat": (120, 60),
    }
    limit, window = limits.get(bucket, limits["read"])
    key = f"{bucket}:{client_ip(request)}"
    allowed, retry_after = get_rate_limiter().check(key, limit=limit, window_seconds=window)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Too many requests. This endpoint allows {limit} per {window // 60 or 1} minute(s) per address; "
                f"retry in {retry_after}s."
            ),
            headers={"Retry-After": str(retry_after)},
        )


def rate_limit(bucket: str):
    def dependency(request: Request) -> None:
        enforce_rate_limit(request, bucket)

    return Depends(dependency)


# ------------------------------------------------------------------- sessions


def create_session(
    container: Container,
    *,
    user_id: int,
    username: str,
    role: str,
    request: Request,
) -> tuple[str, str, str]:
    """Returns ``(session_id, cookie_value, csrf_token)``."""

    settings = container.settings
    session_id = new_token(24)
    secret_part = new_token(32)
    csrf_token = new_token(24)
    issued = utcnow()
    from datetime import UTC, datetime, timedelta

    expires = (datetime.now(UTC) + timedelta(seconds=settings.session_ttl_seconds)).replace(microsecond=0)
    container.db.insert(
        """
        INSERT INTO auth_sessions
          (session_id, user_id, token_fingerprint, csrf_fingerprint, created_at, expires_at, last_seen_at, user_agent, ip)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            user_id,
            token_fingerprint(secret_part),
            token_fingerprint(csrf_token),
            issued,
            expires.isoformat().replace("+00:00", "Z"),
            issued,
            request.headers.get("user-agent", "")[:300],
            client_ip(request),
        ),
    )
    cookie_value = sign_value(f"{session_id}:{secret_part}", settings.session_secret()).encode()
    return session_id, cookie_value, csrf_token


def set_session_cookie(response: Response, container: Container, cookie_value: str, *, secure: bool) -> None:
    response.set_cookie(
        COOKIE_NAME,
        cookie_value,
        max_age=container.settings.session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def _load_session(container: Container, request: Request) -> Principal | None:
    raw = request.cookies.get(COOKIE_NAME, "")
    if not raw:
        return None
    settings = container.settings
    try:
        payload = unsign_value(raw, settings.session_secret())
    except SecurityError:
        return None
    if not payload or ":" not in payload:
        return None
    session_id, _, secret_part = payload.partition(":")
    row = container.db.query_one(
        """
        SELECT s.*, u.username AS username, u.role AS role, u.disabled AS disabled
        FROM auth_sessions s JOIN users u ON u.id = s.user_id
        WHERE s.session_id = ? AND s.revoked = 0
        """,
        (session_id,),
    )
    if not row:
        return None
    if row["expires_at"] and row["expires_at"] < utcnow():
        return None
    if not constant_time_equals(token_fingerprint(secret_part), row["token_fingerprint"]):
        return None
    if row["disabled"]:
        return None
    container.db.execute("UPDATE auth_sessions SET last_seen_at = ? WHERE session_id = ?", (utcnow(), session_id))
    return Principal(user_id=int(row["user_id"]), username=row["username"], role=row["role"], session_id=session_id, kind="cookie")


def _load_bearer(container: Container, request: Request) -> Principal | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    token = header.split(" ", 1)[1].strip()
    if not token:
        return None
    fingerprint = token_fingerprint(token)
    row = container.db.query_one(
        """
        SELECT t.*, u.username AS username, u.role AS role
        FROM api_tokens t JOIN users u ON u.id = t.user_id
        WHERE t.token_fingerprint = ? AND t.revoked = 0
        """,
        (fingerprint,),
    )
    if not row:
        return None
    container.db.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (utcnow(), row["id"]))
    return Principal(user_id=int(row["user_id"]), username=row["username"], role=row["role"], session_id="", kind="bearer")


def optional_principal(request: Request, container: Container) -> Principal | None:
    return _load_bearer(container, request) or _load_session(container, request)


def current_principal(
    request: Request,
    container: Annotated[Container, Depends(get_container)],
) -> Principal:
    principal = optional_principal(request, container)
    if principal is None:
        raise HTTPException(status_code=401, detail="Not signed in.")
    return principal


def require_admin(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator access is required for this action.")
    return principal


def mutation_principal(
    request: Request,
    container: Annotated[Container, Depends(get_container)],
) -> Principal:
    """Identity + CSRF for anything that changes state."""

    enforce_rate_limit(request, "write")
    principal = current_principal(request, container)
    if principal.kind == "cookie":
        supplied = request.headers.get(CSRF_HEADER, "")
        if not supplied:
            raise HTTPException(status_code=403, detail="Missing CSRF token for a state-changing request.")
        row = container.db.query_one(
            "SELECT csrf_fingerprint FROM auth_sessions WHERE session_id = ?", (principal.session_id,)
        )
        if not row or not constant_time_equals(token_fingerprint(supplied), row["csrf_fingerprint"]):
            raise HTTPException(status_code=403, detail="CSRF token did not match this session.")
    if principal.role != "admin" and request.method not in {"GET", "HEAD"}:
        raise HTTPException(status_code=403, detail="Only administrators can change configuration.")
    return principal


def csrf_token_for(container: Container, session_id: str, supplied: str) -> str:
    """Re-issue a CSRF token when the caller proves it already holds one."""

    row = container.db.query_one("SELECT csrf_fingerprint FROM auth_sessions WHERE session_id = ?", (session_id,))
    if not row or not constant_time_equals(token_fingerprint(supplied), row["csrf_fingerprint"]):
        raise HTTPException(status_code=403, detail="CSRF token did not match this session.")
    return supplied


def new_csrf_token(container: Container, session_id: str) -> str:
    token = new_token(24)
    container.db.execute(
        "UPDATE auth_sessions SET csrf_fingerprint = ? WHERE session_id = ?",
        (token_fingerprint(token), session_id),
    )
    return token


def bootstrap_token(container: Container) -> str:
    """Short-lived bootstrap token used only before the first admin exists."""

    from .config import get_settings as _settings

    secret = _settings().session_secret()
    return hashlib.sha256(f"bootstrap:{secret}".encode()).hexdigest()[:32]


# Convenience aliases used throughout the routers.
PrincipalDep = Annotated[Principal, Depends(current_principal)]
AdminDep = Annotated[Principal, Depends(require_admin)]
MutationDep = Annotated[Principal, Depends(mutation_principal)]
ContainerDep = Annotated[Container, Depends(get_container)]
