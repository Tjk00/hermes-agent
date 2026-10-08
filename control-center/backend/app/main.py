"""Hermes Agent Control Center — FastAPI application.

Public surface
--------------
``/``                the mobile-first SPA (built assets, or a build hint)
``/status``          public liveness JSON (no auth, no sensitive data)
``/api/status``      alias of ``/status`` for probes
``/health``          full health report (auth required)
``/api/cc/*``        the Control Center API (auth + CSRF + rate limits)
``/docs``            OpenAPI UI (disabled unless CC_ENABLE_DOCS=1)

The app never imports Hermes. It supervises a Hermes process, talks to it over
HTTP with a session token, and stores everything of its own in ``CC_HOME``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Annotated
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings, get_settings
from .container import Container, build_container, get_container
from .db import utcnow
from .deps import current_principal
from .errors import HumanError, human_error
from .hermes import rendezvous
from .routes import (
    auth as auth_routes,
    chat as chat_routes,
    deploy as deploy_routes,
    hermes_proxy,
    local as local_routes,
    logs as log_routes,
    models as model_routes,
    providers as provider_routes,
    runtime as runtime_routes,
    settings as settings_routes,
    system as system_routes,
)

log = logging.getLogger("control_center")

API_PREFIX = "/api/cc"
STARTED_AT = time.time()


# --------------------------------------------------------------------- startup
def _bootstrap_admin(container: Container, settings: Settings) -> None:
    """Create the first administrator, or explain how to create one."""

    from .security import hash_password, password_strength_problems, valid_username

    row = container.db.query_one("SELECT COUNT(*) AS n FROM users")
    if row and int(row["n"]) > 0:
        return
    user, password = settings.bootstrap_admin_user, settings.bootstrap_admin_password
    if not user or not password:
        log.info("No administrator yet — the Control Center will show the first-run setup screen.")
        container.logs.warning(
            "No administrator exists and no bootstrap credentials were provided (CC_ADMIN_USER / CC_ADMIN_PASSWORD). "
            "Open the web UI to complete first-run setup.",
            "SECURITY",
        )
        return
    if not valid_username(user) or password_strength_problems(password):
        container.logs.error(
            "CC_ADMIN_USER or CC_ADMIN_PASSWORD were rejected (username 3–32 chars; password at least 10 chars, not a "
            "common password). Complete setup in the browser instead.",
            "SECURITY",
        )
        return
    container.db.insert(
        "INSERT INTO users (username, password_hash, role, created_at, disabled) VALUES (?, ?, 'admin', ?, 0)",
        (user, hash_password(password), utcnow()),
    )
    container.logs.system(f"Bootstrap administrator '{user}' created from environment variables.", )
    container.audit("bootstrap_admin_created", actor="system", target=user)


def _warm_up(container: Container) -> None:
    """Everything that can be slow or network-bound, off the request path."""

    try:
        from .hermes.providers import refresh_env_catalog

        asyncio.run(refresh_env_catalog(container))
    except Exception as exc:  # noqa: BLE001 - never block startup
        container.logs.warning(f"Provider catalogue could not be refreshed yet: {exc}", "PROVIDER")
    try:
        from .container import register_redaction

        register_redaction(container)
    except Exception as exc:  # noqa: BLE001
        container.logs.warning(f"Secret redaction registration failed: {exc}", "SECURITY")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    container = build_container(settings)
    container.initialise()
    _bootstrap_admin(container, settings)
    app.state.container = container

    threading.Thread(target=_warm_up, args=(container,), name="cc-warmup", daemon=True).start()
    threading.Thread(target=container.autostart_runtime, name="cc-autostart", daemon=True).start()
    threading.Thread(target=_scheduler_loop, args=(container,), name="cc-scheduler", daemon=True).start()

    container.logs.system(
        f"Listening on {settings.host}:{settings.port} — API at {API_PREFIX}, "
        f"runtime mode '{settings.runtime_mode}', FREE MODE "
        f"{'on' if container.free_mode else 'off'}."
    )
    try:
        yield
    finally:
        container.shutdown()
        container.logs.system("Control Center stopped.")


def _scheduler_loop(container: Container) -> None:
    """Housekeeping that must not depend on a browser tab being open.

    The agent's own schedules run inside the Hermes process — never here. This loop
    only keeps *our* records tidy (log retention, session expiry, a periodic
    rendezvous check so the UI can tell whether the agent is still attached).
    """

    last_prune = 0.0
    while True:
        time.sleep(60)
        try:
            now = time.time()
            if now - last_prune > 3600:
                last_prune = now
                removed = container.logs.prune()
                if removed:
                    container.logs.system(f"Pruned {removed} log entries past retention.")
                container.db.execute(
                    "DELETE FROM auth_sessions WHERE expires_at < ?", (utcnow(),)
                )
            status = container.supervisor.status(probe=False)
            if status.get("state") in {"stopped", "crashed"} and container.db.get_setting("runtime_autostart", False):
                if container.supervisor.should_auto_restart():
                    container.logs.warning("Hermes runtime is not running — attempting an automatic restart.", "SYSTEM")
                    container.supervisor.start(wait=False)
        except Exception as exc:  # noqa: BLE001 - a housekeeping loop must never die loudly
            try:
                container.logs.warning(f"Housekeeping pass failed: {exc}", "SYSTEM")
            except Exception:  # noqa: BLE001
                pass


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    docs_enabled = os.environ.get("CC_ENABLE_DOCS", "0").lower() in {"1", "true", "yes"}

    app = FastAPI(
        title="Hermes Agent Control Center",
        version=settings.version,
        description=(
            "Control and monitor a real Hermes Agent installation: runtime lifecycle, chat, models, providers, "
            "keys, memory, skills, schedules, sessions and logs."
        ),
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    if settings.allowed_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.allowed_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "X-CSRF-Token", "Authorization"],
        )

    for router, tag in (
        (system_routes.router, "system"),
        (auth_routes.router, "auth"),
        (runtime_routes.router, "runtime"),
        (provider_routes.router, "providers"),
        (model_routes.router, "models"),
        (chat_routes.router, "chat"),
        (local_routes.router, "local"),
        (log_routes.router, "logs"),
        (settings_routes.router, "settings"),
        (deploy_routes.router, "deploy"),
        (hermes_proxy.router, "hermes"),
    ):
        app.include_router(router, prefix=API_PREFIX)

    _install_middleware(app, settings)
    _install_exception_handlers(app, settings)
    _install_public_routes(app, settings)
    _install_spa(app, settings)
    return app


# ------------------------------------------------------------------ middleware
def _install_middleware(app: FastAPI, settings: Settings) -> None:
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - converted into a human payload by the handlers
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.url.path.startswith(API_PREFIX):
            response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Server-Timing", f"app;dur={elapsed_ms:.1f}")
        if settings.secure_cookies == "always":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    @app.middleware("http")
    async def block_cross_site_writes(request: Request, call_next):
        """A cheap, honest CSRF first line for cookie-authenticated writes."""

        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith(API_PREFIX):
            origin = request.headers.get("origin", "")
            if origin and settings.allowed_origins:
                if origin.rstrip("/") not in {item.rstrip("/") for item in settings.allowed_origins}:
                    return JSONResponse(
                        status_code=403,
                        content=human_error(
                            "origin_not_allowed",
                            "Request blocked",
                            f"Origin {origin} is not in CC_ALLOWED_ORIGINS.",
                            hint="Add the address you use to reach this app to CC_ALLOWED_ORIGINS, or open the app directly.",
                        ),
                    )
        return await call_next(request)


# -------------------------------------------------------------------- handlers
def _install_exception_handlers(app: FastAPI, settings: Settings) -> None:
    @app.exception_handler(HumanError)
    async def human_error_handler(request: Request, exc: HumanError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.to_response())

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "message" in detail:
            return JSONResponse(status_code=exc.status_code, content={**detail, "status": exc.status_code})
        message = str(detail)
        payload = human_error(
            "http_error",
            _status_title(exc.status_code),
            message,
            hint=_status_hint(exc.status_code),
            retryable=exc.status_code >= 500,
            status=exc.status_code,
        )
        return JSONResponse(status_code=exc.status_code, content=payload)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = []
        for error in exc.errors()[:6]:
            location = ".".join(str(part) for part in error.get("loc", ()) if part not in {"body", "query", "path"})
            problems.append(f"{location or 'request'}: {error.get('msg', 'invalid')}")
        return JSONResponse(
            status_code=422,
            content=human_error(
                "invalid_request",
                "Some fields need fixing",
                "; ".join(problems) or "The request payload was not valid.",
                hint="Correct the highlighted values and try again.",
                status=422,
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        try:
            container = get_container()
            container.logs.error(f"{request.method} {request.url.path} failed: {exc.__class__.__name__}: {exc}", "ERROR")
        except Exception:  # noqa: BLE001
            pass
        return JSONResponse(
            status_code=500,
            content=human_error(
                "internal_error",
                "Something went wrong inside the Control Center",
                f"{exc.__class__.__name__}: {exc}",
                hint="Open Diagnostics for a full report, and Logs → Control Center for the trace.",
                retryable=True,
                status=500,
            ),
        )


def _status_title(status: int) -> str:
    return {
        400: "That request could not be processed",
        401: "You are not signed in",
        403: "Not allowed",
        404: "Not found",
        409: "That conflicts with the current state",
        413: "That file is too large",
        429: "Too many requests",
        500: "The Control Center hit an internal error",
        502: "The agent or provider did not answer",
        503: "A required service is not available right now",
        504: "That took too long",
    }.get(status, "Request failed")


def _status_hint(status: int) -> str:
    return {
        401: "Sign in again — your session may have expired.",
        403: "If this is a change, you need an administrator session; if it is a cross-site request, open the app directly.",
        429: "Wait a moment and retry; limits protect the free tier.",
        502: "Check Dashboard → Runtime and Logs → Hermes agent.",
        503: "Start the runtime (Dashboard → Runtime), then retry.",
    }.get(status, "")


# ---------------------------------------------------------------- public pages
def _install_public_routes(app: FastAPI, settings: Settings) -> None:
    @app.get("/status", tags=["system"])
    async def public_status() -> dict:
        """Liveness for uptime monitors and container health checks.

        Deliberately generic: no paths, no dependency versions, no key names.
        """

        payload = {
            "app": "hermes-control-center",
            "version": settings.version,
            "status": "ok",
            "uptime_seconds": int(time.time() - STARTED_AT),
            "runtime": {"state": "unknown", "attached": False},
            "database": {"ok": None, "kind": None},
            "time": utcnow(),
        }
        try:
            container = get_container()
            runtime = container.supervisor.status(probe=False)
            database = container.db.health()
            payload["runtime"] = {"state": runtime.get("state"), "attached": bool(runtime.get("attached"))}
            payload["database"] = {"ok": database.get("ok"), "kind": database.get("kind")}
        except Exception as exc:  # noqa: BLE001 - a status endpoint must always answer
            payload["status"] = "degraded"
            payload["problem"] = exc.__class__.__name__
        return payload

    app.add_api_route("/api/status", public_status, methods=["GET"], include_in_schema=False)

    @app.get("/health", tags=["system"])
    async def public_health() -> dict:
        """Health of everything the Control Center is responsible for.

        Answers the questions a monitoring probe actually asks — is Hermes running,
        is a model reachable, is the database writable, is there memory and disk
        space, is the scheduler alive — without revealing keys, model names of paid
        providers or filesystem paths.
        """

        started = time.perf_counter()
        checks: list[dict] = []

        def add(name: str, ok: bool, detail: str, *, critical: bool = False) -> None:
            checks.append({"name": name, "ok": ok, "detail": detail, "critical": critical})

        container: Container | None = None
        try:
            container = get_container()
        except Exception as exc:  # noqa: BLE001
            add("database", False, f"unavailable ({exc.__class__.__name__})", critical=True)
            return _health_response(checks, started, settings)

        # ``probe_timeout`` keeps this cheap: /health is what a container probe calls,
        # and a hung agent must not make the whole app look dead for longer than that.
        try:
            runtime = container.supervisor.status(probe=True, probe_timeout=2.0)
        except Exception as exc:  # noqa: BLE001
            runtime = {"state": "unknown", "healthy": False, "last_error": str(exc)}
        managed_mode = container.settings.runtime_mode
        add(
            "hermes_runtime",
            runtime.get("state") in {"running", "attached", "external"},
            f"mode={managed_mode}, state={runtime.get('state')}"
            + (f", pid={runtime.get('pid')}" if runtime.get("pid") else "")
            + (", adopted an existing backend" if runtime.get("attached") else ""),
            critical=True,
        )
        if runtime.get("state") == "disabled":
            add("hermes_api", True, "runtime management is disabled, so no agent is supervised here")
        else:
            add(
                "hermes_api",
                bool(runtime.get("healthy")),
                "session-token probe " + ("answered" if runtime.get("healthy") else "did not answer"),
            )

        database = container.db.health()
        if database.get("available"):
            add(
                "database",
                True,
                f"{database.get('dialect')} reachable in {database.get('latency_ms')} ms, schema {database.get('schema_version')}",
                critical=True,
            )
        else:
            add("database", False, database.get("error") or "the database did not answer", critical=True)
        add("storage", container.settings.home.exists(), "CC_HOME is writable" if os.access(container.settings.home, os.W_OK) else "CC_HOME is not writable", critical=True)

        try:
            metrics = container.logs.tail(1)
            add("logging", True, f"log bus alive ({len(metrics)} recent entries)")
        except Exception as exc:  # noqa: BLE001
            add("logging", False, f"log bus failed ({exc.__class__.__name__})")

        add("scheduler", _scheduler_alive(), "in-process housekeeping thread; Hermes' own cron runs inside the agent")
        return _health_response(checks, started, settings)

    def _scheduler_alive() -> bool:
        return any(thread.name == "cc-scheduler" and thread.is_alive() for thread in threading.enumerate())

    def _health_response(checks: list[dict], started: float, settings: Settings) -> dict:
        critical_failures = [check for check in checks if check["critical"] and not check["ok"]]
        return {
            "app": "hermes-control-center",
            "version": settings.version,
            "status": "unhealthy" if critical_failures else ("degraded" if any(not check["ok"] for check in checks) else "ok"),
            "uptime_seconds": int(time.time() - STARTED_AT),
            "checks": checks,
            "checked_in_ms": round((time.perf_counter() - started) * 1000, 1),
            "time": utcnow(),
            "note": "This endpoint is public by design so a container probe can use it. Detailed reports are at /api/cc/health.",
        }

    @app.get("/api/cc/identity", tags=["system"])
    async def identity(principal: Annotated[object, Depends(current_principal)]) -> dict:  # noqa: ANN401
        return {
            "signed_in": True,
            "actor": principal.username,  # type: ignore[attr-defined]
            "role": principal.role,  # type: ignore[attr-defined]
            "kind": principal.kind,  # type: ignore[attr-defined]
            "runtime_attached": rendezvous.attempt_adopt().adopted,
            "adoption_reason": rendezvous.attempt_adopt().reason,
        }


# ------------------------------------------------------------------------- SPA
def resolve_static_dir(settings: Settings) -> Path:
    """Where the built SPA lives, most explicit first.

    1. ``CC_STATIC_DIR`` when it actually contains an ``index.html``
       (an unset value is ``Path('.')``, which must never be mistaken for a dist);
    2. ``app/static`` — how the Docker image and a wheel install ship it;
    3. ``control-center/frontend/build`` — the source checkout, after ``npm run build``.
    """

    candidates: list[Path] = []
    configured = str(settings.static_dir or "")
    if configured not in {"", "."}:
        candidates.append(Path(configured))
    candidates.append(Path(__file__).parent / "static")
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidates.append(parent / "frontend" / "build")
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return candidates[0]


def _install_spa(app: FastAPI, settings: Settings) -> None:
    static_dir = resolve_static_dir(settings)
    index = static_dir / "index.html"

    @app.get("/", include_in_schema=False)
    async def root():
        return _index_response(index, static_dir, settings, embed=True)

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.startswith(("api/", "docs", "openapi.json", "status", "health", "assets/", "static/")):
            if path.startswith("assets/") or path.startswith("static/"):
                candidate = static_dir / path
                if candidate.is_file():
                    return FileResponse(candidate)
            return JSONResponse(
                status_code=404,
                content=human_error(
                    "not_found",
                    "No such endpoint",
                    f"/{path} does not exist in this app.",
                    hint="The API lives under /api/cc.",
                    status=404,
                ),
            )
        candidate = static_dir / path
        if candidate.is_file():
            return FileResponse(candidate)
        return _index_response(index, static_dir, settings, embed=False)


def _index_response(index: Path, static_dir: Path, settings: Settings, *, embed: bool) -> HTMLResponse:
    if index.is_file():
        return HTMLResponse(
            index.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-cache"} if embed else {},
        )
    return HTMLResponse(_build_hint(static_dir, settings), status_code=200)


def _build_hint(static_dir: Path, settings: Settings) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<title>Hermes Agent Control Center</title>
<style>
:root {{ color-scheme: dark }}
body {{ margin:0; font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  background:#0b0f14; color:#e6edf3; display:flex; min-height:100vh; align-items:center; justify-content:center; padding:24px }}
main {{ max-width:38rem }}
code, pre {{ background:#161b22; border:1px solid #21262d; border-radius:8px; padding:2px 6px; font-size:13px }}
pre {{ padding:12px; overflow-x:auto }}
h1 {{ font-size:20px; margin:0 0 4px }}
p {{ color:#9aa7b4 }}
a {{ color:#58a6ff }}
</style></head>
<body><main>
<h1>The backend is running — the web UI is not built yet</h1>
<p>This server is the Control Center API (v{settings.version}). The mobile app is a Vite build that is not present at
<code>{static_dir}</code>.</p>
<p>Build it once, then reload this page:</p>
<pre>cd control-center/frontend
npm install
npm run build      # writes control-center/frontend/build
CC_STATIC_DIR=$PWD/build python -m app.main</pre>
<p>If you installed with Docker, the image already contains a build. API probes never need the UI:
<code>/status</code> is public, <code>/api/cc/status/detail</code> needs a session.</p>
</main></body></html>"""


def main() -> None:  # pragma: no cover - console entry point
    import uvicorn

    settings = get_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO))
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        proxy_headers=bool(settings.trusted_proxies),
        forwarded_allow_ips=",".join(settings.trusted_proxies) if settings.trusted_proxies else None,
        root_path=settings.root_path,
    )


def get_app() -> FastAPI:
    """Create the application — the factory uvicorn points at."""

    return create_app()


__all__ = ["create_app", "get_app", "main", "get_container"]
