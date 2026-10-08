# Keeping up with upstream Hermes

Hermes Agent is an **upstream dependency** of this project, not a fork. That is a deliberate engineering decision: the
agent moves quickly, and a Control Center that has forked it becomes unmaintainable within weeks.

This document defines the update strategy, the exact boundaries, and what to do when upstream changes something.

---

## The rule

```
UPSTREAM HERMES  →  INTEGRATION LAYER  →  BACKEND  →  WEB UI
 github.com          app/hermes/*         app/routes/*   frontend/src
 (Nous Research)      ← fixes go here
```

1. **Hermes is never modified.** The checkout at `CC_HERMES_SOURCE` stays a clean clone of
   `github.com/NousResearch/hermes-agent`. Running `git -C $CC_HERMES_SOURCE status` must show a clean tree; if an
   upstream file ever shows as modified, that is a bug in this project.
2. **All integration lives in `control-center/`.** The integration layer (`backend/app/hermes/`) is the *only* place that
   knows about Hermes' internals.
3. **Only public surfaces are used:**
   - the dashboard/runtime HTTP API on `127.0.0.1:$CC_HERMES_PORT`, authenticated with the session token upstream
     publishes in its rendezvous record (`$XDG_STATE_HOME/hermes/gateway-locks/host-serve.{json,token}`);
   - the CLI entry points `hermes_cli.main dashboard …` and `… gateway run --external-supervisor`;
   - the agent's OpenAI-compatible API on `127.0.0.1:$CC_CHAT_BRIDGE_PORT` (`/v1/runs`, `/v1/models`) inside the gateway
     process, authenticated with our own `API_SERVER_KEY`;
   - configuration and secrets files it owns: `$HERMES_HOME/config`, `$HERMES_HOME/.env`, and the gateway lock directory.
4. **Nothing is vendored.** `pip install -e /opt/hermes-agent` (Docker) or your existing virtualenv: the agent keeps its
   own release cycle, its own extras, and its own updater.

---

## How to update Hermes

### Option A — let Hermes update itself (preferred)

**Settings → Updates → Apply** calls the proxy route

```
Control Center  POST /api/cc/update/apply
                        └──► Hermes  POST /api/hermes/update
```

Upstream performs its own update (it knows whether it was installed from a wheel, a git checkout or an installer), and
the Control Center only *asks* it to. `GET /api/cc/update/status` (→ `GET /api/hermes/update/check`) shows the pinned
ref and what is available before you press anything.

### Option B — Docker

```bash
cd control-center
docker compose -f docker/docker-compose.yml build --build-arg HERMES_REF=v0.9.3
docker compose -f docker/docker-compose.yml up -d
```

`HERMES_REF` accepts a tag, branch or commit. Pin tags in production; use `main` only if you are happy to chase:

- Docker images are reproducible from `HERMES_REF`, so a rollback is "rebuild with the previous ref".

### Option C — manual checkout

```bash
cd /path/to/hermes-agent
git fetch --tags
git checkout <tag-or-commit>
/path/to/hermes-agent/venv/bin/python -m pip install -e ".[web]"
```

Then restart the Control Center (it starts the runtime itself), or press **Dashboard → Restart**.

---

## Updating this project

```bash
cd control-center
git pull
cd frontend && npm ci && npm run build      # rebuild the SPA
cd ../backend && python -m pip install -e . # only if pyproject.toml changed
python -m app.cli doctor                    # sanity check after the update
```

Your data is not affected: it lives in `CC_HOME` (database, vault, logs, backups) and `$HERMES_HOME` (memory, skills,
sessions). Code updates never migrate or delete it. Schema changes are additive and guarded by `verify_schema()`.

---

## After every upstream update: the 60-second check

```bash
python -m app.cli doctor          # install + capability report
```

or in the UI: **Diagnostics → Re-check**. Then, if the agent's behaviour changed:

1. `Runtime` starts? (`Dashboard`, `python -m app.cli runtime status`)
2. `/api/cc/status/detail` shows `hermes.reachable: true`?
3. Chat turns reach the agent (`Chat → Test inference`)?
4. Sessions/skills/tools lists still return data (they pass through the proxy allowlist)?

The automated equivalent is:

```bash
CC_LIVE=1 CC_HERMES_SOURCE=/path/to/hermes-agent \
CC_HERMES_PYTHON=/path/to/hermes-agent/venv/bin/python \
python -m pytest tests/test_live_end_to_end.py -q
```

---

## What to do when upstream changes something

Work outward from the failing symptom; the file to edit is almost always in the integration layer.

| Symptom | Where the fix belongs |
|---|---|
| A new/changed runtime endpoint | `backend/app/routes/hermes_proxy.py` — add or correct the entry in `READ_ROUTES` / `WRITE_ROUTES` |
| The runtime's session token handling changed | `backend/app/hermes/supervisor.py` (spawn + token) and `backend/app/hermes/rendezvous.py` (record parsing) |
| The rendezvous record format changed | `backend/app/hermes/rendezvous.py` — it validates `protocolVersion` and refuses to guess |
| The gateway CLI flags changed | `backend/app/hermes/gateway.py` — the argv is in one place |
| The api_server platform config/shape changed | `backend/app/routes/chat.py` (`bridge_config`, `bridge_enable`) |
| Provider ids or pricing tiers changed | `backend/app/hermes/provider_catalog.py` + `backend/app/hermes/data/provider_registry.json` (`tools/dump_provider_registry.py` regenerates the latter from `plugins/model-providers/*`) |
| Local runtime / model catalogue changed | `backend/app/hermes/localcap.py` |
| The self-update API changed | the proxy entry for `/update/apply` + `backend/app/hermes/updater.py` |
| The dashboard refuses to start (locking, profiles) | `backend/app/hermes/supervisor.py` — we run `dashboard --isolated` and adopt via the rendezvous record when offered |

**Never** patch inside the Hermes checkout to make something work. If the only possible fix really is upstream, that is an
upstream bug report, not a local fork.

### Guardrails that catch drift

- `backend/tests/test_hermes_passthrough.py` — asserts the proxy allowlist is exactly the documented set (nothing that
  can read the agent's files or execute a shell is exposed).
- `backend/tests/test_live_end_to_end.py` — runs against a real runtime: status, sessions, skills, toolsets, gateway
  bring-up and a real chat turn. Run it after every Hermes update.
- `backend/tests/test_contract.py` — fails the build if the UI calls an endpoint that no longer exists.

---

## Rollback

| Thing | Rollback |
|---|---|
| Hermes (Docker) | rebuild with the previous `HERMES_REF` |
| Hermes (self-update) | `git checkout <previous-tag>` in the checkout, reinstall `-e`, restart (**Settings → Updates** shows the pinned ref) |
| Hermes (manual) | `git checkout <previous-commit>` in the checkout, reinstall, restart |
| This app | `git checkout <previous-commit>` in `control-center/`, rebuild the UI, restart |
| Settings | restore an export or a backup archive (Settings → Import / Backups) |

Your data is orthogonal to all of the above: `CC_HOME` and `$HERMES_HOME` are reused by whichever version you run.
Take a backup first if a downgrade might have migrated something (upstream migrations are not always reversible).

---

## Versioning policy

- The Control Center reports the **real** commit, commit date and licence presence of the checkout it is running with
  (**Settings → About**, `GET /api/cc/settings/about`). It never guesses a version.
- Hermes' own `pyproject.toml` declares `version = "0.0.0"`, so the Control Center shows the git commit and Hermes'
  reported release date instead of a meaningless number.
- Compatibility is asserted by tests, not by a version range: anything the live suite covers is supported; anything
  outside it is unverified and reported as such.
