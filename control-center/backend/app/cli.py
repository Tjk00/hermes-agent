"""Command line administration for the Control Center.

Everything the web UI can do to *itself* can also be done from a terminal — which
matters when you are locked out of your own install, or when the UI is not built
yet:

    python -m app.cli doctor                      # is this host able to run Hermes?
    python -m app.cli create-admin --username me  # first administrator (password prompted)
    python -m app.cli reset-admin  --username me  # forgot the password
    python -m app.cli list-users
    python -m app.cli runtime status|start|stop|restart
    python -m app.cli config export [--output backup.json]
    python -m app.cli keys list                   # names + masks, never values
    python -m app.cli keys remove NAME
    python -m app.cli free-mode status|on|off

Passwords are read from the terminal with echo disabled, or from
``CC_ADMIN_PASSWORD``. They are never accepted as a positional argument (that would
land in your shell history) and never printed back.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from .config import get_settings
from .container import Container, get_container
from .db import utcnow
from .errors import HumanError
from .hermes.locate import detect_install
from .hermes.providers import provider_keys_env_names
from .security import hash_password, password_strength_problems, valid_username


def _container() -> Container:
    container = get_container()
    container.initialise()
    return container


def _read_password(prompt: str, *, confirm: bool = True) -> str:
    import os

    from_env = os.environ.get("CC_ADMIN_PASSWORD", "")
    if from_env:
        return from_env
    try:
        first = getpass.getpass(prompt)
        if confirm:
            again = getpass.getpass("Repeat password: ")
            if first != again:
                raise SystemExit("Passwords did not match.")
        return first
    except EOFError:
        raise SystemExit(
            "No terminal available for a password prompt.\n"
            "Set CC_ADMIN_PASSWORD in the environment for this one command, e.g.\n"
            "  CC_ADMIN_PASSWORD='…' python -m app.cli create-admin --username admin"
        ) from None


# ------------------------------------------------------------------- commands
def cmd_create_admin(args: argparse.Namespace) -> int:
    container = _container()
    username = args.username or input("Username: ").strip()
    if not valid_username(username):
        print("Usernames are 3–32 characters: letters, digits, dot, dash, underscore.", file=sys.stderr)
        return 2
    existing = container.db.query_one("SELECT id FROM users WHERE username = ?", (username,))
    if existing:
        print(f"User '{username}' already exists. Use reset-admin to change the password.", file=sys.stderr)
        return 1
    password = _read_password(f"Password for {username}: ")
    problems = password_strength_problems(password)
    if problems:
        print("Password rejected: " + "; ".join(problems), file=sys.stderr)
        return 2
    container.db.insert(
        "INSERT INTO users (username, password_hash, role, created_at, disabled) VALUES (?, ?, 'admin', ?, 0)",
        (username, hash_password(password), utcnow()),
    )
    container.audit("admin_created", actor="cli", target=username)
    container.logs.security(f"Administrator '{username}' created from the command line.", level="WARNING")
    print(f"Administrator '{username}' created. Sign in at the web UI.")
    return 0


def cmd_reset_admin(args: argparse.Namespace) -> int:
    container = _container()
    username = args.username
    row = container.db.query_one("SELECT id FROM users WHERE username = ?", (username,))
    if not row:
        print(f"No such user: {username}", file=sys.stderr)
        return 1
    password = _read_password(f"New password for {username}: ")
    problems = password_strength_problems(password)
    if problems:
        print("Password rejected: " + "; ".join(problems), file=sys.stderr)
        return 2
    container.db.execute("UPDATE users SET password_hash = ?, disabled = 0 WHERE username = ?", (hash_password(password), username))
    revoked = container.db.execute("DELETE FROM auth_sessions WHERE user_id = ?", (row["id"],))
    container.audit("admin_password_reset", actor="cli", target=username, level="warning")
    container.logs.security(f"Password for '{username}' was reset from the command line; {revoked} session(s) revoked.", "SECURITY")
    print(f"Password updated for '{username}'. {revoked} existing session(s) were revoked.")
    return 0


def cmd_list_users(args: argparse.Namespace) -> int:
    container = _container()
    rows = container.db.query("SELECT username, role, created_at, disabled, last_login_at FROM users ORDER BY username")
    if not rows:
        print("No users yet — the web UI will show first-run setup.")
        return 0
    for row in rows:
        state = "disabled" if row["disabled"] else "active"
        print(f"{row['username']:<24} {row['role']:<8} {state:<9} created {row['created_at']}  last login {row['last_login_at'] or '—'}")
    return 0


def cmd_runtime(args: argparse.Namespace) -> int:
    container = _container()
    action = args.action
    if action == "status":
        status = container.supervisor.status(probe=True)
        for key in ("state", "pid", "port", "attached", "healthy", "mode", "last_error"):
            print(f"{key:<12} {status.get(key)}")
        print(f"{'log file':<12} {container.supervisor.log_file}")
        return 0 if status.get("state") in {"running", "attached", "external"} else 1
    if action == "start":
        container.supervisor.start(wait=True)
    elif action == "stop":
        container.supervisor.stop()
    elif action == "restart":
        container.supervisor.restart()
    status = container.supervisor.status(probe=True)
    print(f"{action}: state={status.get('state')} pid={status.get('pid')} attached={status.get('attached')}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    container = _container()
    settings = container.settings
    install = detect_install(settings)
    print(f"Hermes Agent Control Center {settings.version}")
    print(f"  state home     {settings.home}")
    print(f"  database       {settings.sqlite_path if not settings.database_url else '(external)'}")
    print(f"  Hermes source  {install.source_dir}  {'OK' if install.is_hermes_source else 'MISSING'}")
    print(f"  Hermes home    {settings.hermes_home}")
    print(f"  interpreter    {install.selected_python or 'not found'} {install.selected_python_version}")
    for candidate in install.python_candidates[:4]:
        flags = []
        if candidate.has_hermes_deps:
            flags.append("deps")
        if not candidate.supported:
            flags.append("unsupported-version")
        print(f"    - {candidate.path} {candidate.version} {' '.join(flags)}")
    print("  derived commands:")
    for command in install.launch_commands:
        print(f"    {command}")
    runtime = container.supervisor.status(probe=True)
    print(f"  runtime        state={runtime.get('state')} attached={runtime.get('attached')} healthy={runtime.get('healthy')}")
    print(f"  FREE MODE      {'on' if container.free_mode else 'off'}")
    print(f"  paid unlocked  {container.paid_unlocked}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    container = _container()
    if args.action == "export":
        payload = container.export_configuration(include_secrets=False)
        target = Path(args.output).expanduser() if args.output else settings_backup_path(container)
        target.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"Wrote {target} ({len(payload.get('chat', {}).get('threads', []))} conversations). No API keys included.")
        container.audit("config_exported", actor="cli", target=str(target))
        return 0
    print("Only 'export' is supported from the CLI; import lives in the web UI so a stray file cannot overwrite state silently.", file=sys.stderr)
    return 2


def settings_backup_path(container: Container) -> Path:
    stamp = utcnow().replace(":", "").replace("-", "")
    return container.settings.backups_dir / f"control-center-{stamp}.json"


def cmd_keys(args: argparse.Namespace) -> int:
    container = _container()
    if args.action == "list":
        for row in container.secrets.list_rows():
            print(f"{row['name']:<32} {row.get('masked', '')}  used {row.get('last_used_at') or '—'}")
        if not container.secrets.list_rows():
            print("No keys stored yet. Add them in the web UI (Providers → API keys).")
        return 0
    if args.action == "remove":
        removed = container.secrets.remove(args.name, actor="cli")
        print(f"{'Removed' if removed else 'No such key:'} {args.name}")
        return 0 if removed else 1
    if args.action == "where":
        for name in provider_keys_env_names(args.name):
            print(name)
        return 0
    return 2


def cmd_free_mode(args: argparse.Namespace) -> int:
    container = _container()
    if args.action == "status":
        print(f"free_mode       {container.free_mode}")
        print(f"paid unlocked   {container.paid_unlocked} (requires the confirmation phrase in the app)")
        return 0
    if args.action in {"on", "off"}:
        container.db.set_setting("free_mode", args.action == "on")
        if args.action == "off" and not args.yes:
            print("Refusing to disable FREE MODE without --yes: paid providers become reachable for every user.", file=sys.stderr)
            return 2
        container.audit("free_mode_changed", actor="cli", detail=f"free_mode={args.action == 'on'}", level="warning")
        print(f"free_mode set to {args.action == 'on'}")
        return 0
    return 2


def cmd_reset_db(args: argparse.Namespace) -> int:
    """Start from a clean database, keeping the old one as a timestamped file."""

    if not args.yes:
        print(
            "This moves the current database aside and creates an empty one (existing conversations and\n"
            "stored keys are NOT recreated). Re-run with --yes if that is what you want.",
            file=sys.stderr,
        )
        return 2
    settings = get_settings()
    settings.ensure_dirs()
    backups = []
    for path in (settings.sqlite_path,):
        if path.exists():
            stamp = utcnow().replace(":", "").replace("-", "")
            target = path.with_name(f"{path.name}.old-{stamp}")
            path.rename(target)
            backups.append(str(target))
    # Recreate the schema with a fresh connection.
    from .db import Database

    Database(settings.database_url, home=settings.home)
    print("Reset complete.")
    for item in backups:
        print(f"  previous database kept at {item}")
    if not backups:
        print("  (there was no existing database file)")
    print("  API keys were not restored — add them again under Providers when you next need them.")
    return 0


def cmd_free_routes(args: argparse.Namespace) -> int:
    """Print the free-first route chain this install would actually use."""

    container = _container()
    chain = container.db.get_setting("fallback_chain", []) or []
    print("fallback chain:")
    for index, entry in enumerate(chain, 1):
        print(f"  {index}. {entry.get('provider')} / {entry.get('model')} ({entry.get('billing', '?')})")
    if not chain:
        print("  (empty — open Models → Fallback chain to build one from free routes)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Hermes Agent Control Center administration")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-admin", help="create the first administrator")
    create.add_argument("--username", default="")
    create.set_defaults(func=cmd_create_admin)

    reset = sub.add_parser("reset-admin", help="set a new password for an administrator")
    reset.add_argument("--username", default="admin")
    reset.set_defaults(func=cmd_reset_admin)

    listing = sub.add_parser("list-users", help="list administrators")
    listing.set_defaults(func=cmd_list_users)

    runtime = sub.add_parser("runtime", help="control the Hermes runtime process")
    runtime.add_argument("action", choices=["status", "start", "stop", "restart"])
    runtime.set_defaults(func=cmd_runtime)

    doctor = sub.add_parser("doctor", help="report what this host can run")
    doctor.set_defaults(func=cmd_doctor)

    config = sub.add_parser("config", help="export the Control Center's own configuration")
    config.add_argument("action", choices=["export"])
    config.add_argument("--output", default="")
    config.set_defaults(func=cmd_config)

    keys = sub.add_parser("keys", help="inspect the encrypted key vault (never prints values)")
    keys.add_argument("action", choices=["list", "remove", "where"])
    keys.add_argument("name", nargs="?", default="")
    keys.set_defaults(func=cmd_keys)

    free = sub.add_parser("free-mode", help="show or change FREE MODE")
    free.add_argument("action", choices=["status", "on", "off"])
    free.add_argument("--yes", action="store_true", help="required to turn FREE MODE off")
    free.set_defaults(func=cmd_free_mode)

    reset = sub.add_parser("reset-db", help="move the current database aside and start with an empty one")
    reset.add_argument("--yes", action="store_true", help="confirm the reset (required)")
    reset.set_defaults(func=cmd_reset_db)

    routes = sub.add_parser("free-routes", help="print the configured free-first fallback chain")
    routes.set_defaults(func=cmd_free_routes)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except HumanError as exc:
        print(f"{exc.payload.get('title', 'Failed')}: {exc.payload.get('message', '')}", file=sys.stderr)
        hint = exc.payload.get("hint")
        if hint:
            print(f"hint: {hint}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
