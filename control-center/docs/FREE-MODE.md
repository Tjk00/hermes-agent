# FREE MODE, cost tiers and the paid guard

The acceptance criterion for this project is simple: **the first successful run must not require a paid API key or a
credit card.** This document explains how that is enforced, what "free" actually means for each route, and how to add
paid capacity later without ever losing the free path.

---

## The five labels

These words have fixed meanings in the UI, the API and these docs. The live legend is served by
`GET /api/cc/settings/cost-guard` and rendered on the Providers page, so the UI cannot drift from the code.

| Label | Meaning | Can it bill you? |
|---|---|---|
| **FREE** | Runs on your own hardware. No account, no key, no bill — it uses your CPU/GPU and disk. | No |
| **SELF-HOSTED** | You run it on a server you control and pay for (or already own). No third party in the middle. | You pay for the host, not per request |
| **LIMITED FREE TIER** | A free allowance from a provider you sign up to. Free while it lasts; they may rate-limit, change terms, or ask for a card. **Not** "unlimited free". | Normally no; limits and terms are theirs |
| **OPTIONAL PAID** | Billed to the account that owns the key. Never required, and never activated by this app on its own. | Yes, only if you choose it |
| **REQUIRES EXTERNAL SERVICE** | Needs a third-party service (cloud account, OAuth login, messaging platform, hosted database). You enable it deliberately. | Depends on that service |
| *(UNVERIFIED)* | Nobody verified the pricing. Treat it as paid until proven otherwise. | Assume yes |

---

## What FREE MODE enforces

FREE MODE is on by default (`CC_FREE_MODE=1`) and is enforced **server-side** in the request path:

| Guard | Where | Behaviour |
|---|---|---|
| Paid provider selection | `app/hermes/provider_catalog.guard` + every route that can route a request | **409/402 refused before the request leaves the process** — "Nothing was sent and nothing was charged." |
| Paid as a chain entry | `PUT /api/cc/models/chain` | a chain needs two free entries before an optional paid third is even stored |
| Paid fallback activation | free-mode policy | requires the exact acknowledgement phrase `PAID_ACK` and an explicit administrator action |
| Schedules pinned to paid models | schedules create/update | refused while FREE MODE is on |
| Free-chain apply | `POST /api/cc/models/free-chain/apply` | fails with **409** if no live free route exists — it never silently picks a paid model |
| Model listing | Models/Providers pages | each model carries its tier; `:free` suffixes classify as LIMITED FREE TIER, never PAID |

Two switches exist, and neither is a trap:

- `free_mode` (default **on**) — refuses paid providers entirely.
- `allow_paid_fallback` (default **off**) — the explicit unlock, requiring the acknowledgement phrase.
- Optionally `lock_paid_providers` (default **on**) — keeps paid providers out of pickers.

**Storage is not activation.** A stored key for a paid provider appears on the Providers page under *"What could cost
money — configured ≠ active"* and stays inert until you select it *and* the guards allow it.

---

## The free routes, in priority order

| # | Route | Label | Needs | Honest caveat |
|---|---|---|---|---|
| 1 | Local model (llama.cpp managed runtime, LM Studio, Ollama, any local OpenAI-compatible endpoint) | **FREE** | enough RAM/CPU; multi-GB download | slow on CPU; impossible on a 512 MB free tier. The Local page reports *this host's* verdict |
| 2 | Nous / Hermes free tier (`HERMES_GUEST_ONBOARDING`, `nous.guest`) | **LIMITED FREE TIER** | nothing but the toggle, if upstream offers it | availability and limits are Nous'; can change |
| 3 | Provider free tiers with your own free key: Google AI Studio, Groq, Cerebras, Mistral, Hugging Face, OpenRouter `:free` | **LIMITED FREE TIER** | a free account with that provider | their limits, their terms; a card may be requested |
| 4 | Anything paid | **OPTIONAL PAID** | your key + explicit unlock | billed to your account |

The app **detects** what is actually available rather than assuming:

- `GET /api/cc/local/capability` → `verdict.state` is `supported` / `marginal` / `unsupported` for **this** host, with
  the numbers behind the verdict.
- `GET /api/cc/free-mode` → `routes[]` lists each route with `available`, `state` and an explanation, plus
  `headline` ("N free route(s) available…" or "No free route is ready yet — the app will say so instead of spending
  money.").
- `GET /api/cc/providers/spend-guard` → everything configured that *could* cost money, and whether it can run.

---

## Setting up a free route

### A. A local model (truly free, needs hardware)

1. **Local models → Local check** — read the verdict for your machine.
2. If it says `supported`, install/download a model from the catalogue (llama.cpp runtime is managed by Hermes).
3. **Models → select** the local model; it is labeled **FREE**.
4. If it says `unsupported`, believe it: the verdict names RAM/CPU/GPU as the constraint.

### B. The Nous free tier (no API key)

1. **Providers → Nous free tier** → on. This sets `HERMES_GUEST_ONBOARDING=1` and the runtime's `nous.guest` config.
2. The runtime asks the free tier first. Availability is gated by Nous; the app says so and never implies otherwise.

### C. A provider's free tier with your own free key

1. Create a free account with e.g. Google AI Studio / Groq / Cerebras / Mistral.
2. **Keys → Add key** → paste the key (encrypted immediately, never returned to the browser).
3. **Providers → Test connection** → a real request proves the key works.
4. **Models → select** a model that is labeled **LIMITED FREE TIER**.

### D. A free-first fallback chain

1. **Models → Fallback chain** → add two **free** entries first (e.g. local → provider free tier).
2. *Apply free chain* switches the runtime to the free path in one action, or fails with a clear reason.
3. The optional third (paid) entry can be stored, but is **never used automatically** unless you unlock paid fallback.

---

## Turning paid capacity on (optional, never required)

1. **Providers → Free Mode** → off, *or* leave FREE MODE on and only unlock the fallback: **Allow paid models** →
   confirm with the phrase.
2. Store your **own** key for that provider (**Keys**).
3. Select a paid model in **Models**, deliberately. The UI keeps the PAID label visible the whole time.

Turning it back on restores the previous behaviour immediately, and every change is in the audit log
(**Logs → SECURITY**).

---

## Cost reporting in the UI

- The chat header shows **Model**, **Provider**, **Cost tier** and **Context length** for the route actually in use.
- The dashboard's FREE MODE card states the active route and the cost posture in words.
- Providers shows *"What could cost money"* with a per-provider explanation and a *verify pricing* link where one is
  known; `configured ≠ active` is stated explicitly.
- Rate limits and provider errors are surfaced verbatim — the app never quietly retries on a paid route.

---

## What FREE MODE deliberately does *not* do

- It does not block **you** from using your own paid key. It blocks the app from *choosing* it for you.
- It does not promise a free tier will stay free. It reports the tier it can verify and marks everything else
  UNVERIFIED.
- It does not fake a model. If no free route is reachable, chat returns the agent's own error and the UI explains how to
  fix it — it never invents a reply.
