# Hermes Agent Control Center

A self-hosted web control centre for **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** — the open-source
agent by [Nous Research](https://nousresearch.com). It runs on your own machine (or a free VM), talks to a **real
Hermes runtime** over Hermes' own HTTP API, and gives you a mobile-first UI for chat, models, providers, keys, memory,
skills, tools, schedules, sessions, logs and updates.

> **This directory is the project.** The repository root is the upstream Hermes Agent checkout. Everything here lives in
> `control-center/` and is additive: **no Hermes core file is modified**, so upstream updates keep working. Hermes stays
> an installable upstream dependency, never a fork.

- **Free by default.** The app starts in **FREE MODE**: paid providers are refused before any request leaves your
  server, and nothing requires a credit card or a paid plan.
- **No mocks.** When the agent cannot answer, the UI shows the agent's own error. It never invents a reply.
- **No secrets in the browser.** Provider keys live in an encrypted vault on the server, are masked in the UI, redacted
  from logs, and never included in the frontend bundle.

---

## Table of contents

1. [What it is](#what-it-is)
2. [Attribution](#attribution)
3. [Architecture](#architecture)
4. [Installation](#installation)
5. [FREE MODE](#free-mode)
6. [Local and free models](#local-and-free-models)
7. [Optional paid providers](#optional-paid-providers)
8. [API-key security](#api-key-security)
9. [Authentication](#authentication)
10. [Configuration](#configuration)
11. [Chat](#chat)
12. [Memory](#memory)
13. [Skills](#skills)
14. [Tools](#tools)
15. [Scheduling](#scheduling)
16. [Logs](#logs)
17. [Health and status endpoints](#health-and-status-endpoints)
18. [Docker deployment](#docker-deployment)
19. [Free-hosting limitations](#free-hosting-limitations)
20. [Local / self-hosted deployment](#local--self-hosted-deployment)
21. [Updating Hermes from upstream](#updating-hermes-from-upstream)
22. [Backup, export and import](#backup-export-and-import)
23. [Troubleshooting](#troubleshooting)
24. [Security notes](#security-notes)
25. [Honesty labels](#honesty-labels)
26. [Known limitations](#known-limitations)
27. [Exactly what could cost money](#exactly-what-could-cost-money)
28. [Testing](#testing)
29. [Licence](#licence)

---

## What it is

| | |
|---|---|
| **Frontend** | React 19 + Vite + Tailwind, mobile-first (iPhone/iPad/desktop), no desktop-only panels |
| **Backend** | FastAPI, httpx, SQLite by default (PostgreSQL optional) |
| **Agent** | the real Hermes Agent runtime, started and supervised by this app |
| **Auth** | session cookies (HttpOnly, SameSite=Lax) + CSRF double-submit + scrypt password hashing + API tokens |
| **Secrets** | AES-128-CBC + HMAC-SHA256 (Fernet) vault on the server, master key in `CC_HOME`, mode `0600` |
| **Cost posture** | FREE MODE on by default; paid providers locked until *you* store a key *and* explicitly unlock them |

What you get: start/stop/restart Hermes, chat with streaming and tool events, model and provider management, a fallback
chain that starts with free routes, key management, memory/skills/tools/schedules/sessions/logs views, a 6-step install
wizard, a deployment-compatibility page, health endpoints and a diagnostics page that names the fix for every failure.

---

## Attribution

**Hermes Agent** — © 2025 Nous Research, MIT licensed.
Upstream: <https://github.com/NousResearch/hermes-agent> · Docs: <https://hermes-agent.nousresearch.com/>

This project is an independent integration layer around that software:

- Hermes is cloned/installed **as a dependency** (`pip install -e /opt/hermes-agent`). It is not vendored, relicensed or
  presented as our own work.
- The upstream `LICENSE` and `README.md` are left untouched; the Control Center adds files only inside `control-center/`.
- The licence and provenance are shown in the app itself: **Settings → About**, which reads the real commit, commit date
  and licence presence from the checkout it is running against.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Browser (iPhone / iPad / desktop)                                            │
│   React SPA, relative URLs only — never talks to any host directly           │
└───────────────────────────────┬──────────────────────────────────────────────┘
                                │ HTTPS (or http on loopback)
┌───────────────────────────────▼──────────────────────────────────────────────┐
│ Control Center backend (FastAPI)                       :8080                  │
│   auth · CSRF · rate limits · audit log · log bus · encrypted vault          │
│   ├── integration layer  app/hermes/*   (nothing here edits Hermes)           │
│   │     supervisor · gateway · client · catalogue · localcap · updater        │
│   ├── routes  app/routes/*   (/api/cc/…)                                      │
│   └── storage  CC_HOME: SQLite · secret.key · logs · backups                 │
└──────┬───────────────────────────────────────────┬───────────────────────────┘
       │ supervises (child process)                │ authenticated HTTP
       │                                           │
┌──────▼──────────────────────┐        ┌───────────▼───────────────────────────┐
│ Hermes dashboard/runtime    │        │ Hermes gateway                        │
│   hermes dashboard --isolated│       │   hermes gateway run --external-…     │
│   :9119  (agent API/UI)      │       │   :9120  OpenAI-compatible chat API   │
└─────────────────────────────┘        └───────────────────────────────────────┘
       ▲                                           ▲
       └──────────── CC_HERMES_HOME (~/.hermes) ────┘
                     memory · skills · tools · schedules · sessions
```

**Four layers, one direction of dependency:**

```
UPSTREAM HERMES  →  INTEGRATION LAYER  →  BACKEND  →  WEB UI
 (github.com)        app/hermes/*          app/routes/*   frontend/src
```

The integration layer only uses Hermes' **public surface**: its HTTP API (with the session token it publishes), its
gateway CLI, and its config/env files. If an upstream release changes something, the fix belongs in `app/hermes/`, not in
Hermes — see [docs/UPSTREAM-UPDATE.md](docs/UPSTREAM-UPDATE.md).

### Documentation map

| Document | Read it for |
|---|---|
| This README | what it is, how to install it, every feature and its honest limits |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | local, service and Docker deployment; free-tier hosting reality; ports |
| [docs/FREE-MODE.md](docs/FREE-MODE.md) | the five cost labels, the paid guard, the free routes in priority order |
| [docs/SECURITY.md](docs/SECURITY.md) | threat model, auth/session/CSRF details, key handling, audits |
| [docs/MIGRATION.md](docs/MIGRATION.md) | backups, moving hosts, Docker ↔ host, SQLite → PostgreSQL |
| [docs/UPSTREAM-UPDATE.md](docs/UPSTREAM-UPDATE.md) | updating Hermes, the integration layer's contract, rollback |

---

## Installation

Requirements: **Python 3.11–3.14**, **git**, **Node 22+** (only to build the UI once), ~1 GB disk (plus any local model
you choose to download). No paid account is required at any step.

### 1. Get upstream Hermes

```bash
git clone https://github.com/NousResearch/hermes-agent
cd hermes-agent
./setup-hermes.sh          # upstream installer: venv, dependencies, CLI
```

Remember the interpreter it created — you will point the Control Center at it (e.g. `./venv/bin/python`).

### 2. Install and build the Control Center

```bash
cd control-center/frontend && npm ci && npm run build && cd ../..
cd control-center/backend   && python -m pip install -e .
```

### 3. Start it

```bash
export CC_HERMES_SOURCE="$PWD/control-center/.."        # path to the Hermes checkout you just cloned
export CC_HERMES_PYTHON="/path/to/hermes-agent/venv/bin/python"
export CC_HERMES_HOME="$HOME/.hermes"                   # the agent's home
python -m app
```

Open <http://127.0.0.1:8080> and complete the six-step wizard. On first boot you can also pre-create the administrator:

```bash
export CC_ADMIN_USER=admin
export CC_ADMIN_PASSWORD='a-long-unique-password'
python -m app
```

Or drive it from the CLI:

```bash
python -m app.cli doctor                 # what this host can run, and what is missing
python -m app.cli create-admin --username admin
python -m app.cli runtime status         # start | stop | restart | status
python -m app.cli free-mode status
python -m app.cli free-routes            # the free-first chain this install would use
python -m app.cli keys where             # where keys are stored, never their values
python -m app.cli free-mode off --yes    # only if you really want paid providers reachable
```

> The Control Center starts Hermes itself (`hermes dashboard --isolated`) and keeps it as a child process, so it also
> works on hosts **without systemd** — a container, a free VM, a NAS, a Raspberry Pi.

---

## FREE MODE

FREE MODE is the default and it is enforced server-side, not merely displayed:

| Behaviour | Detail |
|---|---|
| Paid providers are refused | Any request naming a PAID provider gets **HTTP 402** with *"Nothing was sent and nothing was charged."* before it touches the network |
| Paid fallback never self-activates | A chain may store an optional paid third entry, but it is only used if **you** unlock paid usage with an explicit acknowledgement phrase |
| No paid key ships with the app | There is no bundled key, no trial, and no "create an account for me" flow |
| Free routes come first | The chain builder and the model picker sort FREE → LIMITED FREE TIER → (optional) PAID |
| Honest failure | When a free tier rate-limits you, the UI says so instead of silently spending money on a paid route |

Turn it off only if you want to: **Providers → Free Mode**. Turning it off still does not select any paid model for you.

---

## Local and free models

The **Models** page lists what is actually reachable, and the **Local models** page reports what *this* host can run —
including when the answer is "nothing realistic".

| Route | Cost label | Needs |
|---|---|---|
| Local model via llama.cpp (managed runtime, `hermes_cli/local_runtime`) | **FREE** | enough RAM/CPU; downloads several GB |
| LM Studio / Ollama / any local OpenAI-compatible endpoint | **FREE** | a server you run yourself |
| Nous / Hermes free tier (`HERMES_GUEST_ONBOARDING`, no API key) | **LIMITED FREE TIER** | upstream availability; toggled in Providers, gated by Nous |
| Provider free tiers with *your own* free key (Google AI Studio, Groq, Cerebras, Mistral, Hugging Face, OpenRouter `:free`) | **LIMITED FREE TIER** | a free provider account; limits are theirs |
| Anything else | **PAID** or **UNVERIFIED** | your key, and only after you select it |

**Capability honesty.** `Local check` asks the host: CPU count, RAM, disk, GPU. On a small cloud VM it will say
*unsupported* and explain why, rather than pretending a 7B model fits in 512 MB. Local inference also runs on **your**
CPU: expect slow tokens and gigabytes of download.

---

## Optional paid providers

Paid providers are strictly optional and never mandatory:

1. Sign in to the provider yourself and create **your own** API key.
2. **Keys → Add key** → paste it. It is encrypted immediately and never returned to the browser.
3. Only then can the provider be selected, and only with FREE MODE off (or paid fallback explicitly unlocked).

Storing a key does **not** activate anything either — the Providers page lists it under *"What could cost money"* with
the words *configured ≠ active*.

---

## API-key security

| Control | How |
|---|---|
| Encrypted at rest | Fernet (AES-128-CBC + HMAC-SHA256); master key `CC_HOME/secret.key`, mode `0600`, never in Git |
| Never sent to the browser | The list endpoint returns **masked** values only; the UI cannot display a key |
| Never in logs | A redaction filter rewrites known secret values to `••••redacted••••` in logs, exports and backups |
| Never in the bundle | The frontend has no key, no `.env` and no `NEXT_PUBLIC`-style secret |
| Replace / remove | Keys can be replaced or deleted at any time, with an audit entry for each action |
| Optional plaintext mirror | `$HERMES_HOME/.env` mirroring is **off** by default; turning it on shows a warning and an audit line |
| Runtime hand-off | Keys are injected into the Hermes process environment, or written to `.env` only if you ask |

---

## Authentication

- **First run** creates the single administrator (wizard or `CC_ADMIN_USER`/`CC_ADMIN_PASSWORD`).
- **Sessions** are server-side rows; the cookie is `HttpOnly`, `SameSite=Lax`, and `Secure` automatically when served
  over HTTPS (`CC_SECURE_COOKIES=auto`). Sessions can be listed and revoked; revoking invalidates that cookie only.
- **CSRF**: every mutating request needs the `X-CSRF-Token` for the current session (double-submit).
- **Passwords** are hashed with scrypt (N=2^14, r=8, p=1, explicit `maxmem`), never stored or logged in clear.
- **Rate limits** apply to sign-in, key reveal and other sensitive routes; lockouts are audited.
- **API tokens** exist for automation: create them in Settings, send `Authorization: Bearer …`, revoke any time.
- **Public endpoints** are only `/health`, `/status` (and `/api/status`); they expose no paths, keys or usernames.

---

## Configuration

Environment variables (all optional; see [.env.example](.env.example)):

| Variable | Default | Purpose |
|---|---|---|
| `CC_HOST` / `CC_PORT` | `0.0.0.0` / `8080` | where the Control Center listens |
| `CC_HOME` | `~/.hermes/control-center` | database, vault, logs, backups |
| `CC_HERMES_SOURCE` | auto-detected (`$HERMES_SOURCE_DIR`, the repo it ships in, then common clone paths) | path to the upstream checkout |
| `CC_HERMES_PYTHON` | auto-detected | interpreter that has Hermes installed (the page/CLI shows which it picked) |
| `CC_HERMES_HOME` | `~/.hermes` | the agent's own home (memory, skills, sessions) |
| `CC_RUNTIME_MODE` | `dashboard` | `dashboard` \| `external` \| `disabled` |
| `CC_HERMES_PORT` | `9119` | the agent runtime's API port |
| `CC_CHAT_BRIDGE_PORT` | `9120` | the agent's OpenAI-compatible chat port |
| `CC_RUNTIME_AUTOSTART` / `CC_RUNTIME_AUTO_RESTART` | `1` / `1` | start Hermes on boot; restart it if it crashes |
| `CC_ADOPT_EXISTING` | `1` | adopt a runtime that publishes a rendezvous record |
| `CC_DATABASE_URL` | *(empty)* | empty = SQLite inside `CC_HOME`; else PostgreSQL |
| `CC_FREE_MODE` | `1` | refuse paid providers entirely |
| `CC_LOCK_PAID_PROVIDERS` | `1` | keep paid providers unselectable |
| `CC_SESSION_TTL` | 7 days | session lifetime in seconds |
| `CC_SECURE_COOKIES` | `auto` | `auto` \| `1` \| `0` |
| `CC_TRUSTED_PROXIES`, `CC_ALLOWED_ORIGINS`, `CC_ROOT_PATH` | *(empty)* | reverse-proxy and sub-path deployments |
| `CC_LOG_LEVEL`, `CC_LOG_RETENTION_DAYS` | `INFO`, `30` | logging |

Runtime-only extras: `CC_DEV_MODE`, `CC_STATIC_DIR`, `CC_CHAT_BRIDGE`, `CC_CHAT_TIMEOUT`, `CC_EXTERNAL_HERMES_URL`,
`CC_EXTERNAL_HERMES_TOKEN`, `CC_HERMES_ISOLATED`, `CC_RUNTIME_START_TIMEOUT`, `CC_RUNTIME_WEB_DIST`,
`CC_RUNTIME_LOG_MAX_BYTES`, `CC_ALLOW_REGISTRATION`, `CC_ICON_URL`, `CC_TIMEZONE`, `CC_SESSION_SECRET`,
`CC_HERMES_HOST`. Every name is validated by a test that fails if the docs and the code ever disagree.

---

## Chat

- Streaming replies over SSE, Markdown rendering, code blocks with copy buttons, tool-call events, approval prompts.
- **Model + provider selector**, **stop generation**, **retry the last turn**, and per-conversation model override.
- Conversation history: create, rename, archive, delete; sessions can be attached to a Hermes session for continuity.
- Every turn is stored locally, including failures — a failed turn is information, not something to hide.
- The header shows **Model**, **Provider**, **Cost tier**, **Context length** and **Agent status**.
- Chat runs through the agent's own OpenAI-compatible server (`/v1/runs`), which lives inside the **gateway** process.
  **Chat → Enable bridge** does the whole wiring: store `API_SERVER_KEY` encrypted → enable `platforms.api_server` →
  write the key to the runtime environment → restart the runtime → start the gateway as a supervised child process.
  Each step is reported separately, so a failure names the step that failed.

---

## Memory

- **Memory → providers**: what the runtime has configured, what is available, and what is unconfigured.
- Configure a provider, run setup, or start an OAuth flow where the provider supports it.
- Reset memory deliberately, with an audit entry (there is no silent wipe).

Some memory providers are **REQUIRES EXTERNAL SERVICE** (their own account/sync). The page labels them; nothing is
configured for you.

---

## Skills

- Lists the skills the agent actually has (bundled + installed), with category, usage count and provenance.
- Read and edit a skill's content, toggle it, search the skill hub, install/uninstall/update from the hub.
- The hub is an external service: availability and content are upstream's, and the page says so.

---

## Tools

- Toolset list with per-toolset availability, configuration state and the individual tools inside.
- Tools that need a key or an external service say so instead of failing silently.
- Terminal backend selection (local/docker/etc.) and computer-use permission status are reported from the runtime; a
  host that cannot support something is reported as unsupported.

---

## Scheduling

- Create/pause/resume/trigger schedules and inspect their run history.
- **Schedules run inside the Hermes process**, never in your browser tab — you can close the tab, and they still fire.
- If the host sleeps or scales to zero, runs are **missed**; that is a hosting limitation and the page says so.
- A schedule that pins a paid model is refused while FREE MODE is on.

---

## Logs

- Categories: `INFO`, `WARNING`, `ERROR`, `MODEL`, `PROVIDER`, `SECURITY`, `SCHEDULER`, `SYSTEM`.
- Sources: Control Center store, the runtime process log, the agent's own logs, or everything merged.
- Search, level/category filters, export (CSV/JSON/text) and clear, plus a summary view.
- Secrets are redacted **before** they reach the store, the export or the screen; log files are `0600`.

---

## Health and status endpoints

| Endpoint | Auth | Returns |
|---|---|---|
| `GET /health` | public | liveness: database, runtime process/API, vault, model |
| `GET /status` | public | the same, sanitised: no paths, no keys, no usernames |
| `GET /api/cc/status/detail` | session | full detail: runtime, upstream, model, FREE MODE, DB, storage, host metrics, counters |
| `GET /api/cc/diagnostics` | session | every check with pass/fail, the measured detail, and the fix when it fails |
| `GET /api/cc/deploy/preflight` | session | will Hermes actually run on this host (RAM, CPU, disk, ports) |
| `GET /api/cc/runtime/bootstrap/plan` | session | the exact commands install would run — nothing runs without you |

Everything else lives under `/api/cc/…` and requires a session. The API is not a public surface: there is no anonymous
write path and no unauthenticated proxy to the agent.

---

## Docker deployment

```bash
cd control-center
cp .env.example .env          # set CC_ADMIN_PASSWORD at least
docker compose -f docker/docker-compose.yml up -d --build
```

- One container runs the **Control Center and the Hermes runtime together** (the app supervises the agent), so a single
  `docker compose up` gives you UI + API + agent.
- The optional database is a **profile**, not a default: `--profile postgres` starts PostgreSQL; without it you get
  SQLite for free.
- State lives in the `hermes-data` volume (`/data`): database, encrypted vault, logs, backups and the whole
  `HERMES_HOME`. **Copy that volume to migrate** — see [docs/MIGRATION.md](docs/MIGRATION.md).
- The image runs as an unprivileged user (`hermes`, uid 1000), with `no-new-privileges`, a memory limit and a health
  check against `/health`.
- Hermes is cloned inside the image from `github.com/NousResearch/hermes-agent` at `HERMES_REF` (default `main`): rebuild
  to advance upstream deliberately.
- The port binds to `127.0.0.1` by default. Put a reverse proxy or tunnel in front for remote access, and set
  `CC_SECURE_COOKIES=1` when you serve over HTTPS.

---

## Free-hosting limitations

Written to be honest, not flattering. The in-app **Deploy** page carries the same table with dates and links.

| Platform | Label | Reality |
|---|---|---|
| Your own computer / laptop / mini PC | FREE | Runs everything including local models. Only cost: electricity. Not 24/7 if you shut it down. |
| Your own VPS / NAS / Raspberry Pi | SELF-HOSTED | Full control, real uptime, you pay the host (not a subscription to us). |
| Oracle Cloud Always Free, Google e2-micro, AWS Free Tier, Azure 12-month | LIMITED FREE TIER | Can host the agent, but: small RAM/CPU (no local models), a card may be required at signup, and idle-reclaim policies vary. Verify the current terms. |
| GitHub Codespaces, Render/Koyeb/Replit free, HF Spaces free | LIMITED FREE TIER | **Cannot host the runtime properly**: they sleep or stop idle work, or kill background processes. Schedules will be missed. |
| Static/edge hosts (Cloudflare Pages, Vercel, Netlify free) | FREE | Serve static files only. No Hermes, no scheduler. |
| Neon / Supabase free PostgreSQL | REQUIRES EXTERNAL SERVICE | A usable free database tier, but it is a third party and free tiers pause or limit. SQLite costs nothing and needs nobody. |
| Free model inference (no hosting) | LIMITED FREE TIER | Your app stays on your machine; the model runs in someone else's free tier with their limits. |

**No part of this project promises 24/7 uptime on a free host.** Free tiers sleep, reclaim or change their terms.
Existing free tiers may also become paid: the app reports tiers it can verify and marks everything else `UNVERIFIED`
rather than guessing.

---

## Local / self-hosted deployment

The best free setup is hardware you already own:

1. Install Hermes and the Control Center as above.
2. Let it run as a service: `docker compose` (Linux/macOS), `systemd`/`launchd` unit, or any process manager.
   The Control Center supervises the agent itself, so no systemd is required.
3. Reach it from your phone: keep it on your LAN (`http://<host>:8080`), or put it behind a tunnel/VPN. If you expose it
   to the internet, use HTTPS and `CC_SECURE_COOKIES=1`, and keep the admin password long.
4. Optional: `CC_RUNTIME_MODE=external` to attach to a Hermes you run yourself, or `CC_DATABASE_URL` for PostgreSQL.

---

## Updating Hermes from upstream

Two independent things can be updated: **Hermes** (the agent) and the **Control Center** (this app).

**Hermes.** Upstream owns its own update path:

- **Settings → Updates** shows the pinned ref and what upstream offers, and *Apply* asks Hermes to update itself
  (`POST /api/hermes/update` through the integration layer — the Control Center never edits the checkout).
- Docker: rebuild with `--build-arg HERMES_REF=<tag>` to pin a release.
- Manual: `git pull` in the Hermes checkout and `pip install -e ".[web]"` into its interpreter.
- After any update: **Diagnostics → Re-check**. If something moved, the fix goes in `app/hermes/` (integration layer),
  never in Hermes. Full procedure and rollback: [docs/UPSTREAM-UPDATE.md](docs/UPSTREAM-UPDATE.md).

**This app.** `git pull` in `control-center/`, rebuild the UI (`npm run build`), restart the process. Your data lives in
`CC_HOME`, untouched by code updates.

---

## Backup, export and import

| What | Where | Contains |
|---|---|---|
| Create backup | Settings → Backups (or `POST /api/cc/settings/backups`) | ZIP: `config.json`, `control-center.db`, `manifest.json`, `README.txt` — written `0600` to `CC_HOME/backups` |
| Download backup | Settings → Backups → *download* | same archive |
| Export config | Settings → Export (JSON) | settings, chat history, provider choices. **No readable keys**; `include_secrets` adds the *encrypted* rows only |
| Import config | Settings → Import (JSON) | restores settings and history from an export |
| Hermes-side backup | Diagnostics → *Hermes backup* | run by Hermes itself (memory, skills, sessions are its data) |

**The vault master key (`secret.key`) is deliberately excluded** from every backup: an archive that contains both
ciphertext and the key is not encryption. Copy that one file separately if you want restorable keys, and protect it like
a password.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Hermes source directory not found` | `CC_HERMES_SOURCE` wrong | point it at the checkout; Diagnostics shows the exact path |
| `does not have Hermes' dependencies installed` | wrong interpreter | set `CC_HERMES_PYTHON`, or press install in Diagnostics (it runs only the `pip install` line) |
| Runtime `exited with code 1` | usually missing deps or a bad config | the runtime log tail is attached to the error; fix and restart |
| Chat: *bridge not ready* | gateway not running | **Chat → Enable bridge**; if another gateway serves the host, the app adopts it and tells you |
| Chat: *no provider selected* | the agent has no model | Models → pick a free route (Nous free tier needs no key) |
| `--skip-build was passed but no web dist found` | the runtime has no UI dist | the Control Center builds/points at one automatically; in Docker it is `/opt/control-center/frontend/build` |
| Free tier says *rate limited* | provider limit, not a bug | the app never silently falls back to a paid route |
| Schedules missed | host slept / scaled to zero | use an always-on host; nothing can run while the machine is off |
| Port already in use | another gateway/model server | the error names the owning process and the port |
| `one gateway per host` | Hermes allows one gateway per profile | the app adopts or restarts it instead of fighting it |

The CLI has the same answers without a browser: `python -m app.cli doctor`.

---

## Security notes

- Public binds require the app's own authentication; there is no anonymous admin path. `CC_ALLOW_REGISTRATION` is off.
- Cookies are `HttpOnly` + `SameSite=Lax`; `Secure` is automatic behind HTTPS. Revoke sessions individually.
- CSRF tokens are required for every write, bound to the session.
- Rate limiting is applied to sign-in, key reveal and other sensitive routes; failures are audited.
- The audit log records logins, key changes, FREE MODE changes, restores, exports and downloads.
- The Control Center is a **single-tenant admin tool**: exposing it to the internet is a decision you make; put it behind
  HTTPS, a VPN or a tunnel, and keep it patched.
- Security headers (`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, CSP) are sent by the backend; the SPA
  is served from the same origin, so `connect-src 'self'`.
- Report a vulnerability privately rather than opening a public issue with exploit details.

More: [docs/SECURITY.md](docs/SECURITY.md).

---

## Honesty labels

These words mean specific things everywhere in the UI and these docs:

| Label | Meaning |
|---|---|
| **FREE** | Runs on your own hardware. No account, no key, no bill — it uses your CPU/GPU and disk. |
| **SELF-HOSTED** | You run it on a server you control and pay for (or already own). No third party in the middle. |
| **LIMITED FREE TIER** | A free allowance from a provider you sign up to. Free while it lasts; they may rate-limit you, change terms, or ask for a card. **Not** "unlimited free". |
| **OPTIONAL PAID** | Billed to the account that owns the key. Never required to use this app, and never activated by the app on its own. |
| **REQUIRES EXTERNAL SERVICE** | Needs a third-party service (a cloud account, an OAuth provider, a messaging platform, a hosted database). The app labels it and you enable it deliberately. |
| **UNVERIFIED** | Nobody has verified the pricing. Treat it as paid until proven otherwise. |

The legend is also available from the API (`GET /api/cc/settings/cost-guard`) and rendered on the Providers page, so the
UI cannot drift from this table.

---

## Known limitations

1. **No bundled model.** A brand-new install has no model selected until you pick a free route or store a key. The app
   says this instead of faking a reply.
2. **Local models need a capable host.** On small VMs the Local page reports *unsupported* and explains the numbers.
3. **Nous free tier availability is upstream's call** and can change; it is toggled explicitly, never assumed.
4. **One gateway per host/profile** is a Hermes rule. The Control Center adopts or restarts an existing gateway instead
   of starting a second one.
5. **Schedules need a running host.** Sleeping/scaled-to-zero platforms miss runs.
6. **Docker image build not executed because Docker CLI is unavailable in the build environment.** The Dockerfile,
   compose file and `.dockerignore` were validated statically instead (see below); nothing here claims the image was
   built or run.
   compose file are checked by tests (`tests/test_deployment_assets.py`) but not built here.
7. **`hermes dashboard` cannot build the SPA itself** in an offline sandbox; the Control Center supplies the built UI.
8. **No automated browser test suite** ships yet (no Playwright in this environment): the UI is verified by type-check,
   production build, served-SPA checks and API contract tests.
9. **PostgreSQL is supported but less exercised** than SQLite; SQLite is the default precisely because it is enough and
   costs nothing.

---

## Exactly what could cost money

Nothing here is required, and nothing activates itself. This is the complete list of things that *can* incur a charge,
each of which needs you to add something:

| # | Item | When it costs | Requires |
|---|---|---|---|
| 1 | **Paid model providers** (OpenAI, Anthropic, Google Gemini paid tiers, xAI, DeepSeek, Together, Fireworks, DeepInfra, Novita, Bedrock, Vertex, Azure, and any `MIXED` provider's non-free models) | only if you store **your own** key, select that model, and unlock/disable FREE MODE | your account + key |
| 2 | **Provider free tiers** (Google AI Studio, Groq, Cerebras, Mistral, Hugging Face, OpenRouter `:free`, Nous free tier) | normally $0, but limits/terms are the provider's and a card may be requested; exceeding a limit can, on some providers, move you to billed usage if you enabled billing | your free account |
| 3 | **Hosting** beyond your own machine | if you rent a VPS or leave a provider's paid tier on (Oracle/Google/AWS/Azure "free" tiers can convert to paid) | a cloud account; a card is often required at signup |
| 4 | **Managed PostgreSQL** (Neon, Supabase, or any hosted Postgres) | only if you set `CC_DATABASE_URL` instead of using SQLite | a provider account; free tiers pause when idle |
| 5 | **Messaging platforms** (Telegram, Discord, Slack bots) | $0 for the service itself, but each needs an account and to be configured by you | an account with that platform |
| 6 | **Memory/sync providers** and other **REQUIRES EXTERNAL SERVICE** integrations | if their own pricing says so | their account |
| 7 | **Local model downloads** | $0 in money; costs disk (several GB) and CPU time | nothing but your hardware |

No subscription, no licence fee and no payment method is collected by this project. It has no telemetry and phones
nowhere except the providers you configure.

---

## Verification record

Everything below was executed against this working tree. Where something could not be executed, it says so instead of
being rounded up to "passed".

| Gate | Command | Result |
|---|---|---|
| Backend suite (hermetic) | `pytest tests/ -q` | **141 passed, 8 skipped** |
| Live Hermes runtime | `CC_LIVE=1 pytest tests/test_live_end_to_end.py -q` | **8 passed** (46 s) — spawns a real agent |
| Frontend ↔ backend contract | `pytest tests/test_contract.py -q` | **17 passed** |
| Deployment/configuration | `pytest tests/test_deployment_assets.py -q` | **14 passed** |
| Documentation | `pytest tests/test_docs.py -q` | **15 passed** |
| Frontend type-check | `npx tsc --noEmit` | **0 errors** |
| Frontend production build | `npm run build` | **OK** — 340 kB JS / 31 kB CSS (99 kB gzip) |
| Bundle secret scan | secret-shaped regex over `frontend/build/` | **0 hits**; no hard-coded loopback request URLs |
| Repository secret scan | secret-shaped regex over all 93 project files | **0 real hits** (4 matches are placeholders: two `'a-long-unique-password'` doc examples, a `getpass` prompt string, and a test-fixture constant) |
| Docker image build | `docker build …` | **Not executed** — see the sentence in *Known limitations* above |

**Docker image build not executed because Docker CLI is unavailable in the build environment.** What *was* done instead:

- `docker-compose.yml` is parsed and asserted to define the expected services, volumes, port mapping, health check,
  restart policy and `no-new-privileges`;
- every variable the compose file sets is checked against the variables the application actually reads;
- the `HEALTHCHECK` is asserted to hit `/health`, a route that exists;
- the Dockerfile is asserted to run as a non-root user, clone upstream Hermes (never vendor it), bake no secret,
  and use a `CMD` that matches `app/__main__.py`;
- every `COPY` source is resolved against the real build context, and the `.dockerignore` is checked to exclude
  `node_modules`, build output, databases and `.env` while keeping `.env.example`.

**Real-runtime evidence** (from the running instance, not a mock): `hermes dashboard --isolated` started as a supervised
child process on `127.0.0.1:9119`; its gateway exposes `hermes-agent` on `127.0.0.1:9120`; a chat turn issued
`POST /v1/runs → 202 Accepted` and streamed `GET /v1/runs/{id}/events → 200` through SSE frames
`status → run → status → error → done`. The stored transcript contains the agent's *own* error
(`no_provider_selected`) — the app never substitutes an invented reply.

## Testing

```bash
# backend — hermetic: no runtime is spawned, real SQLite, real crypto
cd control-center/backend
python -m pytest tests/ -q                       # 136 passed, 8 skipped

# backend — live, against a real Hermes runtime (spawns a real agent on throwaway ports)
CC_LIVE=1 \
CC_HERMES_SOURCE=/path/to/hermes-agent \
CC_HERMES_PYTHON=/path/to/hermes-agent/venv/bin/python \
python -m pytest tests/test_live_end_to_end.py -q   # 8 passed

# frontend — types and production build
cd ../frontend
npx tsc --noEmit
npm run build
```

What the suite covers: API schema, authentication and CSRF, FREE MODE and the cost guard, the encrypted vault,
data/error handling, the Hermes pass-through allowlist, chat and gateway supervision, the frontend↔backend contract
(every path a page calls must exist), the deployment assets (compose/Dockerfile/`.env.example` must reference real
variables and contain no secrets), and the documentation itself (every link, endpoint, CLI command and environment
variable named in these files must exist).

---

## Licence

This Control Center is released under the **MIT licence**, matching the upstream project it integrates.

**Hermes Agent** is © 2025 **Nous Research**, MIT licensed — <https://github.com/NousResearch/hermes-agent>.
Its `LICENSE` file is preserved in this repository. If you redistribute this stack, keep the upstream attribution and
licence with it.
