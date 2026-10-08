"""Security primitives: password hashing, session cookies, CSRF, secret storage,
redaction and rate limiting.

Design notes
------------
* Passwords: scrypt (memory-hard) with a per-user salt; constant-time verification.
* Sessions: the cookie carries an opaque random id plus a HMAC signature over
  (id, expiry). The database stores the id and a hash of the session token's
  second factor, so a database dump alone cannot be replayed as a cookie.
* API keys: encrypted with Fernet (AES-128-CBC + HMAC-SHA256) using a master key
  created on first run with mode 0600 and never sent anywhere. If ``cryptography``
  is unavailable the store refuses to save secrets instead of writing plaintext.
* Redaction: every log line and every error surfaced to the UI passes through a
  redactor that knows the shapes of well-known API keys and, additionally, the
  literal values currently stored in the vault.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable

# --------------------------------------------------------------------- hashing

SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


class SecurityError(RuntimeError):
    """Raised for security-relevant failures (never leaks secret material)."""


#: OpenSSL caps scrypt's working memory at 32 MiB by default, and 128*N*r for our
#: parameters is ~33.5 MiB — so without an explicit allowance every hash would fail
#: with "memory limit exceeded". We pass 4x the theoretical need instead of lowering
#: N, because lowering N would weaken the hash to make an error go away.
SCRYPT_MAXMEM = 128 * SCRYPT_N * SCRYPT_R * 4


def hash_password(password: str) -> str:
    if not password:
        raise SecurityError("Password must not be empty.")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )
    return "$".join(
        [
            "scrypt",
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        ]
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=SCRYPT_DKLEN,
            maxmem=max(SCRYPT_MAXMEM, 128 * int(n) * int(r) * 4),
        )
        return hmac.compare_digest(digest, base64.b64decode(digest_b64))
    except (ValueError, TypeError, MemoryError):
        return False


def password_strength_problems(password: str, *, minimum: int = 10) -> list[str]:
    problems: list[str] = []
    if len(password) < minimum:
        problems.append(f"Use at least {minimum} characters.")
    if password.lower() in {"password", "password123", "changeme", "admin", "hermes", "controlcenter"}:
        problems.append("That password is on every wordlist.")
    if len(set(password)) < 4:
        problems.append("Use a few more distinct characters.")
    return problems


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


# --------------------------------------------------------------------- session


def new_token(bytes_: int = 32) -> str:
    return secrets.token_urlsafe(bytes_)


def token_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SignedValue:
    value: str
    signature: str

    def encode(self) -> str:
        return f"{self.value}.{self.signature}"


def sign_value(value: str, secret: str) -> SignedValue:
    signature = hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()
    return SignedValue(value=value, signature=signature)


def unsign_value(encoded: str, secret: str) -> str | None:
    if "." not in encoded:
        return None
    value, _, signature = encoded.rpartition(".")
    expected = hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    return value


# ------------------------------------------------------------------ secret box


class EncryptionUnavailable(SecurityError):
    pass


class SecretBox:
    """Symmetric encryption for provider keys held at rest."""

    def __init__(self, key_path) -> None:
        self.key_path = key_path
        self._fernet = None
        self._available = False
        try:  # optional dependency
            from cryptography.fernet import Fernet

            key = self._load_or_create_key(Fernet)
            self._fernet = Fernet(key)
            self._available = True
        except ImportError:
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    @property
    def backend_name(self) -> str:
        return "fernet (AES-128-CBC + HMAC-SHA256)" if self._available else "unavailable"

    def _load_or_create_key(self, fernet_cls) -> bytes:
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        if self.key_path.exists():
            raw = self.key_path.read_bytes().strip()
            if raw:
                return raw
        key = fernet_cls.generate_key()
        self.key_path.write_bytes(key)
        try:
            os.chmod(self.key_path, 0o600)
        except OSError:  # pragma: no cover - non-POSIX filesystems
            pass
        return key

    def encrypt(self, plaintext: str) -> str:
        if not self._available or self._fernet is None:
            raise EncryptionUnavailable(
                "Encrypted key storage needs the 'cryptography' package. "
                "Install it with: pip install 'cryptography' (or pip install 'hermes-control-center[crypto]')."
            )
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        if not self._available or self._fernet is None:
            raise EncryptionUnavailable("Encrypted key storage is unavailable (cryptography is not installed).")
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except Exception as exc:  # noqa: BLE001 - cryptography raises several types
            raise SecurityError(
                "Stored secret could not be decrypted with the current master key. "
                "If you moved CC_HOME without moving secrets.key, re-enter the key."
            ) from exc


# ------------------------------------------------------------------- redaction

SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(sk-[A-Za-z0-9_\-]{12,})"),
    re.compile(r"(sk-ant-[A-Za-z0-9_\-]{12,})"),
    re.compile(r"(sk-or-v1-[A-Za-z0-9_\-]{12,})"),
    re.compile(r"(ghp_[A-Za-z0-9]{20,})"),
    re.compile(r"(github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"(gho_[A-Za-z0-9]{20,})"),
    re.compile(r"(hf_[A-Za-z0-9]{20,})"),
    re.compile(r"(AIza[0-9A-Za-z_\-]{20,})"),
    re.compile(r"(xox[baprs]-[A-Za-z0-9\-]{10,})"),
    re.compile(r"(ya29\.[0-9A-Za-z_\-]{20,})"),
    re.compile(r"(np_[A-Za-z0-9]{20,})"),
    re.compile(r"(nous_[A-Za-z0-9]{12,})"),
    re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?([A-Za-z0-9._\-]{16,})"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*[\"']?)([A-Za-z0-9._\-]{12,})"),
    re.compile(r"(?i)(token\s*[:=]\s*[\"']?)([A-Za-z0-9._\-]{16,})"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
)

REDACTION = "[redacted]"


class Redactor:
    """Removes secret-shaped strings and known secret values from text."""

    def __init__(self) -> None:
        self._values: set[str] = set()
        self._lock = threading.Lock()

    def register(self, value: str | None) -> None:
        if not value or len(value) < 8:
            return
        with self._lock:
            self._values.add(value)

    def forget(self, value: str | None) -> None:
        if not value:
            return
        with self._lock:
            self._values.discard(value)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()

    def redact(self, text: Any) -> str:
        if text is None:
            return ""
        out = str(text)
        with self._lock:
            values = sorted(self._values, key=len, reverse=True)
        for value in values:
            out = out.replace(value, REDACTION)
        for pattern in SECRET_PATTERNS:
            out = pattern.sub(lambda match: (match.group(1) if match.lastindex and match.lastindex >= 1 else "") + REDACTION, out)
        return out


_redactor = Redactor()


def get_redactor() -> Redactor:
    return _redactor


# ---------------------------------------------------------------- rate limiter


class RateLimiter:
    """Small in-process fixed-window limiter.

    Enough for a single-host control plane; documented as such. Behind multiple
    workers each process keeps its own window, which is still a useful brake.
    """

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
        now = time.time()
        with self._lock:
            bucket = [stamp for stamp in self._hits.get(key, []) if now - stamp < window_seconds]
            allowed = len(bucket) < limit
            if allowed:
                bucket.append(now)
            self._hits[key] = bucket
            retry_after = 0
            if not allowed and bucket:
                retry_after = int(window_seconds - (now - bucket[0])) + 1
            return allowed, max(0, retry_after)

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


_rate_limiter = RateLimiter()


def get_rate_limiter() -> RateLimiter:
    return _rate_limiter


# ------------------------------------------------------------------- env keys


def valid_env_key(key: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", key or ""))


def valid_username(username: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9._@-]{3,64}", username or ""))


def mask_secret(value: str, *, keep: int = 4) -> str:
    """``sk-abcdef…1234`` style mask that is safe to render in the UI."""

    if not value:
        return ""
    cleaned = value.strip()
    if len(cleaned) <= keep * 2:
        return "•" * max(4, len(cleaned) // 2)
    return f"{cleaned[:keep]}{'•' * 6}{cleaned[-keep:]}"


def looks_like_secret(value: str) -> bool:
    return any(pattern.search(value or "") for pattern in SECRET_PATTERNS)


def redact_structure(data: Any) -> Any:
    """Deep-redact a JSON-like structure (used before returning diagnostics)."""

    redactor = get_redactor()
    if isinstance(data, dict):
        out = {}
        for key, value in data.items():
            if re.search(r"(?i)(key|token|secret|password|credential)", str(key)) and isinstance(value, str):
                out[key] = mask_secret(value) if value else ""
            else:
                out[key] = redact_structure(value)
        return out
    if isinstance(data, list):
        return [redact_structure(item) for item in data]
    if isinstance(data, str):
        return redactor.redact(data)
    return data


def summarise_env(values: Iterable[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"name": name, "value": mask_secret(value)} for name, value in values]
