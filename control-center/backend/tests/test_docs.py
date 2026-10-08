"""Documentation is checked like code.

Docs drift silently: a renamed endpoint, a removed CLI command or a moved file turns
a guide into a trap. These tests read the Markdown and assert that everything it
points at still exists — and that nothing secret leaked into it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[2]
DOCS = sorted((PROJECT / "docs").glob("*.md")) + [PROJECT / "README.md"]
DOC_PATHS = [path for path in DOCS if path.exists()]

# Public, unauthenticated routes that exist outside the documented API surface.
PUBLIC_ROUTES = {"/health", "/status", "/api/status"}

# Variables that docker-compose or the image build interpolate themselves.
COMPOSE_ONLY = {"CC_BIND", "CC_MEMORY_LIMIT", "HERMES_REF", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"}
SYSTEM_VARS = {"TZ"}

LINK = re.compile(r"\]\((?!https?://|mailto:|#)([^)]+)\)")
API_PATH = re.compile(r"`(?:GET|POST|PUT|PATCH|DELETE)?\s*(/api/cc/[A-Za-z0-9/_{}.-]+)`")
ENV_VAR = re.compile(r"\b((?:CC|HERMES)_[A-Z0-9_]+)\b")
CLI_CALL = re.compile(r"python -m app\.cli ([a-z-]+)")


def test_the_documented_files_all_exist():
    assert (PROJECT / "README.md").is_file(), "the project needs a root README"
    for name in ("UPSTREAM-UPDATE", "DEPLOYMENT", "FREE-MODE", "SECURITY", "MIGRATION"):
        assert (PROJECT / "docs" / f"{name}.md").is_file(), f"docs/{name}.md is required"


def test_relative_links_resolve():
    broken: list[str] = []
    for document in DOC_PATHS:
        for target in LINK.findall(document.read_text(encoding="utf-8")):
            anchor = target.split("#", 1)[0]
            if not anchor:
                continue
            if not (document.parent / anchor).exists():
                broken.append(f"{document.name} → {target}")
    assert not broken, "documentation links must resolve:\n  " + "\n  ".join(broken)


def test_documented_api_paths_exist(app):
    _client, application = app
    known = {re.sub(r"\{[^}]*\}", "{param}", path.rstrip("/") or "/") for path in application.openapi()["paths"]}
    known |= {re.sub(r"\{[^}]*\}", "{param}", path) for path in PUBLIC_ROUTES}

    missing: list[str] = []
    for document in DOC_PATHS:
        for path in API_PATH.findall(document.read_text(encoding="utf-8")):
            normalised = re.sub(r"\{[^}]*\}", "{param}", path.rstrip("/"))
            if normalised not in known:
                missing.append(f"{document.name}: {path}")
    assert not missing, "documentation names endpoints that do not exist:\n  " + "\n  ".join(missing)


def test_documented_cli_commands_exist():
    from app.cli import build_parser

    parser = build_parser()
    subparsers = next(
        action for action in parser._actions if hasattr(action, "choices") and action.choices and action.dest == "command"
    )
    available = set(subparsers.choices)

    missing: list[str] = []
    for document in DOC_PATHS:
        for command in CLI_CALL.findall(document.read_text(encoding="utf-8")):
            if command not in available:
                missing.append(f"{document.name}: app.cli {command}")
    assert not missing, "documentation names CLI commands that do not exist:\n  " + "\n  ".join(missing)


def test_documented_environment_variables_exist():
    config = (PROJECT / "backend" / "app" / "config.py").read_text(encoding="utf-8")
    real = set(re.findall(r'_env(?:_int|_bool|_float|_path|_list)?\(\s*"([A-Z0-9_]+)"', config))
    # Variables the test-suite reads (e.g. the live-test switch) are real too.
    for source in (PROJECT / "backend" / "tests").rglob("*.py"):
        real |= set(re.findall(r'(?:getenv|environ\.get|environ\[\s*)\s*\(\s*"(CC_[A-Z0-9_]+)"', source.read_text(encoding="utf-8")))
        real |= set(re.findall(r'setenv\(\s*"(CC_[A-Z0-9_]+)"', source.read_text(encoding="utf-8")))
    known = real | COMPOSE_ONLY | SYSTEM_VARS

    allowed_foreign = {
        "HERMES_HOME",  # the agent's own variable, referenced by name on purpose
        "HERMES_SOURCE_DIR",
        "HERMES_GUEST_ONBOARDING",
        "HERMES_NONINTERACTIVE",
        "HERMES_DASHBOARD_SESSION_TOKEN",
        "HERMES_WEB_DIST",
        "HERMES_REF",
        "HERMES_PYTHON",
    }
    unknowns: set[str] = set()
    for document in DOC_PATHS:
        for name in ENV_VAR.findall(document.read_text(encoding="utf-8")):
            if name in known or name in allowed_foreign:
                continue
            if name.startswith("CC_") or name == "HERMES_HOME":
                unknowns.add(f"{document.name}: {name}")
    assert not unknowns, "documentation names environment variables nothing reads:\n  " + "\n  ".join(sorted(unknowns))


def test_no_document_contains_a_secret_shaped_value():
    patterns = [
        (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"), "an OpenAI-style key"),
        (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{12,}"), "an Anthropic-style key"),
        (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"), "a Google API key"),
        (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "a GitHub token"),
        (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a private key"),
        (re.compile(r"\bcc_[A-Za-z0-9]{20,}"), "a generated bridge key"),
    ]
    found: list[str] = []
    for document in DOC_PATHS:
        text = document.read_text(encoding="utf-8")
        for pattern, label in patterns:
            if pattern.search(text):
                found.append(f"{document.name}: {label}")
    assert not found, "a secret-shaped value in the docs:\n  " + "\n  ".join(found)


def test_no_state_or_env_file_is_committed():
    """The project directory must not carry state, keys or a real .env."""

    forbidden = {".env", "secret.key", "secrets.key", "session.secret", "control-center.db"}
    offenders: list[str] = []
    for path in PROJECT.rglob("*"):
        if "node_modules" in path.parts or path.name in {".gitignore", ".env.example"}:
            continue
        if path.is_file() and (path.name in forbidden or path.suffix in {".db", ".sqlite", ".sqlite3"}):
            offenders.append(str(path.relative_to(PROJECT)))
    assert not offenders, "these must never be part of the project tree:\n  " + "\n  ".join(offenders)


def test_gitignore_protects_the_things_that_matter():
    text = (PROJECT / ".gitignore").read_text(encoding="utf-8")
    lines = {line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")}
    for required in (".env", "!.env.example", "*.key", "*.db", "logs/", "backups/", "node_modules/", "frontend/build/"):
        assert required in lines, f".gitignore must contain {required}"
    assert not any(line == "!.env" for line in lines), "the .env exception must not exist"


@pytest.mark.parametrize("phrase", ["FREE", "SELF-HOSTED", "LIMITED FREE TIER", "OPTIONAL PAID", "REQUIRES EXTERNAL SERVICE"])
def test_the_honesty_labels_are_defined(phrase):
    """The five labels must be explained in the docs, not just used."""

    corpus = "\n".join(document.read_text(encoding="utf-8") for document in DOC_PATHS)
    assert phrase in corpus, f"the label {phrase} must appear in the documentation"
    assert re.search(rf"\|[^|\n]*{re.escape(phrase)}[^|\n]*\|", corpus), f"{phrase} should be explained in a table"


def test_the_readme_covers_the_deliverable_topics():
    readme = (PROJECT / "README.md").read_text(encoding="utf-8").lower()
    for topic in (
        "attribution",
        "architecture",
        "installation",
        "free mode",
        "api-key security",
        "authentication",
        "docker deployment",
        "free-hosting limitations",
        "updating hermes from upstream",
        "backup, export and import",
        "troubleshooting",
        "security notes",
        "known limitations",
        "exactly what could cost money",
        "testing",
    ):
        assert topic in readme, f"the README should cover “{topic}”"


def test_docker_limitation_is_stated_verbatim():
    """Never claim a Docker build that did not happen."""

    corpus = "\n".join(document.read_text(encoding="utf-8") for document in DOC_PATHS)
    assert "Docker image build not executed" in corpus or "no Docker CLI" in corpus, (
        "the docs must say that the image was not built in this environment"
    )
