"""Deployment assets are code too: the Dockerfile, compose file and env template
must describe *this* application, not a remembered one.

Everything here is checked by reading the files, because a compose file that names a
variable the app ignores is worse than no compose file: it silently does nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / "docker"
COMPOSE = DOCKER / "docker-compose.yml"
DOCKERFILE = DOCKER / "Dockerfile"
ENV_TEMPLATE = ROOT / ".env.example"

# Variables compose interpolates itself, or that only exist inside the image build.
# Compose interpolates these itself, or the image build uses them.
COMPOSE_ONLY = {"CC_BIND", "CC_MEMORY_LIMIT", "HERMES_REF", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"}
# Read by the operating system inside the container, not by our code.
SYSTEM_VARS = {"TZ"}

pytest.importorskip("yaml", reason="PyYAML is only needed to validate the compose file")


def _yaml():
    import yaml

    return yaml


def _app_env_vars() -> set[str]:
    """Every environment variable the application actually reads."""

    config = (ROOT / "backend" / "app" / "config.py").read_text(encoding="utf-8")
    return set(re.findall(r'_env(?:_int|_bool|_float|_path|_list)?\(\s*"([A-Z0-9_]+)"', config))


def test_compose_file_is_valid_yaml_with_the_services_we_promise():
    document = _yaml().safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = document["services"]
    assert "control-center" in services, "the app must be the default service"
    assert "postgres" in services
    assert services["postgres"].get("profiles") == ["postgres"], "the paid-adjacent extra must be opt-in, not default"
    assert document["volumes"], "state must survive a container restart"


def test_compose_builds_this_repository():
    document = _yaml().safe_load(COMPOSE.read_text(encoding="utf-8"))
    build = document["services"]["control-center"]["build"]
    context = (COMPOSE.parent / build["context"]).resolve()
    dockerfile = (context / build["dockerfile"]).resolve()
    assert context == ROOT, f"compose context should be {ROOT}, got {context}"
    assert dockerfile == DOCKERFILE, f"compose should build {DOCKERFILE}, got {dockerfile}"


def test_build_context_is_the_project_directory_so_dockerignore_applies():
    """Docker only reads the .dockerignore that sits at the *context root*.

    With the repository root as the context, Docker would read upstream Hermes'
    .dockerignore — which excludes every ``*.md`` and does not know about our
    state directories — while ``control-center/.dockerignore`` would be inert.
    The image needs neither the Hermes checkout (it clones upstream itself) nor
    anything outside this directory, so the context stays here.
    """

    document = _yaml().safe_load(COMPOSE.read_text(encoding="utf-8"))
    context = (COMPOSE.parent / document["services"]["control-center"]["build"]["context"]).resolve()
    assert context == ROOT, f"context must be {ROOT}, got {context}"
    assert (ROOT / ".dockerignore").is_file(), "the context root needs a .dockerignore"
    assert (ROOT.parent / ".gitignore").is_file(), "the upstream checkout is the repository root"

    upstream = (ROOT.parent / ".dockerignore").read_text(encoding="utf-8")
    assert "*.md" in upstream, (
        "upstream .dockerignore excludes Markdown; if the context ever becomes the "
        "repository root again, the docs and README would silently vanish from it"
    )


def test_dockerfile_paths_are_relative_to_the_project_context():
    """Every COPY source must exist under the build context."""

    text = DOCKERFILE.read_text(encoding="utf-8")
    sources = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("COPY ", "ADD ")):
            continue
        parts = stripped.split()
        if len(parts) < 3:
            continue
        # ``COPY --from=<stage>`` sources live in another stage, not in the context.
        if any(part.startswith("--from") for part in parts):
            continue
        sources.extend(part for part in parts[1:-1] if not part.startswith("--"))
    local = [source for source in sources if not source.startswith(("http://", "https://"))]
    assert local, "the Dockerfile should copy the project into the image"

    missing = []
    for source in local:
        candidate = ROOT / source.rstrip("*").rstrip("/")
        if candidate.exists():
            continue
        pattern = source.rstrip("*").rstrip("/")
        if pattern and not pattern.startswith("/") and list(ROOT.glob(pattern)):
            continue
        missing.append(source)
    assert not missing, f"COPY sources missing from the build context: {missing}"
    assert not any(source.startswith("control-center/") for source in local), (
        "paths are relative to control-center/, so 'control-center/' prefixes are wrong"
    )


def test_compose_publishes_the_app_but_not_the_database():
    document = _yaml().safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = document["services"]
    assert services["control-center"]["ports"], "the UI must be reachable"
    assert not services["postgres"].get("ports"), "the database must not be exposed to the host"
    assert any("/data" in volume for volume in services["control-center"]["volumes"])


def test_compose_healthcheck_matches_the_real_health_route():
    document = _yaml().safe_load(COMPOSE.read_text(encoding="utf-8"))
    service = document["services"]["control-center"]
    command = " ".join(service["healthcheck"]["test"])
    assert "/health" in command, "the healthcheck must hit the route the app actually serves"
    assert service["restart"] == "unless-stopped"
    assert "no-new-privileges:true" in service["security_opt"]


def test_every_compose_variable_is_real():
    """A compose file that sets a variable the app ignores is a silent no-op."""

    known = _app_env_vars() | COMPOSE_ONLY | SYSTEM_VARS
    text = COMPOSE.read_text(encoding="utf-8")
    referenced = set(re.findall(r"\$\{([A-Z0-9_]+)", text))
    unknown = referenced - known
    assert not unknown, f"compose interpolates variables nothing reads: {sorted(unknown)}"

    literal = set(re.findall(r"^\s{6}([A-Z][A-Z0-9_]*):", text, re.M))
    unknown_literal = {name for name in literal if name.startswith(("CC_", "HERMES_"))} - known
    assert not unknown_literal, f"compose sets variables the app ignores: {sorted(unknown_literal)}"


def test_env_template_only_names_variables_the_app_reads():
    text = ENV_TEMPLATE.read_text(encoding="utf-8")
    known = _app_env_vars() | COMPOSE_ONLY | SYSTEM_VARS
    names = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", text, re.M))
    unknown = {name for name in names if name.startswith(("CC_", "HERMES_"))} - known
    assert not unknown, f".env.example documents variables nothing reads: {sorted(unknown)}"


def test_env_template_contains_no_secret_and_forbids_them():
    text = ENV_TEMPLATE.read_text(encoding="utf-8")
    lowered = text.lower()
    assert re.search(r"\bsk-[A-Za-z0-9]", text) is None, "a key-looking value must never appear in a template"
    assert "never commit .env" in lowered
    lowered = text.lower()
    assert "do not put provider keys here" in lowered, "the template must say where keys do NOT belong"
    assert "@@" not in text, "replace the placeholder before shipping the template"


def test_dockerfile_is_pinned_and_unprivileged():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert text.startswith("# syntax=docker/dockerfile:1")
    assert "USER hermes" in text, "the container must not run as root"
    assert "useradd" in text
    assert "HEALTHCHECK" in text
    assert "VOLUME" in text
    assert "--no-install-recommends" in text, "keep the image small on purpose"
    # No secret may be baked in at build time.
    assert not re.search(r"ENV\s+\w*(KEY|TOKEN|SECRET)", text, re.I)
    assert "pip install -e" in text, "upstream Hermes must stay an installable dependency"


def test_dockerfile_clones_upstream_hermes_and_builds_our_ui():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "github.com/NousResearch/hermes-agent" in text, "attribution and provenance must be visible"
    assert "HERMES_REF" in text, "the upstream ref must be overridable without editing the file"
    assert "npm run build" in text, "the UI is built, not committed as a bundle"
    assert "--from=ui /ui/build" in text


def test_dockerfile_cmd_matches_the_real_entry_point():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert 'CMD ["python", "-m", "app"]' in text
    assert (ROOT / "backend" / "app" / "__main__.py").is_file(), "the CMD must point at something that exists"
    assert (ROOT / "backend" / "pyproject.toml").is_file(), "the backend must be installable"


def test_dockerignore_keeps_secrets_and_state_out_of_the_image():
    text = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    entries = {line.strip() for line in text if line.strip() and not line.startswith("#")}
    for expected in (".git", ".env", "**/node_modules", "*.db", "**/build"):
        assert expected in entries, f".dockerignore should exclude {expected}"
    assert "!.env.example" in entries, "the template is safe to include"


def test_dockerignore_keeps_the_build_context_small_and_secret_free():
    """The image is built from control-center/, so this file is what docker reads."""

    path = ROOT / ".dockerignore"
    assert path.is_file(), "the build context needs a .dockerignore"
    rules = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    text = "\n".join(rules)

    for required in ("node_modules", "**/node_modules", "**/build", ".env", "*.db"):
        assert required in rules or required in text, f".dockerignore must exclude {required}"
    # The template must survive: the docs tell people to copy it.
    assert "!.env.example" in rules, ".env.example has to stay in the context"

    # And it must not exclude anything the Dockerfile copies in.
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    for needed in ("frontend/package.json", "backend/pyproject.toml"):
        assert needed in dockerfile, f"the Dockerfile should copy {needed}"
    assert not any(rule.rstrip("/") in {"frontend", "backend", "src"} for rule in rules), (
        "the .dockerignore must not exclude the sources the image needs"
    )
