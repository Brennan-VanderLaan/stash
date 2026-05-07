"""Encryption-at-rest primitives.

Implements the design in spec.md § "Encryption at rest":

* Each tenant gets a 256-bit Data Encryption Key (DEK), generated
  server-side at first use and stored as a wrapped blob on the
  ``tenants`` row.
* The DEK is wrapped (encrypted) with a Key Encryption Key (KEK)
  loaded from ``STASH_KEK`` — a base64-encoded 32-byte secret that
  lives outside the DB and the uploads directory (separate B2
  bucket, ideally separate vendor; see spec § "Off-site DR").
* Photos and thumbnails are encrypted with AES-256-GCM using a
  random 12-byte IV per file.  On disk the blob is laid out as
  ``<iv><ciphertext><auth_tag>`` — the same shape AESGCM.encrypt
  returns natively, with the IV prepended.
* The DEK is cached in process memory after first unwrap so request
  paths don't re-decrypt the wrapped blob on every photo serve.

Threat model (recap from the spec): stop a snooping operator, a
stolen disk, an errant log-tail.  Allow audited recovery via the
``stash-recover`` CLI when a user files a ticket.  Not aimed at
nation-state-resistant trustlessness — that's a different product.
"""

from __future__ import annotations

import base64
import os
import secrets
import threading

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


# Marker prepended to every encrypted-on-disk blob.  Lets the
# migration distinguish already-encrypted files from pre-encryption
# cleartext (a pre-migration legacy file would not start with these
# bytes).  Four bytes so the per-blob overhead stays minimal: marker
# (4) + iv (12) + ciphertext + tag (16) = 32 bytes overhead per file.
ENCRYPTED_MARKER = b"S1V\x01"
_IV_LEN = 12
_TAG_LEN = 16


# Process-wide DEK cache. Keyed by tenant_id; populated lazily on
# first use, never invalidated within process lifetime (a deleted
# tenant has no live requests, and rotation is future work).
_DEK_CACHE: dict[int, bytes] = {}
_DEK_CACHE_LOCK = threading.Lock()


# ── KEK loading ─────────────────────────────────────────────────────


def get_kek() -> bytes:
    """Load the Key Encryption Key from ``STASH_KEK``.

    The KEK is a base64-encoded 32-byte (256-bit) secret.  Generate
    one with::

        python -c "import base64, secrets; \\
            print(base64.b64encode(secrets.token_bytes(32)).decode())"

    Raises ``RuntimeError`` if missing or malformed — the app refuses
    to boot without it, by design.  Losing this value is total data
    loss; back it up to a different bucket (and ideally a different
    vendor) than the data."""
    raw = os.environ.get("STASH_KEK", "").strip()
    if not raw:
        raise RuntimeError(
            "STASH_KEK is not set. Generate one with:\n"
            "  python -c \"import base64, secrets; "
            "print(base64.b64encode(secrets.token_bytes(32)).decode())\"\n"
            "Then set it in your environment.  The KEK wraps every tenant's "
            "DEK; without it, on-disk photos cannot be decrypted."
        )
    try:
        kek = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise RuntimeError(f"STASH_KEK is not valid base64: {exc}")
    if len(kek) != 32:
        raise RuntimeError(
            f"STASH_KEK must decode to 32 bytes (256 bits); got {len(kek)}"
        )
    return kek


# ── Blob encryption / decryption ────────────────────────────────────


def encrypt_blob(key: bytes, plaintext: bytes) -> bytes:
    """AES-256-GCM encrypt with a fresh random IV.  Returns
    ``marker || iv || ciphertext || tag`` — the marker lets the
    migration distinguish encrypted from pre-encryption cleartext,
    and the rest is what AESGCM.encrypt produces natively (IV
    prepended)."""
    iv = secrets.token_bytes(_IV_LEN)
    ct = AESGCM(key).encrypt(iv, plaintext, associated_data=None)
    return ENCRYPTED_MARKER + iv + ct


def decrypt_blob(key: bytes, blob: bytes) -> bytes:
    """Decrypt a blob produced by ``encrypt_blob``.  Raises if the
    marker is missing, the blob is too short, or the auth tag fails
    (data was tampered with or the wrong key was used)."""
    if not blob.startswith(ENCRYPTED_MARKER):
        raise ValueError("blob is missing the encryption marker")
    body = blob[len(ENCRYPTED_MARKER):]
    if len(body) < _IV_LEN + _TAG_LEN:
        raise ValueError("blob too short to be valid AES-GCM output")
    iv = body[:_IV_LEN]
    ct = body[_IV_LEN:]
    return AESGCM(key).decrypt(iv, ct, associated_data=None)


def looks_encrypted(blob: bytes) -> bool:
    """Cheap header-only check the migration uses to skip files that
    have already been migrated."""
    return blob.startswith(ENCRYPTED_MARKER)


# ── Per-tenant DEK lifecycle ────────────────────────────────────────


def generate_dek() -> bytes:
    """A fresh 256-bit DEK.  Called once per tenant; thereafter the
    wrapped form lives on tenants.wrapped_dek."""
    return AESGCM.generate_key(bit_length=256)


def wrap_dek(kek: bytes, dek: bytes) -> bytes:
    """Wrap a DEK with the KEK for at-rest storage."""
    return encrypt_blob(kek, dek)


def unwrap_dek(kek: bytes, wrapped: bytes) -> bytes:
    """Unwrap a stored DEK with the KEK."""
    return decrypt_blob(kek, wrapped)


def get_dek(conn, tenant_id: int, kek: bytes) -> bytes:
    """Return the unwrapped DEK for a tenant, generating + persisting
    it on first call.  Cached in process memory after first unwrap.

    Concurrency: uses ``BEGIN IMMEDIATE`` to serialise the
    generate-and-write path so two requests can't race and store
    different DEKs (which would render half the tenant's photos
    unrecoverable).  WAL + busy_timeout on the connection (set in
    ``app.db()``) keep readers unblocked."""
    with _DEK_CACHE_LOCK:
        cached = _DEK_CACHE.get(tenant_id)
    if cached is not None:
        return cached

    row = conn.execute(
        "SELECT wrapped_dek FROM tenants WHERE id = ?", (tenant_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown tenant_id {tenant_id}")
    wrapped = row["wrapped_dek"]
    if wrapped is not None:
        dek = unwrap_dek(kek, wrapped)
        with _DEK_CACHE_LOCK:
            _DEK_CACHE[tenant_id] = dek
        return dek

    # First call for this tenant — generate, wrap, persist.
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-read inside the immediate-transaction in case another
        # connection wrote the DEK while we were waiting.
        row = conn.execute(
            "SELECT wrapped_dek FROM tenants WHERE id = ?", (tenant_id,),
        ).fetchone()
        if row["wrapped_dek"] is not None:
            conn.execute("ROLLBACK")
            dek = unwrap_dek(kek, row["wrapped_dek"])
        else:
            dek = generate_dek()
            wrapped = wrap_dek(kek, dek)
            conn.execute(
                "UPDATE tenants SET wrapped_dek = ? WHERE id = ?",
                (wrapped, tenant_id),
            )
            conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    with _DEK_CACHE_LOCK:
        _DEK_CACHE[tenant_id] = dek
    return dek


def encrypt_for_tenant(conn, tenant_id: int, kek: bytes, plaintext: bytes) -> bytes:
    """Convenience: encrypt plaintext using the tenant's DEK."""
    return encrypt_blob(get_dek(conn, tenant_id, kek), plaintext)


def decrypt_for_tenant(conn, tenant_id: int, kek: bytes, blob: bytes) -> bytes:
    """Convenience: decrypt a blob using the tenant's DEK."""
    return decrypt_blob(get_dek(conn, tenant_id, kek), blob)


def clear_dek_cache() -> None:
    """Reset the in-process DEK cache.  Used by tests that reload the
    app module so a stale DEK from a previous test fixture doesn't
    bleed into the next one."""
    with _DEK_CACHE_LOCK:
        _DEK_CACHE.clear()
