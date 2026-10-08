"""Authentication: first-run admin creation, sign-in, sessions, API tokens."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..container import Container
from ..db import utcnow
from ..deps import (
    ContainerDep,
    Principal,
    clear_session_cookie,
    client_ip,
    create_session,
    csrf_token_for,
    current_principal,
    enforce_rate_limit,
    mutation_principal,
    new_csrf_token,
    optional_principal,
    require_admin,
    request_is_secure,
    set_session_cookie,
)
from ..security import (
    constant_time_equals,
    hash_password,
    new_token,
    password_strength_problems,
    token_fingerprint,
    valid_username,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class SetupBody(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=1024)


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class PasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=8, max_length=1024)


class TokenBody(BaseModel):
    label: str = Field(default="api", max_length=120)


def _admin_count(container: Container) -> int:
    row = container.db.query_one("SELECT COUNT(*) AS n FROM users")
    return int(row["n"]) if row else 0


@router.get("/status")
async def auth_status(request: Request, container: ContainerDep) -> dict:
    """Public: tells the SPA whether to show sign-in or the first-run wizard."""

    users = _admin_count(container)
    principal = optional_principal(request, container)
    return {
        "needs_setup": users == 0,
        "authenticated": principal is not None,
        "user": principal.to_dict() if principal else None,
        "allow_registration": container.settings.allow_registration,
        "secure_cookie": request_is_secure(request),
        "runtime_mode": container.settings.runtime_mode,
        "free_mode": container.free_mode,
        "paid_unlocked": container.paid_unlocked,
    }


@router.post("/setup")
async def setup(body: SetupBody, request: Request, response: Response, container: ContainerDep) -> dict:
    enforce_rate_limit(request, "login")
    if _admin_count(container) > 0:
        raise HTTPException(status_code=409, detail="An administrator already exists — sign in instead.")
    if not valid_username(body.username):
        raise HTTPException(status_code=400, detail="Use 3–64 characters: letters, digits, . _ @ or -.")
    problems = password_strength_problems(body.password)
    if problems:
        raise HTTPException(status_code=400, detail=" ".join(problems))

    user_id = container.db.insert(
        "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'admin', ?)",
        (body.username, hash_password(body.password), utcnow()),
    )
    session_id, cookie_value, csrf = create_session(
        container, user_id=user_id, username=body.username, role="admin", request=request
    )
    set_session_cookie(response, container, cookie_value, secure=request_is_secure(request))
    container.audit("admin_created", actor=body.username, target=f"user:{user_id}", ip=client_ip(request))
    container.logs.security(f"First-run administrator '{body.username}' created.", level="INFO")
    return {"ok": True, "user": {"username": body.username, "role": "admin"}, "csrf_token": csrf}


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response, container: ContainerDep) -> dict:
    enforce_rate_limit(request, "login")
    row = container.db.query_one("SELECT * FROM users WHERE username = ?", (body.username,))
    if not row or not row["password_hash"] or not verify_password(body.password, row["password_hash"]) or row["disabled"]:
        container.audit("login_failed", actor=body.username, ip=client_ip(request), level="warning")
        container.logs.security(f"Failed sign-in for '{body.username}' from {client_ip(request)}.")
        raise HTTPException(status_code=401, detail="Incorrect username or password.")
    container.db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (utcnow(), row["id"]))
    session_id, cookie_value, csrf = create_session(
        container, user_id=int(row["id"]), username=row["username"], role=row["role"], request=request
    )
    set_session_cookie(response, container, cookie_value, secure=request_is_secure(request))
    container.audit("login", actor=row["username"], ip=client_ip(request))
    return {"ok": True, "user": {"username": row["username"], "role": row["role"]}, "csrf_token": csrf}


@router.post("/logout")
async def logout(request: Request, response: Response, container: ContainerDep) -> dict:
    principal = optional_principal(request, container)
    if principal and principal.session_id:
        container.db.execute("UPDATE auth_sessions SET revoked = 1 WHERE session_id = ?", (principal.session_id,))
        container.audit("logout", actor=principal.username, ip=client_ip(request))
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/me")
async def me(principal: Annotated[Principal, Depends(current_principal)], container: ContainerDep) -> dict:
    payload: dict = {"user": principal.to_dict(), "needs_setup": False}
    row = container.db.query_one("SELECT csrf_fingerprint FROM auth_sessions WHERE session_id = ?", (principal.session_id,)) if principal.session_id else None
    payload["csrf_required"] = principal.kind == "cookie"
    payload["csrf_present"] = bool(row)
    return payload


@router.get("/csrf")
async def csrf(principal: Annotated[Principal, Depends(current_principal)], container: ContainerDep) -> dict:
    """Rotates the CSRF token for this session and returns it.

    The SPA calls this right after sign-in, so a freshly created session always has one.
    """

    if principal.kind != "cookie" or not principal.session_id:
        return {"csrf_token": "", "kind": principal.kind, "note": "Bearer tokens do not use CSRF."}
    return {"csrf_token": new_csrf_token(container, principal.session_id), "kind": "cookie"}


@router.post("/password")
async def change_password(
    body: PasswordBody,
    request: Request,
    principal: Annotated[Principal, Depends(mutation_principal)],
    container: ContainerDep,
) -> dict:
    row = container.db.query_one("SELECT * FROM users WHERE id = ?", (principal.user_id,))
    if not row or not verify_password(body.current_password, row["password_hash"]):
        container.audit("password_change_failed", actor=principal.username, ip=client_ip(request), level="warning")
        raise HTTPException(status_code=401, detail="The current password is not correct.")
    problems = password_strength_problems(body.new_password)
    if problems:
        raise HTTPException(status_code=400, detail=" ".join(problems))
    container.db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(body.new_password), principal.user_id))
    # Every other session is invalidated; this one keeps working.
    container.db.execute(
        "UPDATE auth_sessions SET revoked = 1 WHERE user_id = ? AND session_id != ?",
        (principal.user_id, principal.session_id),
    )
    container.audit("password_changed", actor=principal.username, ip=client_ip(request))
    container.logs.security(f"Password changed for '{principal.username}'. Other sessions were signed out.", level="INFO")
    return {"ok": True}


@router.get("/sessions")
async def list_sessions(principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep) -> dict:
    rows = container.db.query(
        """
        SELECT s.session_id, s.created_at, s.expires_at, s.last_seen_at, s.user_agent, s.ip, s.revoked, u.username
        FROM auth_sessions s JOIN users u ON u.id = s.user_id
        WHERE s.revoked = 0
        ORDER BY s.last_seen_at DESC
        LIMIT 50
        """
    )
    return {
        "sessions": [
            {
                "id": row["session_id"],
                "username": row["username"],
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "last_seen_at": row["last_seen_at"],
                "user_agent": row["user_agent"] or "",
                "ip": row["ip"] or "",
            }
            for row in rows
        ]
    }


@router.delete("/sessions/{session_id}")
async def revoke_session(
    session_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(mutation_principal)],
    container: ContainerDep,
) -> dict:
    container.db.execute("UPDATE auth_sessions SET revoked = 1 WHERE session_id = ?", (session_id,))
    container.audit("session_revoked", actor=principal.username, target=session_id, ip=client_ip(request))
    return {"ok": True, "revoked": session_id}


@router.post("/tokens")
async def create_api_token(
    body: TokenBody,
    principal: Annotated[Principal, Depends(require_admin)],
    container: ContainerDep,
) -> dict:
    token = f"cc_{new_token(32)}"
    container.db.insert(
        "INSERT INTO api_tokens (label, token_fingerprint, created_at, user_id) VALUES (?, ?, ?, ?)",
        (body.label, token_fingerprint(token), utcnow(), principal.user_id),
    )
    container.audit("api_token_created", actor=principal.username, target=body.label)
    container.logs.security(f"API token '{body.label}' created for {principal.username}.", level="WARNING")
    return {
        "ok": True,
        "token": token,
        "note": "Copy it now — only a hash is stored, so it cannot be shown again.",
    }


@router.get("/tokens")
async def list_api_tokens(principal: Annotated[Principal, Depends(require_admin)], container: ContainerDep) -> dict:
    rows = container.db.query(
        "SELECT id, label, created_at, last_used_at, revoked FROM api_tokens ORDER BY id DESC LIMIT 50"
    )
    return {"tokens": rows}


@router.delete("/tokens/{token_id}")
async def revoke_api_token(
    token_id: int,
    principal: Annotated[Principal, Depends(require_admin)],
    container: ContainerDep,
) -> dict:
    container.db.execute("UPDATE api_tokens SET revoked = 1 WHERE id = ?", (token_id,))
    container.audit("api_token_revoked", actor=principal.username, target=str(token_id))
    return {"ok": True}
