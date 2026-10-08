"""Entry point: ``python -m app`` (and the ``hermes-control-center`` console script).

    python -m app                      # 0.0.0.0:8080, SQLite in ~/.hermes/control-center
    CC_PORT=9000 python -m app          # another port
    CC_HERMES_SOURCE=/srv/hermes-agent python -m app

Nothing is required to be configured first: with no environment at all the app
starts in FREE MODE, finds the Hermes checkout it ships next to, and waits for you
to create the first administrator in the browser.
"""

from __future__ import annotations

import logging
import os
import sys


def main() -> int:
    try:
        import uvicorn
    except ImportError:  # pragma: no cover - only on a broken install
        print(
            "uvicorn is not installed. Install the package first:\n"
            "    python -m pip install -e '.[web]'\n"
            "or, for the full feature set (encryption, Postgres, metrics):\n"
            "    python -m pip install -e '.[all]'",
            file=sys.stderr,
        )
        return 2

    from .config import get_settings
    from .main import create_app

    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = create_app(settings)
    print(
        f"Hermes Agent Control Center {settings.version}\n"
        f"  URL           http://{'127.0.0.1' if settings.host in {'0.0.0.0', '::'} else settings.host}:{settings.port}\n"
        f"  state         {settings.home}\n"
        f"  database      {'postgresql' if settings.database_url else settings.sqlite_path}\n"
        f"  Hermes source {settings.hermes_source}\n"
        f"  Hermes home   {settings.hermes_home}\n"
        f"  runtime mode  {settings.runtime_mode} ({settings.runtime_host}:{settings.runtime_port})\n"
        f"  FREE MODE     {'on' if settings.free_mode else 'off'}\n"
    )
    # Report the *actual* first-run state. The lifespan reuses this same container
    # (build_container is a process singleton), so opening the database here is
    # safe and it means the banner never claims an admin is missing when one exists.
    try:
        from .container import build_container

        container = build_container(settings)
        container.initialise()
        admins = container.db.query("SELECT username FROM users WHERE role = 'admin' ORDER BY username")
        usernames = [row["username"] for row in admins]
        if usernames:
            print(f"  admin         {', '.join(usernames)} — open the URL and sign in")
        elif settings.bootstrap_admin_password:
            name = settings.bootstrap_admin_user or "the configured admin"
            print(f"  admin         {name} will be created from CC_ADMIN_USER/CC_ADMIN_PASSWORD")
        else:
            print("  admin         none yet — open the URL and complete first-run setup")
    except Exception as exc:  # noqa: BLE001 - the banner must never stop the server
        print(f"  admin         could not be checked ({exc})")
    print(flush=True)

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        proxy_headers=bool(settings.trusted_proxies),
        forwarded_allow_ips=",".join(settings.trusted_proxies) if settings.trusted_proxies else "*",
        root_path=settings.root_path,
        server_header=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
