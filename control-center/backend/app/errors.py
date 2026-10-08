"""Translate failures into something a human can act on.

Every error the UI shows carries the same shape:

    {
      "error": "provider_key_missing",     # stable machine code
      "title": "No API key for OpenRouter", # short, human
      "message": "...",                     # what happened, no jargon
      "hint": "...",                        # what to do next, specific
      "actions": [{"label": "Add key", "kind": "route", "target": "keys"}],
      "retryable": false,
      "status": 400
    }

The mapping covers the failure modes the brief calls out explicitly: missing key,
invalid key, provider or model unavailable, not enough RAM/CPU, network down,
database down, Hermes failing to start, expired credentials and rate limits.
"""

from __future__ import annotations

import re
from typing import Any

from .security import get_redactor


class HumanError(Exception):
    """An exception that already carries the user-facing payload."""

    def __init__(self, payload: dict[str, Any], *, status: int = 400) -> None:
        super().__init__(payload.get("message", "Request failed"))
        self.payload = payload
        self.status = status

    def to_response(self) -> dict[str, Any]:
        return {**self.payload, "status": self.status}


def human_error(
    code: str,
    title: str,
    message: str,
    *,
    hint: str = "",
    actions: list[dict] | None = None,
    retryable: bool = False,
    status: int = 400,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "error": code,
        "title": title,
        "message": get_redactor().redact(message),
        "hint": get_redactor().redact(hint),
        "detail": get_redactor().redact(detail)[:2000],
        "actions": actions or [],
        "retryable": retryable,
        "status": status,
    }


# --------------------------------------------------------------------- patterns

_PROVIDER_HINTS = {
    "openrouter": ("OpenRouter", "keys", "https://openrouter.ai/settings/keys"),
    "openai": ("OpenAI", "keys", "https://platform.openai.com/api-keys"),
    "anthropic": ("Anthropic", "keys", "https://console.anthropic.com/settings/keys"),
    "google": ("Google AI Studio", "keys", "https://aistudio.google.com/app/apikey"),
    "gemini": ("Google AI Studio", "keys", "https://aistudio.google.com/app/apikey"),
    "groq": ("Groq", "keys", "https://console.groq.com/keys"),
    "mistral": ("Mistral", "keys", "https://console.mistral.ai/api-keys"),
    "deepseek": ("DeepSeek", "keys", "https://platform.deepseek.com/api_keys"),
    "xai": ("xAI", "keys", "https://console.x.ai"),
    "cerebras": ("Cerebras", "keys", "https://cloud.cerebras.ai"),
    "together": ("Together", "keys", "https://api.together.ai/settings/api-keys"),
    "nous": ("Nous Portal", "providers", "https://portal.nousresearch.com"),
}

#: The agent itself saying "I have no AI provider yet" — the most common first-run
#: state, and *not* the same thing as a missing key for a chosen provider. Matched
#: before the key patterns, which would otherwise hijack the message.
NO_PROVIDER_PATTERNS = (
    re.compile(r"not connected to any ai provider", re.I),
    re.compile(r"no ai provider (is )?(configured|connected|selected)", re.I),
    re.compile(r"no provider (is )?(configured|connected|selected)", re.I),
    re.compile(r"run `?hermes model`?", re.I),
    re.compile(r"no model (is )?(configured|selected|set)", re.I),
)

MISSING_KEY_PATTERNS = (
    re.compile(r"no api key", re.I),
    re.compile(r"api key (is )?(not|missing|required)", re.I),
    re.compile(r"missing (credential|api[_ ]?key)", re.I),
    re.compile(r"not authenticated", re.I),
    re.compile(r"no credentials", re.I),
)

INVALID_KEY_PATTERNS = (
    re.compile(r"invalid api key", re.I),
    re.compile(r"incorrect api key", re.I),
    re.compile(r"api key (is )?(invalid|expired|revoked)", re.I),
    re.compile(r"authentication_error", re.I),
    re.compile(r"unauthorized", re.I),
)

EXPIRED_PATTERNS = (
    re.compile(r"token (has )?expired", re.I),
    re.compile(r"credentials? (have )?expired", re.I),
    re.compile(r"refresh token", re.I),
    re.compile(r"oauth.*expired", re.I),
)

MODEL_PATTERNS = (
    re.compile(r"model[_ ]not[_ ]found", re.I),
    re.compile(r"unknown model", re.I),
    re.compile(r"no such model", re.I),
    re.compile(r"model .* does not exist", re.I),
    re.compile(r"invalid model", re.I),
    re.compile(r"does not support (the )?model", re.I),
)

RATE_PATTERNS = (
    re.compile(r"rate[_ ]limit", re.I),
    re.compile(r"too many requests", re.I),
    re.compile(r"429", re.I),
    re.compile(r"quota", re.I),
    re.compile(r"insufficient[_ ]quota", re.I),
)

NETWORK_PATTERNS = (
    re.compile(r"connection refused", re.I),
    re.compile(r"connection reset", re.I),
    re.compile(r"name or service not known", re.I),
    re.compile(r"temporary failure in name resolution", re.I),
    re.compile(r"ssl|certificate", re.I),
    re.compile(r"timed? ?out|timeout", re.I),
    re.compile(r"network is unreachable", re.I),
)

MEMORY_PATTERNS = (
    re.compile(r"out of memory", re.I),
    re.compile(r"cannot allocate memory", re.I),
    re.compile(r"oom", re.I),
    re.compile(r"insufficient memory", re.I),
    re.compile(r"not enough (ram|memory|vram)", re.I),
)

CONTEXT_PATTERNS = (
    re.compile(r"context length", re.I),
    re.compile(r"maximum context", re.I),
    re.compile(r"too many tokens", re.I),
    re.compile(r"prompt is too long", re.I),
)


def _provider_label(provider: str) -> tuple[str, str, str]:
    key = (provider or "").lower().split("/")[0]
    for needle, value in _PROVIDER_HINTS.items():
        if needle in key:
            return value
    return (provider or "the selected provider", "providers", "")


def no_provider_selected(detail: str = "") -> dict[str, Any]:
    """The agent has no model/provider yet — a free route fixes this, not money."""

    return human_error(
        "no_provider_selected",
        "The agent has no AI provider selected yet",
        "Hermes answered, but it has nothing to think with: no model and no provider are configured.",
        hint=(
            "Pick a free route on the Models page — enable the Nous free tier (no API key needed), run a local model "
            "if your hardware fits, or store a key for a provider that has a free tier (Google AI Studio, Groq, "
            "Cerebras, Mistral, OpenRouter). FREE MODE keeps paid providers switched off until you unlock them."
        ),
        actions=[
            {"label": "Choose a model", "kind": "route", "target": "models"},
            {"label": "Add a free provider key", "kind": "route", "target": "providers"},
        ],
        retryable=True,
        status=409,
        detail=detail,
    )


def provider_key_missing(provider: str, env_names: list[str] | None = None) -> dict[str, Any]:
    label, route, docs = _provider_label(provider)
    names = ", ".join(env_names or []) or "the documented API key variable"
    return human_error(
        "provider_key_missing",
        f"No key stored for {label}",
        f"{label} needs an API key before it can answer. None is configured for {names}.",
        hint=(
            "Add your own key on the Keys page — it is encrypted on the server, never sent to the browser, "
            "and never written to logs. Providers with a genuinely free tier are labelled FREE / LIMITED FREE TIER."
        ),
        actions=[
            {"label": "Open API keys", "kind": "route", "target": "keys"},
            {"label": "Choose a free model instead", "kind": "route", "target": "models"},
        ],
        status=409,
    )


def provider_key_invalid(provider: str, detail: str = "") -> dict[str, Any]:
    label, route, docs = _provider_label(provider)
    return human_error(
        "provider_key_invalid",
        f"{label} rejected the key",
        f"{label} answered with an authentication error, so the stored key is wrong, revoked, or for another project.",
        hint="Replace the key on the Keys page. The old value is overwritten; nothing else needs changing.",
        actions=[
            {"label": "Replace key", "kind": "route", "target": "keys"},
            *([{"label": "Provider dashboard", "kind": "link", "target": docs}] if docs else []),
        ],
        status=401,
        detail=detail,
    )


def provider_credentials_expired(provider: str) -> dict[str, Any]:
    label, _route, docs = _provider_label(provider)
    return human_error(
        "provider_credentials_expired",
        f"{label} sign-in expired",
        f"The stored {label} credentials can no longer be used. This is normal for OAuth providers after a while.",
        hint="Re-authorise the provider — the provider page has a Test button that reports the same detail.",
        actions=[
            {"label": "Open providers", "kind": "route", "target": "providers"},
            *([{"label": "Re-authorise", "kind": "link", "target": docs}] if docs else []),
        ],
        status=401,
    )


def model_unavailable(model: str, provider: str = "", detail: str = "") -> dict[str, Any]:
    return human_error(
        "model_unavailable",
        f"Model '{model}' is not available" if model else "The requested model is not available",
        (
            f"{provider or 'The provider'} does not serve '{model}', or your account cannot access it. "
            "Free models are frequently renamed or retired."
        ),
        hint="Pick a model from the list this provider actually reports, or switch to your free default.",
        actions=[
            {"label": "Choose another model", "kind": "route", "target": "models"},
            {"label": "Test the provider", "kind": "route", "target": "providers"},
        ],
        retryable=True,
        status=404,
        detail=detail,
    )


def provider_unreachable(provider: str, detail: str = "") -> dict[str, Any]:
    label, _route, _docs = _provider_label(provider)
    return human_error(
        "provider_unreachable",
        f"Could not reach {label}",
        "The request never completed: DNS, TLS or the network failed before the provider answered.",
        hint=(
            "Check outbound internet access from this host. Behind a firewall or in a locked-down container, "
            "set HTTP(S)_PROXY for the Hermes runtime."
        ),
        actions=[{"label": "Run diagnostics", "kind": "route", "target": "diagnostics"}],
        retryable=True,
        status=503,
        detail=detail,
    )


def rate_limited(provider: str = "", retry_after: int | None = None) -> dict[str, Any]:
    label, _route, _docs = _provider_label(provider)
    wait = f" Retry in about {retry_after} seconds." if retry_after else ""
    return human_error(
        "rate_limited",
        f"{label} is rate limiting this install",
        f"The provider accepted the key but refused the request because a limit or quota was hit.{wait}",
        hint=(
            "Free tiers have small allowances. Wait, switch to a local model, or add a second free provider — "
            "paid providers are never used automatically."
        ),
        actions=[
            {"label": "Models", "kind": "route", "target": "models"},
            {"label": "Providers", "kind": "route", "target": "providers"},
        ],
        retryable=True,
        status=429,
    )


def insufficient_resources(needed: str = "", available: str = "", detail: str = "") -> dict[str, Any]:
    numbers = f" Needed: {needed}. Available: {available}." if needed or available else ""
    return human_error(
        "insufficient_resources",
        "Not enough RAM or CPU on this host",
        (
            "The model or the runtime needs more memory/compute than this machine has, so the process was killed "
            f"or refused to start.{numbers}"
        ),
        hint=(
            "Options that stay at $0: use a smaller quantised model, move to a host with more RAM, or use a "
            "provider's free tier instead of local inference. The Deployment page lists what each host can really do."
        ),
        actions=[
            {"label": "Local models", "kind": "route", "target": "models"},
            {"label": "Deployment reality", "kind": "route", "target": "deploy"},
        ],
        status=507,
        detail=detail,
    )


def network_failure(detail: str = "") -> dict[str, Any]:
    return human_error(
        "network_failure",
        "Network problem",
        "The request could not be completed because of a network failure (DNS, routing, TLS or a proxy).",
        hint="If this host has no internet access, local models and the free-tier route will not work either.",
        retryable=True,
        status=503,
        detail=detail,
    )


def database_failure(detail: str = "") -> dict[str, Any]:
    return human_error(
        "database_failure",
        "The Control Center database is not writable",
        "Settings, sessions, keys and logs are stored in the Control Center database and it just failed.",
        hint=(
            "Check that CC_HOME is writable and has free space, or set CC_DATABASE_URL to a reachable PostgreSQL. "
            "Diagnostics → database shows the exact error."
        ),
        actions=[{"label": "Diagnostics", "kind": "route", "target": "diagnostics"}],
        status=500,
        detail=detail,
    )


def runtime_unavailable(detail: str = "") -> dict[str, Any]:
    return human_error(
        "runtime_unavailable",
        "The Hermes runtime is not running",
        "This action needs the Hermes agent process, and it is currently stopped, starting, or unreachable.",
        hint="Start it from the Dashboard. If it exits immediately, the runtime log tail on the Dashboard shows why.",
        actions=[{"label": "Dashboard", "kind": "route", "target": "dashboard"}],
        retryable=True,
        status=503,
        detail=detail,
    )


def runtime_start_failed(detail: str = "") -> dict[str, Any]:
    return human_error(
        "runtime_start_failed",
        "Hermes failed to start",
        "The runtime process exited before it finished starting up.",
        hint=(
            "Most common causes: dependencies are not installed for the detected Python, the port is already taken, "
            "or $HERMES_HOME is not writable. The Diagnostics page installs dependencies and shows the exact command."
        ),
        actions=[
            {"label": "Diagnostics", "kind": "route", "target": "diagnostics"},
            {"label": "Deployment", "kind": "route", "target": "deploy"},
        ],
        status=503,
        detail=detail,
    )


def context_too_long(model: str = "", detail: str = "") -> dict[str, Any]:
    return human_error(
        "context_too_long",
        "The conversation no longer fits the model",
        f"{model or 'This model'} ran out of context window, so the request was rejected instead of being truncated silently.",
        hint="Start a new conversation, or clear older turns. Memory notes can be kept in the Memory page instead.",
        actions=[{"label": "Back to chat", "kind": "route", "target": "chat"}],
        retryable=True,
        status=413,
        detail=detail,
    )


def cancelled(what: str = "The request") -> dict[str, Any]:
    return human_error(
        "cancelled",
        "Stopped",
        f"{what} was stopped before it finished.",
        hint="Nothing is left running. Retry when you are ready.",
        retryable=True,
        status=499,
    )


def free_mode_blocked(provider: str, model: str = "") -> dict[str, Any]:
    label, _route, _docs = _provider_label(provider)
    return human_error(
        "free_mode_blocked",
        "FREE MODE blocked a paid provider",
        (
            f"{label} bills per request, and FREE MODE is on. The Control Center will not spend your money "
            "without an explicit unlock."
        ),
        hint=(
            "Either pick a free model (local, free tier, or a provider free tier with your own key), or turn OFF "
            "Free Mode in Settings and confirm — the confirmation is deliberate."
        ),
        actions=[
            {"label": "Models", "kind": "route", "target": "models"},
            {"label": "Settings", "kind": "route", "target": "settings"},
        ],
        status=402,
    )


def classify(
    *,
    status: int | None = None,
    message: str = "",
    provider: str = "",
    model: str = "",
) -> dict[str, Any]:
    """Best-effort mapping of an upstream failure onto a human payload."""

    text = message or ""
    if any(pattern.search(text) for pattern in NO_PROVIDER_PATTERNS):
        return no_provider_selected(text)
    if status == 429 or any(pattern.search(text) for pattern in RATE_PATTERNS):
        return rate_limited(provider)
    if any(pattern.search(text) for pattern in MEMORY_PATTERNS):
        return insufficient_resources(detail=text)
    if any(pattern.search(text) for pattern in CONTEXT_PATTERNS):
        return context_too_long(model, detail=text)
    if any(pattern.search(text) for pattern in EXPIRED_PATTERNS):
        return provider_credentials_expired(provider)
    if any(pattern.search(text) for pattern in MISSING_KEY_PATTERNS):
        return provider_key_missing(provider)
    if status in {401, 403} or any(pattern.search(text) for pattern in INVALID_KEY_PATTERNS):
        return provider_key_invalid(provider, detail=text)
    if any(pattern.search(text) for pattern in MODEL_PATTERNS) or status == 404:
        return model_unavailable(model, provider, detail=text)
    if any(pattern.search(text) for pattern in NETWORK_PATTERNS):
        return provider_unreachable(provider, detail=text)
    if status and status >= 500:
        return provider_unreachable(provider, detail=text)
    return human_error(
        "upstream_error",
        "The request failed",
        text or "The upstream service returned an error.",
        hint="Check Logs → Hermes agent for the full request trail.",
        retryable=True,
        status=status or 502,
        detail=text,
    )
