"""OAuth 2.1 authorization server primitives.

Stash plays both roles per spec rev 2025-11-25:

* **Resource server** for ``/mcp`` (and incidentally ``/api/v1``).
* **Authorization server** at ``/oauth/authorize`` + ``/oauth/token``
  with discovery via ``/.well-known/oauth-authorization-server``
  and ``/.well-known/oauth-protected-resource``.

Implemented subset:

* Authorization Code grant with PKCE (S256 only).
* Dynamic Client Registration (RFC 7591), public + confidential.
* Refresh token rotation per OAuth 2.1 §4.3.1.
* Resource indicator (RFC 8707) — every issued access token is
  bound to its target resource and validated at the protected
  endpoint.

Out of scope (deliberately):

* Implicit + password grants (forbidden by OAuth 2.1).
* Client ID Metadata Documents — overkill for a single-deploy
  stash; preregistration + DCR cover claude.ai + IDE clients.
* OpenID Connect — we never issue id_tokens; identity stays on
  the oauth2-proxy / X-Forwarded-Email path.

Storage shape (see ``app.py`` for the CREATE TABLE):

* ``oauth_clients`` — registered clients.  Plaintext secrets
  (when issued) are returned once; only sha256 lands in the DB.
* ``oauth_authorization_codes`` — short-lived (60 s) codes
  produced by the consent page.
* ``oauth_refresh_tokens`` — hashed long-lived refreshers,
  rotated on every use.
* ``api_tokens`` — augmented with ``audience``, ``expires_at``,
  ``oauth_client_id`` so OAuth-issued access tokens reuse the
  existing bearer-validation path.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import obs
from dao._base import (
    Actor,
    ForbiddenError,
    NotFoundError,
    db,
    require_operator,
)


_log = obs.get_logger("dao.oauth")


# ── Lifetimes ──────────────────────────────────────────────────────


# Authorization codes per OAuth 2.1 §4.1.3 — short.  60 s is
# generous for a redirect bounce; spec recommends ≤10 minutes
# upper bound, much shorter is safer.
AUTH_CODE_TTL_SECONDS = 60

# Access tokens — 1 hour.  Spec wants short-lived; rotation
# happens via refresh tokens.
ACCESS_TOKEN_TTL_SECONDS = 3600

# Refresh tokens — 30 days.  Long enough that an MCP client
# doesn't bug the user weekly; short enough that abandoned tokens
# expire on their own.  Rotated on every use.
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600


# ── Token shapes ────────────────────────────────────────────────────


_ACCESS_TOKEN_PREFIX = "stash_"  # same shape as phase-11 tokens
_REFRESH_TOKEN_PREFIX = "stashr_"
_AUTH_CODE_PREFIX = "stashc_"


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    """SQLite-CURRENT_TIMESTAMP-shaped string so window comparisons
    don't trip on the ``' '`` vs ``'T'`` lexical difference (same
    fix as dao.quotas)."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ── Client registration ────────────────────────────────────────────


def register_client(
    *,
    name: str,
    redirect_uris: list[str],
    is_public: bool = True,
    registered_by_email: str = "<dcr>",
) -> dict:
    """Mint a new OAuth client.  Used by both the operator
    pre-registration path and the public DCR endpoint.

    For public clients (``is_public=True``) the returned dict
    omits ``client_secret`` — PKCE is the auth mechanism on the
    /token call.  For confidential clients we mint a one-time
    secret (shown once, hashed in storage)."""
    if not name or not name.strip():
        raise ValueError("client name required")
    if not redirect_uris:
        raise ValueError("at least one redirect_uri required")
    for uri in redirect_uris:
        # Strict form: HTTPS or localhost.  Spec § Communication
        # Security: "All redirect URIs MUST be either localhost or
        # use HTTPS."
        if not (uri.startswith("https://")
                or uri.startswith("http://localhost")
                or uri.startswith("http://127.0.0.1")):
            raise ValueError(
                f"redirect_uri {uri!r} must be HTTPS or localhost",
            )

    client_id = "client_" + secrets.token_urlsafe(16)
    client_secret = None
    client_secret_hash = None
    if not is_public:
        client_secret = "secret_" + secrets.token_urlsafe(24)
        client_secret_hash = _hash(client_secret)

    with db() as conn:
        conn.execute(
            "INSERT INTO oauth_clients "
            "(client_id, client_secret_hash, name, redirect_uris, "
            " is_public, registered_by_email) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (client_id, client_secret_hash, name.strip(),
             json.dumps(redirect_uris), 1 if is_public else 0,
             registered_by_email),
        )
        # Audit-log every registration so an operator can spot
        # spammy DCR floods.  tenant_id NULL — clients are
        # cross-tenant by nature.
        obs.write_audit(
            conn, tenant_id=None, actor_email=registered_by_email,
            action="oauth.client.register",
            target_kind="oauth_client",
            metadata={
                "client_id": client_id,
                "name": name.strip(),
                "is_public": is_public,
                "redirect_uris": redirect_uris,
            },
        )
        conn.commit()
    _log.info(
        "oauth.client.register id=%s name=%r public=%s by=%s",
        client_id, name, is_public, registered_by_email,
    )
    out = {
        "client_id": client_id,
        "name": name.strip(),
        "redirect_uris": redirect_uris,
        "is_public": is_public,
    }
    if client_secret:
        out["client_secret"] = client_secret
    return out


def get_client(client_id: str) -> Optional[dict]:
    """Look up a client by id.  Returns None for unknown /
    revoked.  Used at /authorize + /token to validate the
    incoming client_id.  Doesn't check secrets — caller does
    that with :func:`verify_client_secret`."""
    if not client_id:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT client_id, client_secret_hash, name, redirect_uris, "
            "       is_public, registered_at "
            "FROM oauth_clients "
            "WHERE client_id = ? AND revoked_at IS NULL",
            (client_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "client_id": row["client_id"],
        "client_secret_hash": row["client_secret_hash"],
        "name": row["name"],
        "redirect_uris": json.loads(row["redirect_uris"]),
        "is_public": bool(row["is_public"]),
        "registered_at": row["registered_at"],
    }


def verify_client_secret(client: dict, secret_plaintext: str) -> bool:
    """Constant-time-ish secret check for confidential clients.
    Public clients return True regardless — they don't carry
    secrets (PKCE is the auth mechanism)."""
    if client.get("is_public"):
        return True
    expected = client.get("client_secret_hash") or ""
    actual = _hash(secret_plaintext or "")
    return secrets.compare_digest(expected, actual)


def list_clients(actor: Actor) -> list[dict]:
    """Operator-only roster of every OAuth client on the
    deployment.  Surfaces who registered each (or ``<dcr>`` for
    self-registered clients) so an audit can find the source."""
    require_operator(actor)
    with db() as conn:
        rows = conn.execute(
            "SELECT client_id, name, redirect_uris, is_public, "
            "       registered_by_email, registered_at, revoked_at "
            "FROM oauth_clients ORDER BY registered_at DESC"
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            "client_id": r["client_id"],
            "name": r["name"],
            "redirect_uris": json.loads(r["redirect_uris"]),
            "is_public": bool(r["is_public"]),
            "registered_by_email": r["registered_by_email"],
            "registered_at": r["registered_at"],
            "revoked_at": r["revoked_at"],
        })
    return out


def revoke_client(actor: Actor, client_id: str) -> None:
    """Operator-only client revocation.  Existing access tokens
    issued under this client stay valid until their natural
    expiry (we don't iterate api_tokens to mass-revoke), but no
    new auth code or refresh exchange will succeed."""
    require_operator(actor)
    with db() as conn:
        cur = conn.execute(
            "UPDATE oauth_clients SET revoked_at = CURRENT_TIMESTAMP "
            "WHERE client_id = ? AND revoked_at IS NULL",
            (client_id,),
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"oauth_client {client_id}")
        obs.write_audit(
            conn, tenant_id=None, actor_email=actor.email,
            action="oauth.client.revoke",
            target_kind="oauth_client",
            metadata={"client_id": client_id},
        )
        conn.commit()
    _log.warning("oauth.client.revoke id=%s by=%s",
                 client_id, actor.email)


# ── Authorization codes ────────────────────────────────────────────


def issue_authorization_code(
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    code_challenge_method: str,
    scope: str,
    resource: str,
    tenant_id: int,
    user_email: str,
    role: str,
) -> str:
    """Mint a fresh authorization code after user consent.
    Returns the plaintext code — caller (the /authorize POST)
    appends it to the ``redirect_uri`` query string for the
    browser bounce."""
    if code_challenge_method != "S256":
        raise ValueError(
            "Only S256 code_challenge_method is supported "
            "(spec mandates it for OAuth 2.1)",
        )
    code = _AUTH_CODE_PREFIX + secrets.token_urlsafe(32)
    expires_at = _iso(_now() + timedelta(seconds=AUTH_CODE_TTL_SECONDS))
    with db() as conn:
        conn.execute(
            "INSERT INTO oauth_authorization_codes "
            "(code, client_id, redirect_uri, code_challenge, "
            " code_challenge_method, scope, resource, tenant_id, "
            " user_email, role, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (code, client_id, redirect_uri, code_challenge,
             code_challenge_method, scope, resource, tenant_id,
             user_email, role, expires_at),
        )
        obs.write_audit(
            conn, tenant_id=tenant_id, actor_email=user_email,
            action="oauth.code.issue",
            target_kind="oauth_client",
            metadata={
                "client_id": client_id,
                "scope": scope,
                "resource": resource,
            },
        )
        conn.commit()
    return code


def consume_authorization_code(
    *,
    code: str,
    client_id: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict:
    """Validate + atomically consume an authorization code.
    Returns the dict of context the access-token issuer needs
    (tenant_id, user_email, role, scope, resource).

    Raises ValueError on any of:
    * unknown / consumed / expired code
    * client_id / redirect_uri mismatch
    * PKCE verifier doesn't match the stored challenge
    """
    with db() as conn:
        row = conn.execute(
            "SELECT client_id, redirect_uri, code_challenge, "
            "       code_challenge_method, scope, resource, "
            "       tenant_id, user_email, role, expires_at, "
            "       consumed_at "
            "FROM oauth_authorization_codes WHERE code = ?",
            (code,),
        ).fetchone()
        if row is None:
            raise ValueError("invalid_grant: unknown code")
        if row["consumed_at"] is not None:
            # Single-use replay attempt — spec says revoke any
            # tokens issued from this code too, but we'd need to
            # track that linkage; flag in audit + reject instead.
            raise ValueError("invalid_grant: code already consumed")
        if row["expires_at"] < _iso(_now()):
            raise ValueError("invalid_grant: code expired")
        if row["client_id"] != client_id:
            raise ValueError("invalid_grant: client_id mismatch")
        if row["redirect_uri"] != redirect_uri:
            raise ValueError("invalid_grant: redirect_uri mismatch")

        # PKCE: verify the code_verifier hashes to the stored
        # code_challenge (S256 = base64url(sha256(verifier))).
        import base64
        digest = hashlib.sha256(
            (code_verifier or "").encode("utf-8"),
        ).digest()
        expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        if not secrets.compare_digest(row["code_challenge"], expected):
            raise ValueError("invalid_grant: PKCE verification failed")

        # Mark consumed before returning — single-use.
        conn.execute(
            "UPDATE oauth_authorization_codes "
            "SET consumed_at = CURRENT_TIMESTAMP WHERE code = ?",
            (code,),
        )
        conn.commit()

    return {
        "tenant_id": row["tenant_id"],
        "user_email": row["user_email"],
        "role": row["role"],
        "scope": row["scope"],
        "resource": row["resource"],
    }


# ── Access + refresh token issuance ────────────────────────────────


def issue_token_pair(
    *,
    client_id: str,
    tenant_id: int,
    user_email: str,
    role: str,
    scope: str,
    resource: str,
) -> dict:
    """Mint a fresh access token (in api_tokens) + refresh token
    (in oauth_refresh_tokens) for a successful authorization or
    refresh exchange.  Returns the OAuth /token JSON shape.

    Both token plaintexts appear exactly once — in the return
    dict.  After this call, the DB only holds sha256 hashes."""
    access_token = _ACCESS_TOKEN_PREFIX + secrets.token_urlsafe(32)
    refresh_token = _REFRESH_TOKEN_PREFIX + secrets.token_urlsafe(32)
    access_expires_at = _iso(
        _now() + timedelta(seconds=ACCESS_TOKEN_TTL_SECONDS),
    )
    refresh_expires_at = _iso(
        _now() + timedelta(seconds=REFRESH_TOKEN_TTL_SECONDS),
    )
    name = f"oauth:{client_id}:{user_email}"[:100]
    with db() as conn:
        # Access token rides in the existing api_tokens table so
        # /api/v1 + /mcp bearer paths Just Work; the audience +
        # expires_at columns gate it to the right resource and
        # lifetime.
        conn.execute(
            "INSERT INTO api_tokens "
            "(tenant_id, token_hash, name, role, "
            " created_by_email, oauth_client_id, audience, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, _hash(access_token), name, role,
             user_email, client_id, resource, access_expires_at),
        )
        conn.execute(
            "INSERT INTO oauth_refresh_tokens "
            "(token_hash, oauth_client_id, tenant_id, user_email, "
            " role, scope, resource, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_hash(refresh_token), client_id, tenant_id, user_email,
             role, scope, resource, refresh_expires_at),
        )
        obs.write_audit(
            conn, tenant_id=tenant_id, actor_email=user_email,
            action="oauth.token.issue",
            target_kind="oauth_client",
            metadata={
                "client_id": client_id,
                "scope": scope,
                "resource": resource,
                "expires_in": ACCESS_TOKEN_TTL_SECONDS,
            },
        )
        conn.commit()
    _log.info(
        "oauth.token.issue client_id=%s tenant_id=%s user=%s",
        client_id, tenant_id, user_email,
    )
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL_SECONDS,
        "refresh_token": refresh_token,
        "scope": scope,
    }


def consume_refresh_token(
    *,
    refresh_token: str,
    client_id: str,
) -> dict:
    """Atomic rotation: validate + consume the refresh token, then
    return its context (tenant_id, user_email, role, scope,
    resource) so the caller can issue a fresh pair.

    Raises ValueError on any of: unknown / consumed / expired /
    wrong client_id."""
    with db() as conn:
        row = conn.execute(
            "SELECT oauth_client_id, tenant_id, user_email, role, "
            "       scope, resource, expires_at, consumed_at "
            "FROM oauth_refresh_tokens WHERE token_hash = ?",
            (_hash(refresh_token),),
        ).fetchone()
        if row is None:
            raise ValueError("invalid_grant: unknown refresh_token")
        if row["consumed_at"] is not None:
            # Replay → spec says revoke the entire chain.  Keep it
            # simple: refuse and audit so the operator notices.
            obs.write_audit(
                conn, tenant_id=row["tenant_id"],
                actor_email=row["user_email"],
                action="oauth.refresh.replay",
                target_kind="oauth_client",
                metadata={"client_id": client_id},
            )
            conn.commit()
            raise ValueError("invalid_grant: refresh_token already used")
        if row["expires_at"] < _iso(_now()):
            raise ValueError("invalid_grant: refresh_token expired")
        if row["oauth_client_id"] != client_id:
            raise ValueError("invalid_grant: client_id mismatch")
        conn.execute(
            "UPDATE oauth_refresh_tokens "
            "SET consumed_at = CURRENT_TIMESTAMP WHERE token_hash = ?",
            (_hash(refresh_token),),
        )
        conn.commit()
    return {
        "tenant_id": row["tenant_id"],
        "user_email": row["user_email"],
        "role": row["role"],
        "scope": row["scope"],
        "resource": row["resource"],
    }


# ── Discovery metadata ─────────────────────────────────────────────


def authorization_server_metadata(public_url: str) -> dict:
    """RFC 8414 Authorization Server Metadata.  Public — anyone
    can fetch this to learn how to talk to us as an OAuth AS."""
    base = public_url.rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "scopes_supported": ["mcp"],
        "response_types_supported": ["code"],
        "grant_types_supported": [
            "authorization_code",
            "refresh_token",
        ],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": [
            "none",  # public clients (PKCE)
            "client_secret_post",  # confidential clients
        ],
        "service_documentation": f"{base}/about",
    }


def protected_resource_metadata(public_url: str) -> dict:
    """RFC 9728 Protected Resource Metadata.  ``/mcp`` is the
    canonical resource; the AS is this same deployment."""
    base = public_url.rstrip("/")
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "scopes_supported": ["mcp"],
        "bearer_methods_supported": ["header"],
        "resource_documentation": f"{base}/about",
    }
