"""Deployment compatibility catalogue — written to be honest, not flattering.

The rule this file obeys: never call something free when it is a trial, never
call something free when it needs a credit card, and never promise 24/7 uptime
on a platform that sleeps or reclaims idle resources. Where a platform *is*
genuinely usable at $0, say exactly which parts work and which do not.

Facts age. Each row carries ``verified`` and ``verify_url``; the UI shows
"verify before you rely on it" with that date. Nothing here is a promise.
"""

from __future__ import annotations

FREE = "FREE"
SELF_HOSTED = "SELF-HOSTED"
LIMITED = "LIMITED FREE TIER"
PAID = "PAID"

SUITABLE_YES = "yes"
SUITABLE_PARTIAL = "partial"
SUITABLE_NO = "no"

VERIFIED = "2026-10-07"

PLATFORMS: list[dict] = [
    {
        "name": "Your own computer, spare laptop or mini PC",
        "cost_label": FREE,
        "cost_detail": "You already own the hardware; the only cost is electricity.",
        "card_required": False,
        "sleeps": "only if you suspend the machine",
        "ram": "whatever the machine has",
        "cpu": "whatever the machine has",
        "disk": "local disk, persistent",
        "persistent_storage": True,
        "background_processes": True,
        "holds_24_7_agent": "yes, while the machine is powered on",
        "suitable": SUITABLE_YES,
        "suitable_for": "Control Center + Hermes runtime + local models",
        "notes": (
            "The only zero-cost option where local model inference is realistic, and the only one with no "
            "sleeping, quota or reclaim policy. A headless box on your LAN plus a tunnel gives you remote access."
        ),
        "verify_url": "https://hermes-agent.nousresearch.com/docs/getting-started/installation",
        "verified": VERIFIED,
    },
    {
        "name": "Your own VPS, NAS or Raspberry Pi (Docker)",
        "cost_label": SELF_HOSTED,
        "cost_detail": "$0 if you already own it, otherwise a small monthly fee you already pay.",
        "card_required": False,
        "sleeps": "no",
        "ram": "depends on the box (a 1 GB Pi runs the Control Center, not a 7B model)",
        "cpu": "depends on the box (ARM is fine for everything except big models)",
        "disk": "persistent",
        "persistent_storage": True,
        "background_processes": True,
        "holds_24_7_agent": "yes",
        "suitable": SUITABLE_YES,
        "suitable_for": "Control Center + Hermes runtime; local models only if RAM allows",
        "notes": (
            "The deployment upstream targets and the one this project's docker-compose.yml is written for. "
            "A 1 GB box is enough for the Control Center and the agent with a cloud model."
        ),
        "verify_url": "https://hermes-agent.nousresearch.com/docs/deployment",
        "verified": VERIFIED,
    },
    {
        "name": "Oracle Cloud Always Free (ARM Ampere A1 / x86 micro)",
        "cost_label": LIMITED,
        "cost_detail": "A permanently-free allotment exists, but the account is reclaimable and capacity is limited.",
        "card_required": True,
        "sleeps": "idle instances can be reclaimed; free ARM capacity is often unavailable in a region",
        "ram": "up to 24 GB on the ARM free shape, 1 GB on the x86 micro shape — if you can get one",
        "cpu": "up to 4 ARM cores on the free shape",
        "disk": "200 GB block volume total on the free tier",
        "persistent_storage": True,
        "background_processes": True,
        "holds_24_7_agent": "yes, until the instance is reclaimed",
        "suitable": SUITABLE_PARTIAL,
        "suitable_for": "Control Center + Hermes runtime; the ARM free shape can even run local models",
        "notes": (
            "The most generous real free tier for a 24/7 agent, and the least predictable: signup needs a payment "
            "method, popular regions report no ARM capacity, and idle-instance reclamation policies exist. Take "
            "backups off the box."
        ),
        "verify_url": "https://www.oracle.com/cloud/free/",
        "verified": VERIFIED,
    },
    {
        "name": "Google Cloud e2-micro Always Free",
        "cost_label": LIMITED,
        "cost_detail": "One e2-micro instance in us-central1/us-west1/europe-west1 is free; network egress is not unlimited.",
        "card_required": True,
        "sleeps": "no, but it is a very small shared-vCPU instance",
        "ram": "1 GB",
        "cpu": "2 shared vCPU (bursty)",
        "disk": "30 GB standard persistent disk",
        "persistent_storage": True,
        "background_processes": True,
        "holds_24_7_agent": "yes, with a cloud model; not for local inference",
        "suitable": SUITABLE_PARTIAL,
        "suitable_for": "Control Center + Hermes runtime with a hosted model",
        "notes": (
            "1 GB of RAM is the floor: fine for the Control Center and a cloud-hosted model, tight if the agent "
            "runs heavy tools. Needs a billing account (card) even while you stay inside the free limits."
        ),
        "verify_url": "https://cloud.google.com/free/docs/free-cloud-features",
        "verified": VERIFIED,
    },
    {
        "name": "AWS Free Tier (t3.micro / t2.micro)",
        "cost_label": LIMITED,
        "cost_detail": "750 hours/month for 12 months for new accounts — a trial, then it bills.",
        "card_required": True,
        "sleeps": "no, but the allowance ends after 12 months and the instance starts costing money",
        "ram": "1 GB",
        "cpu": "2 vCPU (burstable)",
        "disk": "up to 30 GB EBS on the free allowance",
        "persistent_storage": True,
        "background_processes": True,
        "holds_24_7_agent": "yes for 12 months, then you are paying",
        "suitable": SUITABLE_PARTIAL,
        "suitable_for": "Evaluation, not a permanent $0 home",
        "notes": (
            "This is a trial, not a free tier. If you use it, set a billing alarm on day one. Marked LIMITED "
            "FREE TIER and time-boxed on purpose."
        ),
        "verify_url": "https://aws.amazon.com/free/",
        "verified": VERIFIED,
    },
    {
        "name": "Azure free account (B1s, 12 months)",
        "cost_label": LIMITED,
        "cost_detail": "$200 credit for 30 days plus 12 months of limited free services.",
        "card_required": True,
        "sleeps": "no, but the 12-month window ends",
        "ram": "1 GB on B1s",
        "cpu": "1 vCPU",
        "disk": "64 GB on the free allowance",
        "persistent_storage": True,
        "background_processes": True,
        "holds_24_7_agent": "yes for 12 months, then it bills",
        "suitable": SUITABLE_PARTIAL,
        "suitable_for": "Evaluation",
        "notes": "Same caveat as AWS: time-boxed trial. Do not put anything you cannot rebuild onto it.",
        "verify_url": "https://azure.microsoft.com/free/",
        "verified": VERIFIED,
    },
    {
        "name": "GitHub Codespaces (dev container)",
        "cost_label": LIMITED,
        "cost_detail": "Free monthly core-hours for personal accounts, then billed.",
        "card_required": True,
        "sleeps": "stops automatically after idle timeout; a stopped codespace runs nothing",
        "ram": "2–4 GB on the free machine types",
        "cpu": "2 cores",
        "disk": "persistent while the codespace exists",
        "persistent_storage": True,
        "background_processes": "only while the codespace is running",
        "holds_24_7_agent": "no — it stops on idle and on quota exhaustion",
        "suitable": SUITABLE_NO,
        "suitable_for": "Trying the app out, or developing against a temporary port; not for schedules",
        "notes": (
            "Genuinely useful for a quick look: the container has Python and can run this project. But anything "
            "scheduled dies with the stop, so do not rely on it for cron jobs."
        ),
        "verify_url": "https://docs.github.com/billing/managing-billing-for-your-products/about-billing-for-github-codespaces",
        "verified": VERIFIED,
    },
    {
        "name": "Render free web service",
        "cost_label": LIMITED,
        "cost_detail": "Free instance type with a monthly hour cap; disk is ephemeral.",
        "card_required": False,
        "sleeps": "yes — spins down after ~15 minutes without traffic and cold-starts on the next request",
        "ram": "512 MB",
        "cpu": "0.1 vCPU (shared)",
        "disk": "ephemeral; lost on every deploy/restart",
        "persistent_storage": False,
        "background_processes": "not reliably — the container is stopped when idle",
        "holds_24_7_agent": "no",
        "suitable": SUITABLE_NO,
        "suitable_for": "Hosting the Control Center UI only, with the runtime elsewhere",
        "notes": (
            "512 MB is enough for the Control Center alone, and the UI/API split makes that a legitimate "
            "deployment: UI on Render, agent on your own box over a tunnel. Schedules will not run here."
        ),
        "verify_url": "https://render.com/docs/free",
        "verified": VERIFIED,
    },
    {
        "name": "Koyeb free instance",
        "cost_label": LIMITED,
        "cost_detail": "One free service on limited resources.",
        "card_required": True,
        "sleeps": "scale-to-zero behaviour on the free plan; cold starts",
        "ram": "512 MB–1 GB depending on plan changes",
        "cpu": "low shared CPU",
        "disk": "ephemeral on the free plan",
        "persistent_storage": False,
        "background_processes": "not reliably",
        "holds_24_7_agent": "no",
        "suitable": SUITABLE_NO,
        "suitable_for": "Control Center UI only",
        "notes": "Fine for a small always-available UI if it stays awake; never for the agent's scheduler.",
        "verify_url": "https://www.koyeb.com/pricing",
        "verified": VERIFIED,
    },
    {
        "name": "Replit free",
        "cost_label": LIMITED,
        "cost_detail": "Free tier exists but always-on/published deployments are a paid feature.",
        "card_required": False,
        "sleeps": "yes — the workspace sleeps, and free repls stop when the tab closes",
        "ram": "small shared allocation",
        "cpu": "shared",
        "disk": "persistent inside the repl",
        "persistent_storage": True,
        "background_processes": "no on the free tier",
        "holds_24_7_agent": "no",
        "suitable": SUITABLE_NO,
        "suitable_for": "A quick experiment in a browser tab",
        "notes": "Honest label: Replit's free tier is not a hosting product for a long-lived agent.",
        "verify_url": "https://docs.replit.com/legal-and-security-info/pricing",
        "verified": VERIFIED,
    },
    {
        "name": "Hugging Face Spaces (free CPU)",
        "cost_label": LIMITED,
        "cost_detail": "Free CPU tier for public Spaces; paid for persistent hardware.",
        "card_required": False,
        "sleeps": "yes — spaces sleep after a period of inactivity (and free CPU spaces pause on inactivity)",
        "ram": "16 GB on the free CPU tier while it is awake",
        "cpu": "2 vCPU",
        "disk": "ephemeral; wiped on rebuild unless you use persistent storage (paid)",
        "persistent_storage": False,
        "background_processes": "only while the Space is awake",
        "holds_24_7_agent": "no",
        "suitable": SUITABLE_NO,
        "suitable_for": "Demos with a cloud model; not for stored keys or schedules",
        "notes": (
            "Attractive RAM for a free tier, but sleeping plus ephemeral storage makes it a demo host. Never put "
            "provider keys on a public Space."
        ),
        "verify_url": "https://huggingface.co/pricing",
        "verified": VERIFIED,
    },
    {
        "name": "Static/edge hosts (Cloudflare Pages, Vercel, Netlify)",
        "cost_label": FREE,
        "cost_detail": "Genuinely free for static hosting and short serverless functions.",
        "card_required": False,
        "sleeps": "no (edge is always on) but functions are time-limited per request",
        "ram": "n/a — you cannot run a long-lived Python process",
        "cpu": "n/a",
        "disk": "n/a",
        "persistent_storage": False,
        "background_processes": False,
        "holds_24_7_agent": "no",
        "suitable": SUITABLE_NO,
        "suitable_for": "Hosting only the built UI, if you point it at a backend you run elsewhere",
        "notes": (
            "These platforms cannot run the agent: no long-lived processes, no SQLite file, request time limits. "
            "The Control Center is a Python app, so it needs a host that runs one."
        ),
        "verify_url": "https://developers.cloudflare.com/pages/",
        "verified": VERIFIED,
    },
    {
        "name": "Free managed PostgreSQL (Neon, Supabase)",
        "cost_label": FREE,
        "cost_detail": "Free database tiers with storage and compute limits; some pause when idle.",
        "card_required": False,
        "sleeps": "some pause the compute instance after inactivity and resume on connect",
        "ram": "n/a",
        "cpu": "n/a",
        "disk": "0.5–1 GB on the free tiers",
        "persistent_storage": True,
        "background_processes": False,
        "holds_24_7_agent": "n/a — this is a database, not a runtime",
        "suitable": SUITABLE_PARTIAL,
        "suitable_for": "CC_DATABASE_URL when the host has no persistent disk; not for the agent itself",
        "notes": (
            "Useful when your UI host is ephemeral: point CC_DATABASE_URL at a free Postgres and the Control Center "
            "keeps its users, settings and logs across restarts. Keys are encrypted with the local master key, so "
            "back up CC_HOME/secrets.key too or they become unreadable."
        ),
        "verify_url": "https://neon.com/pricing",
        "verified": VERIFIED,
    },
    {
        "name": "Free model inference (no hosting at all)",
        "cost_label": LIMITED,
        "cost_detail": "$0 API access with hard limits: local models, or a provider's free tier with your own account.",
        "card_required": False,
        "sleeps": "not applicable — but free allowances are rate limited and can change without notice",
        "ram": "n/a",
        "cpu": "n/a",
        "disk": "n/a",
        "persistent_storage": False,
        "background_processes": False,
        "holds_24_7_agent": "the rate limits decide",
        "suitable": SUITABLE_YES,
        "suitable_for": "Any host: this is how the app answers questions without spending money",
        "notes": (
            "Two honest options: run a model on hardware you own (truly free, needs RAM/VRAM), or use a provider "
            "free tier that requires an account and imposes rate limits. There is no third option, and this app "
            "never pretends otherwise."
        ),
        "verify_url": "https://hermes-agent.nousresearch.com/docs/models",
        "verified": VERIFIED,
    },
]


def deployment_matrix() -> dict:
    return {
        "platforms": PLATFORMS,
        "labels": {
            "yes": "can host the agent around the clock",
            "partial": "usable with the stated limitation",
            "no": "cannot host the Hermes runtime",
        },
        "verified": VERIFIED,
        "disclaimer": (
            "Free tiers change without notice, and 'free' very often means 'free until a limit'. Every row was "
            "written on 2026-10-07 and links to the provider's own limits page — verify before you depend on it. "
            "Nothing in this table is a promise of 24/7 uptime."
        ),
        "recommendation": {
            "zero_cost_24_7": [
                "Run it on hardware you already own — everything works, including local models, and nothing sleeps.",
                "If you need a cloud VM: Oracle Always Free or Google Cloud's e2-micro, accepting the caveats listed.",
                "If the platform sleeps, do not put schedules on it: Hermes cron jobs run inside the runtime process.",
            ],
            "zero_cost_ui_only": [
                "Host the Control Center on a free instance and keep the Hermes runtime on a machine at home.",
                "Set CC_DATABASE_URL to a free Postgres if the UI host has no persistent disk.",
            ],
        },
    }


def free_hosting_mode() -> dict:
    return {
        "name": "FREE HOSTING MODE",
        "what_it_does": (
            "Makes the zero-cost deployment the default and the honest one: SQLite for storage (no managed database "
            "bill), the Control Center serving its own UI (no CDN or build service), Hermes installed from the "
            "upstream Git source (no paid registry), and provider keys optional with free routes first."
        ),
        "why_it_matters": [
            "Nothing in the default configuration costs money or requires a credit card.",
            "Paid providers are unselectable until you explicitly store your own key and unlock paid fallback.",
            "The app tells you when a free route is rate limited instead of silently spending on a paid one.",
        ],
        "what_it_cannot_do": (
            "It cannot make a sleeping host stay awake, cannot give a 512 MB container room for a 7B model, and "
            "cannot guarantee a third party's free tier stays free. Those are physical and commercial limits, and "
            "the app reports them rather than hiding them."
        ),
        "requirements": [
            "One always-on machine (or free VM) with at least 1 GB RAM; 2 GB+ recommended for the agent with tools.",
            "Python 3.11–3.14 for the Hermes runtime (3.14 is what upstream develops against).",
            "Outbound HTTPS for model providers and GitHub (unless you only use local models).",
        ],
        "verification": [
            "Deployment page → preflight: measures this host's RAM, CPU, disk and reports exactly what fits.",
            "Models page → FREE MODE card: shows the active model and whether it can spend money (it cannot, by default).",
            "Diagnostics page → checks every dependency with the real command it would run.",
        ],
    }
