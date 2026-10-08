# Deployment guide

Everything here is designed for **$0/month by default**. Where a step can cost money, it says so and stays optional.

---

## 0. Choose a home for it

| Option | Label | Best for | Watch out for |
|---|---|---|---|
| Your computer / laptop / mini PC | **FREE** | trying it, local models, personal use | not 24/7 if you suspend it |
| Your own VPS / NAS / Raspberry Pi | **SELF-HOSTED** | always-on, full control, schedules | you pay the host; small boards can't run local models |
| Free cloud VM (Oracle Always Free, Google e2-micro, AWS/Azure free tier) | **LIMITED FREE TIER** | free 24/7-ish hosting | small RAM/CPU, card often required, verify current terms |
| PaaS free tiers (Render, Koyeb, Replit, HF Spaces, Codespaces) | **LIMITED FREE TIER** | nothing — they sleep/stop idle processes | **cannot host the runtime properly**; schedules are missed |
| Static/edge hosts (Pages, Vercel, Netlify) | **FREE** | — | static files only; no agent, no scheduler |

The in-app **Deploy** page (`GET /api/cc/deploy/platforms`) shows the same matrix with the date each row was verified and
a link to the provider's own limits page. Free tiers change: verify before you depend on one.

---

## 1. Local / self-hosted (recommended)

### 1.1 Prerequisites

- Python **3.11–3.14** (matching what Hermes supports)
- `git`, and `node` 22+ **only** if you build the UI yourself
- ~1 GB free disk (plus any local model you download)

### 1.2 Install upstream Hermes

```bash
git clone https://github.com/NousResearch/hermes-agent
cd hermes-agent && ./setup-hermes.sh
```

Note the interpreter: usually `hermes-agent/venv/bin/python` (Linux/macOS) or `hermes-agent\.venv\Scripts\python.exe`
(Windows).

### 1.3 Install the Control Center

```bash
cd control-center/frontend && npm ci && npm run build
cd ../backend && python -m pip install -e .
```

### 1.4 First run

```bash
export CC_HERMES_SOURCE="$PWD/../.."                 # the Hermes checkout
export CC_HERMES_PYTHON="$PWD/../../venv/bin/python" # the interpreter from 1.2
export CC_HERMES_HOME="$HOME/.hermes"
export CC_ADMIN_PASSWORD='a-long-unique-password'
python -m app
```

Open <http://127.0.0.1:8080>. The wizard walks through: deployment mode → model → optional providers → admin →
system test → launch.

### 1.5 Run it as a service

**systemd** (Linux, your own machine):

```ini
# /etc/systemd/system/hermes-control-center.service
[Unit]
Description=Hermes Agent Control Center
After=network-online.target

[Service]
User=hermes
WorkingDirectory=/opt/control-center/backend
EnvironmentFile=/etc/hermes-control-center.env
ExecStart=/usr/bin/python3 -m app
Restart=on-failure
RestartSec=5
# The app supervises the agent itself; no extra unit is needed for Hermes.

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now hermes-control-center
journalctl -u hermes-control-center -f
```

**launchd** (macOS) and **Task Scheduler** (Windows) work the same way: run `python -m app` with the environment above.
You do **not** need to start Hermes separately — the Control Center starts and supervises it, and it survives hosts
without systemd (containers, free VMs, NAS boxes).

### 1.6 Reaching it from your phone

- Same network: `http://<host-ip>:8080` — fine on your LAN.
- Anywhere else: put it behind a VPN (WireGuard/Tailscale) or a tunnel (Cloudflare Tunnel), terminate HTTPS, and set
  `CC_SECURE_COOKIES=1`. If you run it under a sub-path, set `CC_ROOT_PATH=/hermes`; behind a proxy, list it in
  `CC_TRUSTED_PROXIES` and add your origin to `CC_ALLOWED_ORIGINS`.

---

## 2. Docker (single command, no systemd)

```bash
cd control-center
cp .env.example .env
$EDITOR .env                                  # set CC_ADMIN_PASSWORD at least
docker compose -f docker/docker-compose.yml up -d --build
docker compose -f docker/docker-compose.yml logs -f
```

One container runs the Control Center **and** the Hermes runtime, so the compose file is enough for UI + API + agent.
Health: `docker compose ps` (uses the image's `HEALTHCHECK` against `/health`).

### Optional PostgreSQL

```bash
docker compose -f docker/docker-compose.yml --profile postgres up -d
# then set in .env:
CC_DATABASE_URL=postgresql://hermes:change-me-before-use@postgres:5432/hermes_control_center
docker compose -f docker/docker-compose.yml up -d control-center   # recreate with the new DSN
```

SQLite is the default because it is free, needs no service, and is enough for a single-host tool. PostgreSQL is for
people who already run one.

### Publishing and TLS

The compose file binds to `127.0.0.1:8080`. To expose it: `CC_BIND=0.0.0.0` and a reverse proxy in front, or a tunnel
that terminates TLS. Use HTTPS for anything non-loopback, then set `CC_SECURE_COOKIES=1`.

### Memory

`CC_MEMORY_LIMIT` (default `2g`) is the container ceiling. Hermes with tools is happy in 1–2 GB; local models are not —
they need the RAM of the model plus overhead, which is why the Local page reports a verdict instead of assuming.

---

## 3. Free-tier hosting, honestly

**Oracle Cloud Always Free / Google e2-micro / AWS t3.micro / Azure B1s** — genuine free VMs, and the Control Center
runs on them. Expect 1 GB–1 GB-ish RAM and no GPU, so:

- local models: **not realistic** (the app will say so);
- use a cloud free tier for the model (Nous free tier, or a provider free-tier key you create yourself);
- keep an eye on the provider's idle-reclaim policy and their "free → paid" conversion rules.

**PaaS free tiers (Render, Koyeb, Replit, HF Spaces, Codespaces)** — they sleep, stop, or kill long-running background
work. Hermes needs a resident process for the gateway and for schedules, so these are marked *cannot host the runtime
properly* in the Deploy page. Using one means chat works only while the instance is awake, and scheduled tasks are
missed.

**Static hosts (Pages, Vercel, Netlify)** — can serve the built UI only. They cannot run the agent or the scheduler, and
this project does not pretend otherwise.

**Nothing here promises 24/7 uptime on a free host.** Free tiers sleep, reclaim, rate-limit, and change their terms.

---

## 4. Ports and firewall

| Port | Variable | What listens | Exposure |
|---|---|---|---|
| 8080 | `CC_PORT` | the Control Center (UI + API) | the only port you may need to reach |
| 9119 | `CC_HERMES_PORT` | the Hermes runtime API | loopback only |
| 9120 | `CC_CHAT_BRIDGE_PORT` | the agent's OpenAI-compatible chat API | loopback only, key-protected |

Only `CC_PORT` should ever be exposed. The other two are bound to `127.0.0.1` by the app.

---

## 5. Post-deploy checks

```bash
curl -fsS http://127.0.0.1:8080/health          # liveness (public, sanitised)
python -m app.cli doctor                        # install + capability report
```

In the UI: **Diagnostics** must show every check green except the ones that are honestly informational (e.g. "runtime
management disabled" when you deliberately set `CC_RUNTIME_MODE=disabled`). **Deploy → Preflight** shows whether the host
passes the RAM/CPU/disk bar for the agent.

Then take a backup (**Settings → Backups**) — the whole point of a backup is that you have one before you need it.

---

## 6. Migrating to another host

See [MIGRATION.md](MIGRATION.md). Short version: move `CC_HOME` **and** `$HERMES_HOME` (in Docker: the `hermes-data`
volume) and the vault master key, then start the app there and re-check Diagnostics.
