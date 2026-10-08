#!/usr/bin/env python3
"""Regenerate app/hermes/data/provider_registry.json from an upstream checkout.

Why a generated file instead of parsing upstream at runtime:

* the Control Center must not import Hermes (it has no business loading the whole
  agent just to know that OpenRouter uses OPENROUTER_API_KEY);
* provider lists change upstream, so we want a *snapshot* we can diff in review;
* anything we cannot verify stays "unknown" in our own classification layer.

Usage
-----
    python tools/dump_provider_registry.py --source /path/to/hermes-agent

The output is committed so a fresh clone works with no checkout present; re-run it
after `git pull` in the Hermes checkout to pick up new providers.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

FIELDS = ("name", "display_name", "default_model")
FLAGS = ("supports_model_listing", "supports_health_check")


def extract(plugin_dir: pathlib.Path) -> dict | None:
    init = plugin_dir / "__init__.py"
    if not init.exists():
        return None
    text = init.read_text(encoding="utf-8", errors="ignore")
    entry: dict = {"dir": plugin_dir.name}
    for field in FIELDS:
        match = re.search(rf"{field}\s*=\s*[\"']([^\"']+)[\"']", text)
        if match:
            entry[field] = match.group(1)
    match = re.search(r"env_vars\s*=\s*\(([^)]*)\)", text)
    if match:
        entry["env_vars"] = re.findall(r"[\"']([^\"']+)[\"']", match.group(1))
        match = re.search(r"env_vars\s*=\s*[\"']([^\"']+)[\"']", text)
    if "env_vars" not in entry:
        match = re.search(r"env_vars\s*=\s*[\"']([^\"']+)[\"']", text)
        if match:
            entry["env_vars"] = [match.group(1)]
    for flag in FLAGS:
        match = re.search(rf"{flag}\s*=\s*(True|False)", text)
        if match:
            entry[flag] = match.group(1) == "True"
    match = re.search(r"base_url\s*=\s*[\"']([^\"']+)[\"']", text)
    if match:
        entry["base_url"] = match.group(1)
    return entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="path to the hermes-agent checkout")
    parser.add_argument("--out", default=str(pathlib.Path(__file__).resolve().parents[1] / "app/hermes/data/provider_registry.json"))
    args = parser.parse_args()

    root = pathlib.Path(args.source).expanduser().resolve() / "plugins" / "model-providers"
    if not root.is_dir():
        print(f"not a Hermes checkout: {root} does not exist", file=sys.stderr)
        return 2

    registry: dict[str, dict] = {}
    for plugin_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        entry = extract(plugin_dir)
        if entry:
            registry[plugin_dir.name] = entry

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {len(registry)} providers to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
