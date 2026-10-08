# Security model

This is a **single-tenant administration tool for a personal agent**. It is built to be safe on a private host or behind
a VPN/tunnel, not to be a public multi-user service. This document states what is enforced, where, and what is a
deployment decision you have to make.

---

## Threat model

| Adversary | In scope? | Mitigation |
|---|---|---|
| Someone on the network reaching the UI | **Yes** | authentication, session cookies, CSRF, rate limits, security headers |
| A malicious web page in your browser (CSRF/clickjacking) | **Yes** | double-submit CSRF token, `SameSite=Lax` cookies, `X-Frame-Options: SAMEORIGIN` |
| Reading keys off disk / out of a backup | **Yes** | encrypted vault, master key separate from the database, no plaintext in exports or logs |
| A key leaking to the browser or a bundle | **Yes** | values are never returned by the API; no secret is compiled into the frontend |
| Another OS user on the same machine | Partly | state directories and log files are `0600`/`0700`; a compromised *admin* account is out of scope |
| A hostile provider or a compromised upstream agent | No | the agent runs with *your* privileges; treat it like any tool you install |
| Full host compromise / root | No | nothing at this layer survives a root attacker |

---

## Authentication

- **Local accounts.** The first administrator is created by the wizard or by `CC_ADMIN_USER`/`CC_ADMIN_PASSWORD` on first
  boot. There is no self-registration (`CC_ALLOW_REGISTRATION` is off).
- **Password hashing.** scrypt with `N=2^15`, `r=8`, `p=1`, 32-byte output and an explicit `maxmem`
  (`128·N·r·4`) so the parameters are honoured instead of silently downgraded. Hashes are never logged.
- **Sessions.** Server-side rows in `auth_sessions`; the cookie holds a random token whose fingerprint (SHA-256) is
  stored. Cookies are `HttpOnly`, `SameSite=Lax`, `Path=/`, and `Secure` automatically when the request is HTTPS
  (`CC_SECURE_COOKIES=auto`; force with `1`). Lifetime `CC_SESSION_TTL` (default 7 days).
- **Revocation.** Sessions can be listed and revoked individually; revoking deletes the row, so the cookie is dead
  immediately. Changing a password revokes the other sessions.
- **API tokens.** Optional bearer tokens for automation (`Authorization: Bearer …`), stored as fingerprints, revocable,
  and not usable for browser sessions.
- **Rate limits** (`app/deps.py`): `login` 10/15 min, `sensitive` 30/5 min (key reveal, key writes), `write` 240/min,
  `read` 1200/min, `chat` 120/min — per client address, with `Retry-After`.

---

## CSRF and request integrity

- Every mutating method (`POST`, `PUT`, `PATCH`, `DELETE`) requires the session's CSRF token in `X-CSRF-Token`
  (`app/deps.py`). The token is bound to the session row's `csrf_fingerprint`, not merely echoed.
- The token is issued at login and rotated with the session; the SPA fetches the current one from
  `GET /api/cc/auth/csrf`.
- `SameSite=Lax` cookies mean cross-site form posts are not sent, and cross-site writes are additionally rejected by
  origin checks (`CC_ALLOWED_ORIGINS`).
- Security headers on every response: `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`,
  `Referrer-Policy: same-origin`, a CSP that keeps `connect-src 'self'`, and `Strict-Transport-Security` when served over
  HTTPS.

---

## Secrets

| Control | Implementation |
|---|---|
| Format | Fernet (AES-128-CBC + HMAC-SHA256) via `cryptography`; one row per key in `secrets.ciphertext` |
| Master key | `CC_HOME/secret.key`, created on first use, mode `0600`, never exported, never in Git |
| Vault unavailable | the app refuses to store keys and says so (`GET /api/cc/keys` reports `vault.available: false`) |
| API exposure | list endpoints return **masked** values only; there is no route that returns a plaintext key to a browser |
| Runtime hand-off | decrypted values are injected into the Hermes **process environment**, or written to `$HERMES_HOME/.env` only if you enable mirroring (warned, audited, mode `0600`, removable) |
| Logging | a redaction filter rewrites every known secret to `••••redacted••••` before it reaches the log store, the log file, the export or the screen |
| Backups/exports | exports contain no readable keys; `include_secrets=true` adds the **encrypted** rows only, and the archive never contains `secret.key` |
| Git | `.gitignore` (inside `control-center/`) excludes `.env`, `*.key`, databases, logs and backups; a test asserts the template contains no key-shaped value |

The design rule: **an archive that contains both the ciphertext and the key is not encryption.** Backups therefore omit
the master key on purpose — copy `secret.key` separately if you want restorable keys.

---

## Agent and runtime isolation

- Hermes runs as a **child process** of the Control Center, in its own process group, with `HERMES_HOME` pinned to your
  configured directory. `CC_RUNTIME_MODE=disabled` stops the app from managing any process at all.
- The runtime's API binds to `127.0.0.1` only, and is reached with the session token upstream publishes in its
  rendezvous record (`host-serve.token`, mode `0600`). The app validates the record's protocol version and fingerprint
  before trusting it, and refuses to adopt anything it cannot verify.
- The chat bridge (`platforms.api_server`) also binds to loopback and requires `API_SERVER_KEY`, which is generated by
  the app and stored in the vault.
- **Allowlisted pass-through.** The UI can only reach the agent's endpoints that `app/routes/hermes_proxy.py` lists
  explicitly. Nothing that can read the agent's files, upload artifacts or execute a shell is exposed; a test asserts the
  table is exactly the documented set.
- The agent itself executes tools (shell, files, network) **with your privileges** — that is its job. Run it as an
  unprivileged user, and treat anything it downloads as untrusted.

---

## Network posture

| Port | What | Binding |
|---|---|---|
| 8080 | Control Center UI + API | `CC_HOST` (default `0.0.0.0`; Docker publishes `127.0.0.1` unless you change `CC_BIND`) |
| 9119 | Hermes runtime API | loopback |
| 9120 | Agent chat bridge | loopback |

- Put TLS in front (reverse proxy or tunnel) for anything beyond loopback, then set `CC_SECURE_COOKIES=1`.
- `CC_TRUSTED_PROXIES` controls which `X-Forwarded-*` headers are believed; `CC_ROOT_PATH` supports sub-path hosting.
- The public endpoints (`/health`, `/status`, `/api/status`) are intentionally minimal: database/runtime/vault/model
  booleans and no paths, keys, usernames or configuration.
- The OpenAPI schema is **not** served publicly (no `/openapi.json`, no `/docs`).

---

## Auditing

`audit_events` records, at minimum: sign-in success/failure, session revocation, password change, token creation and
revocation, key store/replace/remove, FREE MODE and paid-unlock changes, env-mirroring policy changes, backup
create/download, config export/import, setup steps, and runtime/gateway lifecycle events. Events are visible in
**Logs** (categories `SECURITY`, `SYSTEM`, `PROVIDER`) and are subject to the same redaction as everything else.

---

## Operating advice

1. Use a long, unique administrator password; a password manager is fine.
2. Keep the app off the public internet unless it is behind TLS **and** you understand that it is a single-tenant admin
   tool with the agent's privileges.
3. Turn on provider key mirroring only if you actually need the Hermes CLI to see the keys; it is plaintext on disk.
4. Back up `CC_HOME` and keep `secret.key` somewhere safe and separate.
5. Re-run `python -m app.cli doctor` and the Diagnostics page after every update of the app or Hermes.
6. Update dependencies on a schedule — pinned versions are a reproducibility feature, not a security guarantee.

---

## Reporting

If you find a vulnerability, report it privately to the maintainer of your copy (and to Nous Research if the issue is in
Hermes itself: <https://github.com/NousResearch/hermes-agent/security>). Do not open a public issue containing exploit
details or real credentials.
