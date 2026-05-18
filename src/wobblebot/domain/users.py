"""Operator-account domain model for Phase 7 web UI auth (ADR-017).

Phase 7's web UI authenticates a single-operator (v1) via
bcrypt-hashed password stored in operator.db's ``users`` table.
This module defines the domain types; persistence lives in
``ports/storage.py`` + ``adapters/sqlite_storage.py``.

**Two types here:**

- :class:`User` — the persisted operator account row.
  ``id`` is ``None`` before insert (SQLite AUTOINCREMENT), populated
  after. Includes the password hash; the plaintext password is NEVER
  stored anywhere — the auth layer (Stage 7.1.C) hashes via
  ``bcrypt`` before any persistence call.

- :class:`UserCredentials` — operator-supplied login form data.
  Plaintext password lives in memory only for the duration of the
  login flow (form POST → bcrypt comparison → discard). Frozen
  Pydantic model so it can't be accidentally mutated mid-flow.

**Per ADR-017 decision 4** password hashing uses the ``bcrypt``
package directly (no ``passlib`` abstraction). The hash string is
the ``$2b$``-prefixed value bcrypt produces; ~60 chars including
the salt + cost factor.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from wobblebot.domain.value_objects import Timestamp


class User(BaseModel):
    """Persisted operator account.

    Attributes:
        id: SQLite-assigned row id. ``None`` before persist;
            populated by ``StoragePort.create_user`` on the returned
            instance.
        username: Operator's chosen username. Unique across the
            table.
        password_hash: Bcrypt-hashed password (``$2b$``-prefixed).
            ~60 chars. NEVER the plaintext.
        created_at: When the row was inserted.
        last_login_at: When the operator last successfully
            authenticated. ``None`` until the first login.
    """

    id: int | None = None
    username: str = Field(min_length=1, max_length=64)
    password_hash: str = Field(min_length=1)
    created_at: Timestamp
    last_login_at: Timestamp | None = None

    class Config:
        frozen = True


class UserCredentials(BaseModel):
    """Operator-supplied login form data.

    Plaintext ``password`` lives in memory only for the duration of
    the login flow — never persisted, never logged. Frozen so the
    auth handler can't accidentally mutate the credentials between
    receipt and bcrypt comparison.

    The Pydantic ``min_length=1`` on both fields is a sanity guard;
    the real validation (existence + password-match) happens in
    ``web/auth.py`` via the storage adapter + bcrypt.
    """

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1)

    class Config:
        frozen = True
