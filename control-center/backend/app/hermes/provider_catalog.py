"""Provider catalogue and the honest money classification behind FREE MODE.

Two layers, deliberately kept apart:

1. **What upstream ships** — ``data/provider_registry.json`` is generated from the
   Hermes checkout (``tools/dump_provider_registry.py``): provider ids, display
   names, environment variable names. Factual, regenerable, diffable.
2. **What it costs** — ``CLASSIFICATION`` below. This is ours, because upstream
   has no reason to track billing and nobody else can be trusted to label
   "free" correctly. Every row carries the date it was written and a link to the
   provider's own limits page.

Rules this file obeys (the brief is explicit about them):

* a trial is not free — it is labelled PAID with "trial credits" in the note;
* a free tier with limits is LIMITED FREE TIER, never plain FREE;
* anything requiring a card says so;
* anything we cannot verify is ``unknown`` and renders as UNVERIFIED, which the UI
  shows as loudly as PAID;
* local inference is FREE and needs no key — but only where the hardware exists,
  which is why the Local page measures the host instead of assuming.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_FILE = Path(__file__).resolve().parent / "data" / "provider_registry.json"

FREE_LOCAL = "free_local"
FREE_TIER = "free_tier"
PAID = "paid"
MIXED = "mixed"
UNKNOWN = "unknown"

VERIFIED = "2026-10-07"

#: The exact phrase an operator must type to allow a paid model as a fallback.
#: Deliberately annoying: it is the last thing standing between a tap and a bill.
PAID_ACK = "I UNDERSTAND PAID"

BILLING_LABELS = {
    FREE_LOCAL: "FREE",
    FREE_TIER: "LIMITED FREE TIER",
    PAID: "PAID",
    MIXED: "FREE + PAID",
    UNKNOWN: "UNVERIFIED",
}


# --------------------------------------------------------------------- helpers
def _row(
    billing: str,
    *,
    requires_key: bool = True,
    card_required: bool | None = None,
    signup_url: str = "",
    free_note: str = "",
    cost_note: str = "",
    notes: str = "",
    label: str = "",
    verify_url: str = "",
) -> dict:
    return {
        "billing": billing,
        "cost_label": BILLING_LABELS[billing],
        "requires_key": requires_key,
        "card_required": card_required,
        "signup_url": signup_url,
        "free_note": free_note,
        "cost_note": cost_note,
        "notes": notes,
        "label": label,
        "verify_url": verify_url,
        "verified": VERIFIED,
    }


#: Provider id -> money facts. Keys match the ids upstream uses plus the aliases
#: that appear in ``hermes model`` output.
CLASSIFICATION: dict[str, dict] = {
    # ------------------------------------------------------------- local (FREE)
    "lmstudio": _row(
        FREE_LOCAL,
        requires_key=False,
        card_required=False,
        label="LM Studio (local server)",
        free_note="Runs on your own machine. No account, no key, no per-request cost.",
        cost_note="Only your electricity and hardware.",
        notes="LM Studio must be running with its local server enabled. Model size is limited by your RAM/VRAM.",
        verify_url="https://lmstudio.ai/docs/local-server",
    ),
    "custom": _row(
        FREE_LOCAL,
        requires_key=False,
        card_required=False,
        label="Custom / local OpenAI-compatible endpoint",
        free_note="Any endpoint you host: Ollama, vLLM, llama.cpp server, LM Studio, or a remote box of your own.",
        cost_note="Whatever the machine you point it at costs you.",
        notes="Correctly classified as free only while the endpoint is on this host or your own network.",
        verify_url="https://hermes-agent.nousresearch.com/docs/models",
    ),
    "llamacpp": _row(
        FREE_LOCAL,
        requires_key=False,
        card_required=False,
        label="llama.cpp (Hermes managed runtime)",
        free_note="Hermes can download and supervise llama.cpp locally; inference then costs $0 per request.",
        cost_note="Download size and CPU/RAM usage are real; check the Local page verdict first.",
        notes="Model downloads are multi-GB and come from the internet at setup time.",
        verify_url="https://hermes-agent.nousresearch.com/docs/models/local",
    ),
    "ollama": _row(
        FREE_LOCAL,
        requires_key=False,
        card_required=False,
        label="Ollama (local server)",
        free_note="Local Ollama is free — it is your own GPU/CPU doing the work.",
        cost_note="No API cost. Disk and memory are consumed by the model.",
        notes="Hermes reaches a local Ollama through the custom-endpoint path (http://127.0.0.1:11434/v1).",
        verify_url="https://ollama.com",
    ),
    # ------------------------------------------------------- limited free tiers
    "nous": _row(
        FREE_TIER,
        card_required=False,
        label="Nous Portal / Hermes free tier",
        free_note="A genuinely free tier exists, including an anonymous guest mode that needs no key.",
        cost_note="Nothing, while you stay inside the allowance. Paid plans are separate and never auto-selected.",
        notes="Availability is gated upstream and the allowance can change. If it refuses, the app says so instead of silently switching to a paid provider.",
        signup_url="https://portal.nousresearch.com",
        verify_url="https://hermes-agent.nousresearch.com/docs/getting-started/free-tier",
    ),
    "gemini": _row(
        FREE_TIER,
        card_required=False,
        label="Google AI Studio (Gemini API)",
        free_note="A free tier with rate limits exists for the Gemini API, and it does not require a card.",
        cost_note="Paid tier is optional and only used if you add billing and choose a paid model.",
        notes="Free-tier models and quotas change; Vertex AI (see 'vertex') is a separate, paid product.",
        signup_url="https://aistudio.google.com/app/apikey",
        verify_url="https://ai.google.dev/gemini-api/docs/pricing",
    ),
    "groq": _row(
        FREE_TIER,
        card_required=False,
        label="Groq",
        free_note="A generous free tier with daily request/token limits exists.",
        cost_note="Paid on-demand pricing applies only if you enable billing.",
        notes="Fast inference; free limits are per-model and change often.",
        signup_url="https://console.groq.com/keys",
        verify_url="https://groq.com/pricing",
    ),
    "cerebras": _row(
        FREE_TIER,
        card_required=False,
        label="Cerebras",
        free_note="A free developer tier with rate limits exists.",
        cost_note="Paid plans are separate.",
        notes="Free allowances and model access change; the app never assumes a model exists without asking the provider.",
        signup_url="https://cloud.cerebras.ai",
        verify_url="https://www.cerebras.ai/pricing",
    ),
    "mistral": _row(
        FREE_TIER,
        card_required=False,
        label="Mistral AI",
        free_note="A free 'experiment' tier exists with strict rate limits, and it requires phone verification.",
        cost_note="Beyond the free tier it becomes paid per token.",
        notes="Phone verification is a real requirement, not a technical detail we can skip.",
        signup_url="https://console.mistral.ai/api-keys",
        verify_url="https://mistral.ai/pricing",
    ),
    "openrouter": _row(
        MIXED,
        card_required=False,
        label="OpenRouter",
        free_note="Hundreds of models include ':free' variants that cost nothing (with rate limits).",
        cost_note="Non-free models are billed per token through your OpenRouter account.",
        notes="Free models are rate limited and can be withdrawn; the app marks ':free' models as free and everything else as paid.",
        signup_url="https://openrouter.ai/settings/keys",
        verify_url="https://openrouter.ai/docs/api-reference/limits",
    ),
    "huggingface": _row(
        FREE_TIER,
        card_required=False,
        label="Hugging Face Inference Providers",
        free_note="A monthly free credit allowance exists for Inference Providers.",
        cost_note="Beyond the allowance, usage is billed to your HF account.",
        notes="Credits reset monthly; not a guarantee of uninterrupted service.",
        signup_url="https://huggingface.co/settings/tokens",
        verify_url="https://huggingface.co/docs/inference-providers/pricing",
    ),
    "nvidia": _row(
        FREE_TIER,
        card_required=False,
        label="NVIDIA NIM / build.nvidia.com",
        free_note="Free API credits are offered on build.nvidia.com for development use.",
        cost_note="Production use moves to paid NIM/Enterprise pricing.",
        notes="Credit amounts change; dev-only terms apply.",
        signup_url="https://build.nvidia.com",
        verify_url="https://build.nvidia.com",
    ),
    "ollama-cloud": _row(
        FREE_TIER,
        card_required=False,
        label="Ollama Cloud",
        free_note="A limited free allowance exists with hourly/daily caps.",
        cost_note="Paid subscription removes the caps.",
        notes="This is the *cloud* service; a local Ollama is free and unrelated to it.",
        signup_url="https://ollama.com/settings",
        verify_url="https://ollama.com/pricing",
    ),
    "qwen-oauth": _row(
        FREE_TIER,
        card_required=False,
        label="Qwen (portal sign-in)",
        free_note="Signing in with a Qwen account provides a limited free allowance.",
        cost_note="Heavier usage is billed by Alibaba Cloud.",
        notes="Hermes performs this sign-in from its CLI; the Control Center never handles the OAuth token into the browser.",
        signup_url="https://portal.qwen.ai",
        verify_url="https://portal.qwen.ai",
    ),
    "kilocode": _row(
        FREE_TIER,
        card_required=False,
        label="Kilo Code",
        free_note="A free tier with limited credits is advertised.",
        cost_note="Credit top-ups are paid.",
        notes="Verify current terms — this is a small provider and terms move.",
        signup_url="https://kilocode.ai",
        verify_url="https://kilocode.ai/pricing",
    ),
    "opencode-zen": _row(
        FREE_TIER,
        card_required=False,
        label="OpenCode Zen",
        free_note="Some models are offered free of charge while the service is in preview.",
        cost_note="Other models are paid.",
        notes="Preview pricing can change without notice.",
        signup_url="https://opencode.ai",
        verify_url="https://opencode.ai/docs/zen",
    ),
    "alibaba": _row(
        MIXED,
        card_required=None,
        label="Alibaba Cloud DashScope",
        free_note="DashScope hands out a limited free quota to new accounts.",
        cost_note="Pay-per-token afterwards.",
        notes="Regional variants exist (intl vs China endpoints).",
        signup_url="https://dashscope.aliyun.com",
        verify_url="https://www.alibabacloud.com/help/en/model-studio/pricing",
    ),
    # -------------------------------------------------------------------- paid
    "openai": _row(
        PAID,
        card_required=True,
        label="OpenAI",
        free_note="No ongoing free tier for API usage.",
        cost_note="Billed per token; you must add a payment method.",
        notes="New accounts sometimes receive expiring trial credits — a trial, not a free tier.",
        signup_url="https://platform.openai.com/api-keys",
        verify_url="https://openai.com/api/pricing/",
    ),
    "anthropic": _row(
        PAID,
        card_required=True,
        label="Anthropic",
        free_note="No ongoing free tier for API usage.",
        cost_note="Billed per token with a payment method on file.",
        notes="Console credits, when offered, expire.",
        signup_url="https://console.anthropic.com/settings/keys",
        verify_url="https://www.anthropic.com/pricing",
    ),
    "xai": _row(
        PAID,
        card_required=True,
        label="xAI (Grok)",
        free_note="No ongoing free tier for API usage.",
        cost_note="Billed per token.",
        notes="Promotional credits appear from time to time; they are promotions, not a free tier.",
        signup_url="https://console.x.ai",
        verify_url="https://x.ai/api",
    ),
    "deepseek": _row(
        PAID,
        card_required=True,
        label="DeepSeek",
        free_note="Off-peak discounts, but no free tier.",
        cost_note="Prepaid credit balance.",
        notes="Cheap per token; still paid.",
        signup_url="https://platform.deepseek.com/api_keys",
        verify_url="https://api-docs.deepseek.com/quick_start/pricing",
    ),
    "together": _row(
        PAID,
        card_required=True,
        label="Together AI",
        free_note="A small starting credit for new accounts; not recurring.",
        cost_note="Per-token billing after the credit.",
        signup_url="https://api.together.ai/settings/api-keys",
        verify_url="https://www.together.ai/pricing",
    ),
    "fireworks": _row(
        PAID,
        card_required=True,
        label="Fireworks AI",
        free_note="New-account credits only.",
        cost_note="Per-token billing.",
        signup_url="https://fireworks.ai",
        verify_url="https://fireworks.ai/pricing",
    ),
    "deepinfra": _row(
        PAID,
        card_required=True,
        label="DeepInfra",
        free_note="No recurring free tier.",
        cost_note="Per-token billing with prepaid balance.",
        signup_url="https://deepinfra.com/dash/api_keys",
        verify_url="https://deepinfra.com/pricing",
    ),
    "novita": _row(
        PAID,
        card_required=True,
        label="Novita AI",
        free_note="Occasional signup credits.",
        cost_note="Per-token billing.",
        signup_url="https://novita.ai",
        verify_url="https://novita.ai/pricing",
    ),
    "arcee": _row(
        PAID,
        card_required=True,
        label="Arcee AI",
        free_note="No recurring free tier that we could verify.",
        cost_note="Per-token billing.",
        signup_url="https://arcee.ai",
        verify_url="https://arcee.ai/pricing",
    ),
    "gmi": _row(
        PAID,
        card_required=True,
        label="GMI Cloud",
        free_note="Credits sometimes offered at signup.",
        cost_note="Per-token billing.",
        signup_url="https://gmicloud.ai",
        verify_url="https://gmicloud.ai/pricing",
    ),
    "zai": _row(
        PAID,
        card_required=True,
        label="Z.ai (GLM)",
        free_note="A free tier has been advertised at times; treat it as changeable.",
        cost_note="Paid coding plans and API billing.",
        signup_url="https://z.ai",
        verify_url="https://z.ai/subscribe",
    ),
    "minimax": _row(
        PAID,
        card_required=True,
        label="MiniMax",
        free_note="Trial credits only.",
        cost_note="Per-token billing.",
        signup_url="https://platform.minimax.io",
        verify_url="https://platform.minimax.io/docs/pricing",
    ),
    "stepfun": _row(
        PAID,
        card_required=True,
        label="StepFun",
        free_note="Trial credits only.",
        cost_note="Per-token billing.",
        signup_url="https://platform.stepfun.com",
        verify_url="https://platform.stepfun.com/docs/pricing",
    ),
    "tencent-tokenhub": _row(
        PAID,
        card_required=True,
        label="Tencent TokenHub",
        free_note="Trial quota only.",
        cost_note="Paid afterwards.",
        signup_url="https://cloud.tencent.com",
        verify_url="https://cloud.tencent.com/product/hunyuan",
    ),
    "upstage": _row(
        PAID,
        card_required=True,
        label="Upstage",
        free_note="Trial credits only.",
        cost_note="Per-token billing.",
        signup_url="https://console.upstage.ai",
        verify_url="https://www.upstage.ai/pricing",
    ),
    "xiaomi": _row(
        PAID,
        card_required=True,
        label="Xiaomi (MiMo)",
        free_note="Unknown / changes frequently.",
        cost_note="Paid API access.",
        signup_url="https://xiaomi.com",
        verify_url="https://xiaomi.com",
    ),
    "copilot": _row(
        PAID,
        card_required=True,
        label="GitHub Copilot",
        free_note="A free Copilot tier exists for some accounts (students, some OSS maintainers, limited monthly requests).",
        cost_note="Copilot is a subscription; using it here uses that subscription's allowance.",
        notes=(
            "Nothing new is billed by this app, but it is not free for most accounts — labelled PAID on purpose. "
            "Hermes reads GH_TOKEN / GITHUB_TOKEN / COPILOT_GITHUB_TOKEN."
        ),
        signup_url="https://github.com/settings/copilot",
        verify_url="https://docs.github.com/copilot/about-github-copilot/plans-for-github-copilot",
    ),
    "copilot-acp": _row(
        PAID,
        card_required=True,
        label="GitHub Copilot (ACP)",
        free_note="Same subscription as Copilot.",
        cost_note="Uses your Copilot plan.",
        notes="Requires the Copilot CLI binary on this host.",
        verify_url="https://docs.github.com/copilot",
    ),
    "openai-codex": _row(
        PAID,
        card_required=True,
        label="OpenAI Codex (ChatGPT sign-in)",
        free_note="No free API tier; it borrows a ChatGPT subscription.",
        cost_note="Your ChatGPT plan pays for the usage.",
        signup_url="https://chatgpt.com",
        verify_url="https://openai.com/chatgpt/pricing/",
    ),
    "bedrock": _row(
        PAID,
        card_required=True,
        label="Amazon Bedrock",
        free_note="No free tier for model inference.",
        cost_note="AWS billing per token plus any provisioned throughput.",
        notes="Requires AWS credentials; Hermes can use your configured profile or keys.",
        signup_url="https://aws.amazon.com/bedrock/",
        verify_url="https://aws.amazon.com/bedrock/pricing/",
    ),
    "vertex": _row(
        PAID,
        card_required=True,
        label="Google Vertex AI",
        free_note="Vertex is the paid Google Cloud path (AI Studio is the free one).",
        cost_note="Google Cloud billing per token.",
        notes="Requires a GCP project and credentials. Free GCP trial credits are a trial, not a free tier.",
        signup_url="https://console.cloud.google.com/vertex-ai",
        verify_url="https://cloud.google.com/vertex-ai/pricing",
    ),
    "azure-foundry": _row(
        PAID,
        card_required=True,
        label="Azure AI Foundry",
        free_note="Trial credits only.",
        cost_note="Azure billing.",
        signup_url="https://ai.azure.com",
        verify_url="https://azure.microsoft.com/pricing/details/cognitive-services/",
    ),
    "ai-gateway": _row(
        PAID,
        card_required=True,
        label="Vercel AI Gateway",
        free_note="Depends entirely on the models you route through it.",
        cost_note="Billed by the upstream model provider, through Vercel.",
        signup_url="https://vercel.com/docs/ai-gateway",
        verify_url="https://vercel.com/docs/ai-gateway/pricing",
    ),
    "actual": _row(
        UNKNOWN,
        card_required=None,
        label="Actual Computer",
        free_note="We could not verify a free tier.",
        cost_note="Unverified pricing.",
        notes="Treat as unverified until you read the provider's own terms.",
        signup_url="https://actual.inc",
        verify_url="https://actual.inc",
    ),
    "alibaba-coding-plan": _row(
        PAID,
        card_required=True,
        label="Alibaba Cloud (Coding Plan)",
        free_note="Plan-based access; no free tier verified.",
        cost_note="Subscription.",
        verify_url="https://www.alibabacloud.com",
    ),
    "kimi-coding": _row(
        PAID,
        card_required=True,
        label="Kimi (Moonshot) coding plan",
        free_note="No free tier verified for the coding plan.",
        cost_note="Subscription / pay-as-you-go.",
        verify_url="https://platform.moonshot.ai",
    ),
    "commandcode": _row(
        UNKNOWN,
        card_required=None,
        label="CommandCode",
        free_note="Unverified.",
        cost_note="Unverified.",
        verify_url="https://commandcode.ai",
    ),
    "meta-ai": _row(
        UNKNOWN,
        card_required=None,
        label="Meta AI",
        free_note="Unverified.",
        cost_note="Unverified.",
        verify_url="https://ai.meta.com",
    ),
    "router": _row(
        UNKNOWN,
        card_required=None,
        label="Router (RAMp)",
        free_note="Unverified.",
        cost_note="Unverified.",
        verify_url="https://ramp.dev",
    ),
    "solstice": _row(
        UNKNOWN,
        card_required=None,
        label="Solstice",
        free_note="Unverified.",
        cost_note="Unverified.",
        verify_url="",
    ),
}

#: Upstream sometimes reports a provider slug that differs from the plugin directory.
ALIASES = {
    "github-copilot": "copilot",
    "github_copilot": "copilot",
    "copilot_chat": "copilot",
    "ollama_cloud": "ollama-cloud",
    "ollama-cloud": "ollama-cloud",
    "azure": "azure-foundry",
    "azure_ai": "azure-foundry",
    "chatgpt": "openai-codex",
    "codex": "openai-codex",
    "google": "gemini",
    "google-ai": "gemini",
    "google_ai": "gemini",
    "vllm": "custom",
    "llamacpp": "llamacpp",
    "llama.cpp": "llamacpp",
    "local": "custom",
    "lm-studio": "lmstudio",
    "lm_studio": "lmstudio",
    "nous-portal": "nous",
    "nousresearch": "nous",
    "open-router": "openrouter",
}

LOCAL_HOST_PATTERN = re.compile(
    r"^(https?://)?(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|::1|10\.|172\.(1[6-9]|2\d|3[01])\.|192\.168\.|169\.254\.|\.local|host\.docker\.internal)",
    re.IGNORECASE,
)


@lru_cache(maxsize=1)
def registry() -> dict[str, dict]:
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def known_provider_ids() -> list[str]:
    ids = set(registry()) | set(CLASSIFICATION)
    return sorted(ids)


def normalize_id(provider: str) -> str:
    cleaned = (provider or "").strip().lower()
    if not cleaned:
        return ""
    cleaned = cleaned.split("/", 1)[0].strip()
    cleaned = ALIASES.get(cleaned, cleaned)
    if cleaned.startswith("custom:"):
        return "custom"
    return cleaned


def is_local_endpoint(base_url: str | None) -> bool:
    if not base_url:
        return False
    return bool(LOCAL_HOST_PATTERN.match(base_url.strip()))


def classify_provider(provider: str, *, base_url: str | None = None) -> dict[str, Any]:
    """Everything the UI needs to label a provider honestly."""

    identifier = normalize_id(provider)
    entry = CLASSIFICATION.get(identifier, {})
    registry_entry = registry().get(identifier, {}) or registry().get(provider, {})
    label = entry.get("label") or registry_entry.get("display_name") or (identifier or "Unknown provider")

    payload: dict[str, Any] = {
        "id": identifier or provider,
        "raw_id": provider,
        "label": label,
        "env_vars": registry_entry.get("env_vars") or [],
        "default_model": registry_entry.get("default_model") or "",
        "base_url": registry_entry.get("base_url") or "",
        "source": "curated" if entry else ("registry" if registry_entry else "heuristic"),
    }

    if entry:
        payload.update(entry)
    else:
        payload.update(
            _row(
                UNKNOWN,
                card_required=None,
                label=label,
                free_note="We have not verified this provider's pricing.",
                cost_note="Unverified — assume it may cost money.",
                notes=(
                    "This provider is shipped by Hermes but is not in the Control Center's verified cost table. "
                    "Treat it as unverified: check the provider's own pricing before using it."
                ),
            )
        )

    # A local endpoint wins over the provider name: whatever is billed elsewhere,
    # nothing leaves this machine, so it cannot cost money per request.
    if is_local_endpoint(base_url) or base_url and is_local_endpoint(payload.get("base_url", "")):
        payload.update(
            billing=FREE_LOCAL,
            cost_label=BILLING_LABELS[FREE_LOCAL],
            requires_key=False,
            card_required=False,
            free_note="The endpoint is on this host or your own network, so requests never reach a paid API.",
            cost_note="No per-request cost. CPU, RAM and electricity only.",
            notes="Confirmed local by address, which is stronger evidence than any provider name.",
            source="heuristic:local-endpoint",
        )
    return payload


def classify_model(provider: str, model: str, *, base_url: str | None = None) -> dict[str, Any]:
    """Model-level classification: catches things like OpenRouter's ':free' variants."""

    classification = classify_provider(provider, base_url=base_url)
    name = (model or "").strip()
    if name.endswith(":free") or ":free" in name:
        classification.update(
            billing=FREE_TIER,
            cost_label=BILLING_LABELS[FREE_TIER],
            cost_note="This specific model variant is free on the provider, subject to its rate limits.",
            notes="Free variants can disappear when a provider changes its catalogue.",
        )
    return classification


def is_free_billing(billing: str) -> bool:
    return billing in {FREE_LOCAL, FREE_TIER}


def tier_legend() -> list[dict[str, str]]:
    """The vocabulary the UI is allowed to use about money.

    Kept in one place so a new page cannot invent its own optimistic wording: every
    label here says exactly what the tier costs and who pays.
    """

    return [
        {
            "tier": FREE_LOCAL,
            "label": "FREE",
            "means": "Runs on your own machine. No account, no key, no bill — it uses your CPU/GPU and disk.",
        },
        {
            "tier": FREE_TIER,
            "label": "LIMITED FREE TIER",
            "means": "A free allowance from a provider you sign up to. Free while the allowance lasts; "
            "the provider may rate limit you, change the terms, or ask for a card.",
        },
        {
            "tier": MIXED,
            "label": "FREE + PAID",
            "means": "Some models are free (often tagged ':free'), the rest are billed. The app marks each model.",
        },
        {
            "tier": PAID,
            "label": "PAID",
            "means": "Billed to the account that owns the key. Never activated by this app on its own.",
        },
        {
            "tier": UNKNOWN,
            "label": "UNVERIFIED",
            "means": "Nobody has verified the pricing. Treat it as paid until proven otherwise.",
        },
    ]


def cost_tiers() -> list[dict]:
    return [
        {"id": FREE_LOCAL, "label": BILLING_LABELS[FREE_LOCAL], "detail": "Runs on your machine. No account, no key, no per-request cost."},
        {"id": FREE_TIER, "label": BILLING_LABELS[FREE_TIER], "detail": "Free with limits, rate limits or usage caps, and it can change at any time."},
        {"id": MIXED, "label": BILLING_LABELS[MIXED], "detail": "Some models are free, others are billed."},
        {"id": PAID, "label": BILLING_LABELS[PAID], "detail": "Costs money per request or per subscription."},
        {"id": UNKNOWN, "label": BILLING_LABELS[UNKNOWN], "detail": "We could not verify. Assume it may cost money."},
    ]


def catalog_snapshot() -> dict:
    """Full curated view (no live state) — used by the providers page."""

    providers = []
    for identifier in known_provider_ids():
        entry = classify_provider(identifier)
        entry["shipped_by_upstream"] = identifier in registry()
        providers.append(entry)
    providers.sort(key=lambda item: ({"free_local": 0, "free_tier": 1, "mixed": 2, "unknown": 3, "paid": 4}.get(item["billing"], 5), item["label"].lower()))
    return {
        "providers": providers,
        "tiers": cost_tiers(),
        "verified": VERIFIED,
        "generated_from": str(DATA_FILE.name),
        "notes": [
            "FREE means $0 with no account and no key.",
            "LIMITED FREE TIER means free with usage limits and no card required (verify per provider).",
            "PAID means it costs money — trials and expiring credits are still PAID here.",
            "UNVERIFIED means we could not confirm; the app refuses to call it free.",
        ],
    }


def free_route_summary(container) -> dict:
    """What free capacity this install actually has right now."""

    routes: list[dict] = []
    capability = container.db.get_setting("local_capability_summary", {}) or {}
    verdict = (capability or {}).get("verdict", {}) if isinstance(capability, dict) else {}
    local_state = verdict.get("state", "unknown")
    routes.append(
        {
            "id": "local",
            "label": "Local model on this machine",
            "available": local_state == "supported",
            "state": local_state,
            "detail": verdict.get("headline")
            or "Run the Local check to see whether a local model fits on this host.",
            "cost_label": BILLING_LABELS[FREE_LOCAL],
            "requires_key": False,
        }
    )
    nous_enabled = bool(container.db.get_setting("nous_free_tier_enabled", False))
    routes.append(
        {
            "id": "nous-free-tier",
            "label": "Nous / Hermes free tier",
            "available": nous_enabled,
            "state": "enabled" if nous_enabled else "not enabled",
            "detail": (
                "Enabled: the runtime asks the free tier first. Availability and limits are controlled upstream."
                if nous_enabled
                else "Off. Turning it on makes the runtime try the free tier before anything paid."
            ),
            "cost_label": BILLING_LABELS[FREE_TIER],
            "requires_key": False,
        }
    )
    stored = container.secrets.list_rows() if hasattr(container, "secrets") else []
    free_keys = []
    for row in stored:
        classification = classify_provider(row["name"])
        if classification["billing"] in {FREE_TIER, MIXED, FREE_LOCAL}:
            free_keys.append({"name": row["name"], "label": classification["label"], "billing": classification["billing"]})
    routes.append(
        {
            "id": "free-tier-keys",
            "label": "Provider free tiers with your own key",
            "available": bool(free_keys),
            "state": f"{len(free_keys)} stored" if free_keys else "none stored",
            "detail": (
                "Keys stored for: " + ", ".join(item["label"] for item in free_keys)
                if free_keys
                else "Google AI Studio, Groq, Cerebras, Mistral, Hugging Face and OpenRouter all offer free allowances."
            ),
            "cost_label": BILLING_LABELS[FREE_TIER],
            "requires_key": True,
        }
    )
    available = [route for route in routes if route["available"]]
    return {
        "routes": routes,
        "available_count": len(available),
        "headline": (
            f"{len(available)} free route(s) available on this install."
            if available
            else "No free route is ready yet — the app will say so instead of spending money."
        ),
        "paid_unlocked": container.paid_unlocked,
        "free_mode": container.free_mode,
    }
