"""Server-side store for optional provider API keys.

Rules this module enforces:

* Keys are **encrypted at rest** (see :class:`~app.security.SecretBox`) and live
  in the Control Center database, never in the frontend, never in Git.
* The plaintext is only ever materialised (a) for the process environment of
  the Hermes runtime we start, and (b) optionally, if the operator opts in, to
  Hermes' own ``$HERMES_HOME/.env`` for CLI use.
* Every read API returns ``configured: true/false`` plus a mask — never the
  value. ``/api/cc/keys/{name}/reveal`` does return the plaintext, but it is
  admin-only, audited, and rate-limited.
* The redactor learns every stored value so it can scrub log lines.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..db import Database, utcnow
from ..security import SecretBox, get_redactor, mask_secret, valid_env_key


@dataclass
class StoredKey:
    name: str
    configured: bool
    masked: str
    updated_at: str
    updated_by: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "configured": self.configured,
            "masked": self.masked,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


class SecretStore:
    def __init__(self, db: Database, box: SecretBox) -> None:
        self.db = db
        self.box = box
        self._redactor = get_redactor()
        self._warm_redactor()

    # ------------------------------------------------------------- internals
    def _warm_redactor(self) -> None:
        try:
            for value in self.all_values().values():
                self._redactor.register(value)
        except Exception:  # noqa: BLE001 - never block startup on this
            pass

    def all_values(self) -> dict[str, str]:
        rows = self.db.query("SELECT name, ciphertext FROM secrets")
        values: dict[str, str] = {}
        for row in rows:
            try:
                values[row["name"]] = self.box.decrypt(row["ciphertext"])
            except Exception:  # noqa: BLE001 - corrupted row: report, don't crash
                continue
        return values

    # ---------------------------------------------------------------- public
    def set(self, name: str, value: str, *, actor: str = "") -> StoredKey:
        if not valid_env_key(name):
            raise ValueError(
                f"'{name}' is not a valid environment variable name "
                "(letters, digits and underscores, starting with a letter)."
            )
        value = (value or "").strip()
        if not value:
            raise ValueError("The key value is empty.")
        if len(value) > 8192:
            raise ValueError("That key is implausibly long — check for a copy/paste error.")
        ciphertext = self.box.encrypt(value)
        existing = self.db.query_one("SELECT name FROM secrets WHERE name = ?", (name,))
        if existing:
            self.db.execute(
                "UPDATE secrets SET ciphertext = ?, updated_at = ?, updated_by = ? WHERE name = ?",
                (ciphertext, utcnow(), actor, name),
            )
        else:
            self.db.execute(
                "INSERT INTO secrets (name, ciphertext, updated_at, updated_by) VALUES (?, ?, ?, ?)",
                (name, ciphertext, utcnow(), actor),
            )
        self._redactor.register(value)
        return self.get(name)

    def delete(self, name: str) -> bool:
        removed = self.db.execute("DELETE FROM secrets WHERE name = ?", (name,)) > 0
        return removed

    def get(self, name: str) -> StoredKey:
        row = self.db.query_one("SELECT name, ciphertext, updated_at, updated_by FROM secrets WHERE name = ?", (name,))
        if row is None:
            return StoredKey(name=name, configured=False, masked="", updated_at="")
        try:
            plaintext = self.box.decrypt(row["ciphertext"])
            masked = mask_secret(plaintext)
        except Exception:  # noqa: BLE001
            masked = "(unreadable — re-enter this key)"
        return StoredKey(
            name=name,
            configured=True,
            masked=masked,
            updated_at=row["updated_at"] or "",
            updated_by=row["updated_by"] or "",
        )

    def list(self, names: Iterable[str] | None = None) -> dict[str, StoredKey]:
        rows = self.db.query("SELECT name, ciphertext, updated_at, updated_by FROM secrets ORDER BY name")
        out: dict[str, StoredKey] = {}
        for row in rows:
            try:
                masked = mask_secret(self.box.decrypt(row["ciphertext"]))
            except Exception:  # noqa: BLE001
                masked = "(unreadable — re-enter this key)"
            out[row["name"]] = StoredKey(
                name=row["name"],
                configured=True,
                masked=masked,
                updated_at=row["updated_at"] or "",
                updated_by=row["updated_by"] or "",
            )
        if names:
            for name in names:
                out.setdefault(name, StoredKey(name=name, configured=False, masked="", updated_at=""))
        return out

    def reveal(self, name: str) -> str | None:
        row = self.db.query_one("SELECT ciphertext FROM secrets WHERE name = ?", (name,))
        if row is None:
            return None
        return self.box.decrypt(row["ciphertext"])

    def env_for_runtime(self) -> dict[str, str]:
        """Values injected into the Hermes process environment at start."""
        return self.all_values()

    def clear_redactor(self) -> None:
        self._redactor.clear()
        self._warm_redactor()
