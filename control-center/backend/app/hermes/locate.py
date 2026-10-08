"""Locate the upstream Hermes Agent checkout and a Python that can run it.

The Control Center treats Hermes as an *external upstream dependency*. It never
imports Hermes into its own process, never patches it, and never vendors its
code. All it needs is:

* a checkout of https://github.com/NousResearch/hermes-agent, and
* an interpreter with that project's dependencies installed.

Both are discovered here and reported honestly — including the case where they
are missing, which is the normal state on a fresh machine.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

MIN_PYTHON = (3, 11)
MAX_PYTHON = (3, 15)  # exclusive: upstream declares <3.15
DEFAULT_PYTHON = (3, 14)  # what upstream develops against

#: Modules that must import for the *Hermes runtime* to start. ``hermes_cli`` proves
#: the upstream package itself is installed; the rest are direct upstream dependencies
#: (see hermes-agent/pyproject.toml) that a bare FastAPI environment will not have,
#: which is what makes this a real check rather than a guess.
REQUIRED_IMPORTS = ("hermes_cli", "openai", "rich", "httpx", "fastapi", "uvicorn", "croniter", "pydantic")

#: Needed by the upstream *API server platform* (``gateway/platforms/api_server.py``)
#: — the OpenAI-compatible surface this app uses to chat with the agent. Upstream
#: keeps it in the ``[messaging]`` extra, so the runtime boots without it but the
#: agent bridge cannot start. Reported separately, never conflated with "broken".
BRIDGE_IMPORTS = ("aiohttp",)


@dataclass
class PythonCandidate:
    path: str
    version: str = ""
    major_minor: tuple[int, int] | None = None
    supported: bool = False
    has_hermes_deps: bool = False
    error: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "version": self.version,
            "supported": self.supported,
            "has_hermes_deps": self.has_hermes_deps,
            "error": self.error,
            "note": self.note,
        }


@dataclass
class InstallReport:
    source_dir: str
    is_hermes_source: bool
    source_reason: str = ""
    version: str = ""
    commit: str = ""
    commit_date: str = ""
    license_present: bool = False
    setup_script_present: bool = False
    python_candidates: list[PythonCandidate] = field(default_factory=list)
    selected_python: str = ""
    selected_python_version: str = ""
    hermes_home: str = ""
    hermes_home_exists: bool = False
    hermes_cli_on_path: bool = False
    node_available: bool = False
    problems: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)

    @property
    def launch_commands(self) -> list[str]:
        """The exact commands this app would run to start Hermes here.

        Shown in Diagnostics and printed by ``python -m app.cli doctor`` so the
        answer to "what is it actually doing?" is never a mystery.
        """

        interpreter = self.selected_python or "python3"
        commands = [f"cd {self.source_dir}"]
        commands.append(
            f"{interpreter} -m hermes_cli.main dashboard --skip-build --no-open "
            "--host 127.0.0.1 --port <CC_HERMES_PORT>"
        )
        commands.append(f"{interpreter} -m pip install -e {self.source_dir}[web]   # only if dependencies are missing")
        return commands

    def to_dict(self) -> dict:
        return {
            "source_dir": self.source_dir,
            "is_hermes_source": self.is_hermes_source,
            "source_reason": self.source_reason,
            "version": self.version,
            "commit": self.commit,
            "commit_date": self.commit_date,
            "license_present": self.license_present,
            "setup_script_present": self.setup_script_present,
            "python_candidates": [candidate.to_dict() for candidate in self.python_candidates],
            "selected_python": self.selected_python,
            "selected_python_version": self.selected_python_version,
            "hermes_home": self.hermes_home,
            "hermes_home_exists": self.hermes_home_exists,
            "hermes_cli_on_path": self.hermes_cli_on_path,
            "node_available": self.node_available,
            "problems": self.problems,
            "hints": self.hints,
            "launch_commands": self.launch_commands,
        }


def _run(args: list[str], *, timeout: float = 20.0) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return completed.returncode, completed.stdout.strip(), completed.stderr.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def python_version(path: str) -> tuple[tuple[int, int] | None, str, str]:
    code, out, err = _run([path, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"])
    if code != 0 or not out:
        return None, "", err or "could not run this interpreter"
    parts = out.splitlines()[-1].strip().split(".")
    try:
        return (int(parts[0]), int(parts[1])), out.strip(), ""
    except (IndexError, ValueError):
        return None, out.strip(), "unparsable version output"


def has_runtime_dependencies(path: str, source_dir: Path) -> tuple[bool, str]:
    """Does this interpreter have what the Hermes web backend needs?"""

    probe = (
        "import importlib.util, sys;"
        f"sys.path.insert(0, {str(source_dir)!r});"
        f"missing=[name for name in {list(REQUIRED_IMPORTS)!r} if importlib.util.find_spec(name) is None];"
        "print('missing:' + ','.join(missing) if missing else 'missing:')"
    )
    code, out, err = _run([path, "-c", probe], timeout=45)
    if code != 0:
        return False, err or "probe failed"
    missing = out.split("missing:", 1)[-1].strip()
    if missing:
        return False, f"missing packages: {missing}"
    return True, ""


def has_bridge_dependencies(path: str) -> tuple[bool, str]:
    """Can this interpreter run the agent's OpenAI-compatible API server?"""

    return _probe_imports(path, BRIDGE_IMPORTS)


def _probe_imports(path: str, names: tuple[str, ...]) -> tuple[bool, str]:
    probe = (
        "import importlib.util;"
        f"missing=[name for name in {list(names)!r} if importlib.util.find_spec(name) is None];"
        "print('missing:' + ','.join(missing) if missing else 'missing:')"
    )
    code, out, err = _run([path, "-c", probe], timeout=30)
    if code != 0:
        return False, err or "probe failed"
    missing = out.split("missing:", 1)[-1].strip()
    if missing:
        return False, f"missing packages: {missing}"
    return True, ""


def inspect_python(path: str, source_dir: Path, *, deep: bool = False) -> PythonCandidate:
    candidate = PythonCandidate(path=path)
    if not Path(path).exists():
        candidate.error = "not found"
        return candidate
    major_minor, version, error = python_version(path)
    candidate.version = version
    candidate.major_minor = major_minor
    if error:
        candidate.error = error
        return candidate
    if major_minor is None:
        candidate.error = "could not determine the interpreter version"
        return candidate
    candidate.supported = MIN_PYTHON <= major_minor < MAX_PYTHON
    if not candidate.supported:
        candidate.error = (
            f"Python {major_minor[0]}.{major_minor[1]} is outside the range upstream supports "
            f"({MIN_PYTHON[0]}.{MIN_PYTHON[1]}–{MAX_PYTHON[0]}.{MAX_PYTHON[1] - 1})"
        )
        return candidate
    if major_minor < DEFAULT_PYTHON:
        candidate.note = f"works, but upstream develops against Python {DEFAULT_PYTHON[0]}.{DEFAULT_PYTHON[1]}"
    if deep:
        ok, problem = has_runtime_dependencies(path, source_dir)
        candidate.has_hermes_deps = ok
        if not ok:
            candidate.note = (candidate.note + " • " if candidate.note else "") + problem
    return candidate


def _git_info(source_dir: Path) -> tuple[str, str]:
    if not (source_dir / ".git").exists():
        return "", ""
    code, out, _ = _run(["git", "-C", str(source_dir), "rev-parse", "--short", "HEAD"])
    commit = out if code == 0 else ""
    code, out, _ = _run(["git", "-C", str(source_dir), "log", "-1", "--format=%cs"])
    date = out if code == 0 else ""
    return commit, date


def _declared_version(source_dir: Path) -> str:
    """Best-effort version, preferring what the project itself reports."""

    for relative in ("hermes_cli/_build_info.json", "hermes_cli/build_info.py", "VERSION"):
        candidate = source_dir / relative
        if not candidate.exists():
            continue
        try:
            if candidate.suffix == ".json":
                payload = json.loads(candidate.read_text(encoding="utf-8"))
                version = payload.get("version") or payload.get("tag")
                if version:
                    return str(version)
            else:
                text = candidate.read_text(encoding="utf-8", errors="ignore")
                for token in text.replace('"', " ").replace("'", " ").split():
                    if token[:1].isdigit() and token.count(".") >= 1:
                        return token
        except (OSError, json.JSONDecodeError):
            continue
    candidate = source_dir / "pyproject.toml"
    if candidate.exists():
        try:
            for line in candidate.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.strip().startswith("version") and "=" in line:
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
    return ""


def detect_install(settings) -> InstallReport:
    source_dir = Path(settings.hermes_source)
    report = InstallReport(
        source_dir=str(source_dir),
        is_hermes_source=False,
        hermes_home=str(settings.hermes_home),
        hermes_home_exists=Path(settings.hermes_home).exists(),
        hermes_cli_on_path=bool(shutil.which("hermes")),
        node_available=bool(shutil.which("npm")),
    )

    main_py = source_dir / "hermes_cli" / "main.py"
    if not source_dir.exists():
        report.source_reason = "the configured path does not exist"
        report.problems.append(f"Hermes source directory not found: {source_dir}")
    elif not main_py.is_file():
        report.source_reason = "the directory exists but does not look like a Hermes checkout"
        report.problems.append(f"{source_dir} does not contain hermes_cli/main.py")
    else:
        report.is_hermes_source = True
        report.source_reason = "hermes_cli/main.py found"
        report.license_present = (source_dir / "LICENSE").exists()
        report.setup_script_present = (source_dir / "setup-hermes.sh").exists()
        report.version = _declared_version(source_dir)
        report.commit, report.commit_date = _git_info(source_dir)

    if not report.is_hermes_source:
        report.hints.append(
            "Clone the upstream project (it stays an updatable dependency): "
            "git clone https://github.com/NousResearch/hermes-agent && cd hermes-agent && ./setup-hermes.sh"
        )
        report.hints.append("Then point CC_HERMES_SOURCE at that directory and reload this page.")

    seen: set[str] = set()
    for path in settings.hermes_python_candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        candidate = inspect_python(path, source_dir, deep=False)
        report.python_candidates.append(candidate)

    preferred = [c for c in report.python_candidates if c.supported and c.has_hermes_deps]
    if not preferred:
        # Deep-check the supported ones only, so we do not pay the import cost for
        # interpreters we already know cannot be used.
        for candidate in report.python_candidates:
            if candidate.supported and not candidate.has_hermes_deps:
                deep = inspect_python(candidate.path, source_dir, deep=True)
                candidate.has_hermes_deps = deep.has_hermes_deps
                candidate.note = deep.note or candidate.note
        preferred = [c for c in report.python_candidates if c.supported and c.has_hermes_deps]

    if settings.hermes_python:
        explicit = [c for c in report.python_candidates if c.path == settings.hermes_python]
        if explicit:
            chosen = explicit[0]
        else:
            chosen = inspect_python(settings.hermes_python, source_dir, deep=True)
            report.python_candidates.insert(0, chosen)
    else:
        chosen = preferred[0] if preferred else next((c for c in report.python_candidates if c.supported), None)

    if chosen:
        report.selected_python = chosen.path
        report.selected_python_version = chosen.version
        if not chosen.has_hermes_deps:
            report.problems.append(
                f"{chosen.path} can run Python {chosen.version} but does not have Hermes' dependencies installed"
            )
            report.hints.append(
                "Install them into that interpreter: "
                f"{chosen.path} -m pip install -e {source_dir}[web]  (the Diagnostics page can run this for you)"
            )
    else:
        report.problems.append("No supported Python interpreter (3.11–3.14) was found for the Hermes runtime")
        report.hints.append("Install Python 3.14, then set CC_HERMES_PYTHON to its path and reload.")

    if sys.version_info[:2] < MIN_PYTHON:
        report.problems.append(f"The Control Center itself wants Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+")
    return report


def runtime_command(settings, *, host: str, port: int, python: str, web_dist: Path) -> list[str]:
    """The exact command the supervisor runs. Printed in the UI for transparency.

    ``--isolated`` tells Hermes to run a *dedicated* server instead of attaching to a
    machine-level owner. That matters when the person already runs ``hermes dashboard`` for
    themselves: we adopt that one when we can (see ``rendezvous.py``) and only ever bind our
    own port when we cannot.
    """

    command = [
        python,
        "-m",
        "hermes_cli.main",
        "dashboard",
        "--skip-build",
        "--no-open",
    ]
    if getattr(settings, "runtime_isolated", True):
        command.append("--isolated")
    command += ["--host", host, "--port", str(port)]
    return command


def runtime_env(settings, *, token: str, web_dist: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "HERMES_HOME": str(settings.hermes_home),
            "HERMES_NONINTERACTIVE": "1",
            "HERMES_DASHBOARD_SESSION_TOKEN": token,
            "HERMES_WEB_DIST": str(web_dist),
            "PYTHONUNBUFFERED": "1",
        }
    )
    # Never inherit a stale token from the developer's shell.
    env.setdefault("NO_COLOR", "1")
    if extra:
        env.update(extra)
    return env
