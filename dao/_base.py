"""Shared types + helpers for the DAO layer.

* ``Actor`` is the resolved identity of the current request — email,
  active tenant, role, operator flag, full membership tuple.  The
  ``current_actor`` middleware in app.py builds it from the
  oauth2-proxy headers; the DAO accepts it on every method.
* ``DAOError`` and friends are the canonical exception types DAO
  methods raise.  Routes catch them and translate to HTTP responses
  (404 / 403 / 409) — the DAO never raises ``HTTPException`` itself
  so it can be reused outside the FastAPI request path (CLI tools,
  background jobs, the eventual stash-recover utility).
* ``require_role`` and ``require_membership`` are the gates every
  mutation method runs at the top.  Spec § "Roles" defines what each
  role can and can't do.
"""

from __future__ import annotations

import dataclasses
import os
import sqlite3
from pathlib import Path


# ── Actor ────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Actor:
    """The resolved identity of the current request.

    `tenant_id` and `role` reflect the *active* membership; a user
    who's a member of multiple tenants picks via the switcher
    (roadmap step 15), but for now we just take the first by
    joined_at.  `memberships` is the full list so the eventual
    switcher UI can render without another DB hit.
    """
    email: str
    tenant_id: int | None
    role: str | None
    is_operator: bool
    memberships: tuple[tuple[int, str], ...]

    def has_membership(self, tenant_id: int) -> str | None:
        """Return the role this actor has on `tenant_id`, or None if
        no membership exists.  Used by the DAO to widen access from
        the active tenant to any tenant the actor is a member of —
        e.g. when serving a file whose row lives in a non-active
        tenant the actor still has rights to (multi-tenant member,
        future object shares)."""
        for tid, role in self.memberships:
            if tid == tenant_id:
                return role
        return None


# ── Errors ──────────────────────────────────────────────────────────


class DAOError(Exception):
    """Base class for DAO errors.  Routes catch this and friends and
    translate to the appropriate HTTP response."""


class NotFoundError(DAOError):
    """The requested row doesn't exist or isn't visible to this actor.

    Routes translate to 404 — never 403, because a 403 would leak
    "the row exists but you can't see it" (data exfiltration via
    response codes)."""


class ForbiddenError(DAOError):
    """The actor exists in the right tenant but lacks the role for
    this operation.  Routes translate to 403."""


class ConflictError(DAOError):
    """Optimistic-concurrency conflict — the caller's expected
    version doesn't match what's in the DB.  Routes translate to
    409, with a body that points the client at refresh-and-retry."""


# ── Role gates ──────────────────────────────────────────────────────


_ROLE_RANKS = {"readonly": 1, "maintainer": 2}


def require_membership(actor: Actor, tenant_id: int) -> str:
    """Assert the actor is a member of `tenant_id` and return their
    role on it.  Raises ForbiddenError otherwise.

    Use this for explicit cross-tenant operations (an object share
    accessed from outside the active tenant).  For
    "actor must be operating on their own active tenant", just
    compare `actor.tenant_id` to the row's `tenant_id` and 404 on
    mismatch — that's tenancy isolation, not a permissions check."""
    role = actor.has_membership(tenant_id)
    if role is None:
        raise ForbiddenError(
            f"{actor.email} is not a member of tenant {tenant_id}"
        )
    return role


def require_role(actor: Actor, minimum: str) -> None:
    """Assert the actor's *active* role meets at least `minimum`.

    `minimum` is "readonly" or "maintainer".  An operator without
    membership has no role — they get ForbiddenError, by design,
    since the operator path doesn't grant data access (see spec §
    "Operator surface").
    """
    needed = _ROLE_RANKS.get(minimum)
    if needed is None:
        raise ValueError(f"unknown minimum role {minimum!r}")
    have = _ROLE_RANKS.get(actor.role) if actor.role else 0
    if have < needed:
        raise ForbiddenError(
            f"{actor.email} has role {actor.role!r}; needs {minimum!r}"
        )


# ── Connection ──────────────────────────────────────────────────────


# Late-bound DB path — read at call time so test fixtures that
# monkeypatch STASH_DB before importing app pick up the right value.
def _db_path() -> Path:
    return Path(os.environ.get("STASH_DB", Path(__file__).resolve().parent.parent / "stash.db"))


def db() -> sqlite3.Connection:
    """A fresh connection with the standard pragmas applied.  DAO
    methods open and close their own connections — request handlers
    don't pass connections in, so the layering stays clean and a
    forgotten ``conn`` parameter at the route layer can't accidentally
    bypass the DAO."""
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn
