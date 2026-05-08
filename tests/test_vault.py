"""Encryption-at-rest primitives.  These tests don't go through
FastAPI — they hit vault.py directly so the crypto layer can be
verified independently of the file-path integration that lands in
the next phase commit."""

import base64
import os
import secrets
import sqlite3

import pytest

import vault


# ── KEK loading ─────────────────────────────────────────────────────


def test_get_kek_loads_valid_base64(monkeypatch):
    raw = secrets.token_bytes(32)
    monkeypatch.setenv("STASH_KEK", base64.b64encode(raw).decode())
    assert vault.get_kek() == raw


def test_get_kek_refuses_when_unset(monkeypatch):
    monkeypatch.delenv("STASH_KEK", raising=False)
    with pytest.raises(RuntimeError, match="STASH_KEK is not set"):
        vault.get_kek()


def test_get_kek_refuses_invalid_base64(monkeypatch):
    monkeypatch.setenv("STASH_KEK", "not!valid!base64!!")
    with pytest.raises(RuntimeError, match="not valid base64"):
        vault.get_kek()


def test_get_kek_refuses_wrong_length(monkeypatch):
    short = base64.b64encode(secrets.token_bytes(16)).decode()
    monkeypatch.setenv("STASH_KEK", short)
    with pytest.raises(RuntimeError, match="must decode to 32 bytes"):
        vault.get_kek()


# ── Blob encrypt / decrypt round-trip ───────────────────────────────


def test_encrypt_then_decrypt_returns_plaintext():
    key = secrets.token_bytes(32)
    plaintext = b"the cat is on the mat" * 100
    blob = vault.encrypt_blob(key, plaintext)
    assert vault.decrypt_blob(key, blob) == plaintext


def test_encrypt_produces_marker_iv_ciphertext_tag_layout():
    key = secrets.token_bytes(32)
    plaintext = b"hello"
    blob = vault.encrypt_blob(key, plaintext)
    # marker (4) + iv (12) + ciphertext (5) + tag (16) = 37 bytes
    assert blob.startswith(vault.ENCRYPTED_MARKER)
    assert len(blob) == 4 + 12 + len(plaintext) + 16


def test_two_encrypts_of_same_plaintext_differ():
    """Random IV per call → identical input never produces identical
    output."""
    key = secrets.token_bytes(32)
    a = vault.encrypt_blob(key, b"x")
    b = vault.encrypt_blob(key, b"x")
    assert a != b


def test_decrypt_rejects_wrong_key():
    key = secrets.token_bytes(32)
    blob = vault.encrypt_blob(key, b"secret")
    other = secrets.token_bytes(32)
    with pytest.raises(Exception):  # InvalidTag from the cryptography lib
        vault.decrypt_blob(other, blob)


def test_decrypt_rejects_blob_without_marker():
    key = secrets.token_bytes(32)
    fake = b"\x00\x00\x00\x00" + b"x" * 50
    with pytest.raises(ValueError, match="missing the encryption marker"):
        vault.decrypt_blob(key, fake)


def test_decrypt_rejects_short_blob():
    key = secrets.token_bytes(32)
    too_short = vault.ENCRYPTED_MARKER + b"x" * 5
    with pytest.raises(ValueError, match="too short"):
        vault.decrypt_blob(key, too_short)


def test_decrypt_rejects_tampered_ciphertext():
    """AES-GCM auth tag catches any modification."""
    key = secrets.token_bytes(32)
    blob = vault.encrypt_blob(key, b"do not change this")
    tampered = bytearray(blob)
    tampered[20] ^= 0x01  # flip a bit somewhere inside the ciphertext
    with pytest.raises(Exception):
        vault.decrypt_blob(key, bytes(tampered))


def test_looks_encrypted_returns_true_for_real_blob():
    key = secrets.token_bytes(32)
    assert vault.looks_encrypted(vault.encrypt_blob(key, b"x"))


def test_looks_encrypted_returns_false_for_plaintext():
    """Used by the migration to skip already-encrypted files."""
    assert not vault.looks_encrypted(b"\xff\xd8\xff\xe0...JFIF...")
    assert not vault.looks_encrypted(b"")


# ── DEK lifecycle ──────────────────────────────────────────────────


def _setup_test_db(tmp_path):
    """Minimal sqlite DB with just the tenants table, for isolated
    DEK tests."""
    path = tmp_path / "vault_test.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE tenants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            wrapped_dek BLOB
        );
    """)
    cur = conn.execute("INSERT INTO tenants (name) VALUES ('Test')")
    tenant_id = cur.lastrowid
    conn.commit()
    return conn, tenant_id


def test_get_dek_creates_on_first_call(tmp_path):
    conn, tenant_id = _setup_test_db(tmp_path)
    kek = secrets.token_bytes(32)
    vault.clear_dek_cache()

    dek = vault.get_dek(conn, tenant_id, kek)
    assert isinstance(dek, bytes)
    assert len(dek) == 32

    # Persisted as wrapped blob on the row.
    row = conn.execute(
        "SELECT wrapped_dek FROM tenants WHERE id = ?", (tenant_id,)
    ).fetchone()
    assert row["wrapped_dek"] is not None
    assert vault.unwrap_dek(kek, row["wrapped_dek"]) == dek


def test_get_dek_returns_same_value_on_subsequent_calls(tmp_path):
    """Caching: the same tenant + same KEK always yields the same
    DEK once it's been generated."""
    conn, tenant_id = _setup_test_db(tmp_path)
    kek = secrets.token_bytes(32)
    vault.clear_dek_cache()

    first = vault.get_dek(conn, tenant_id, kek)
    second = vault.get_dek(conn, tenant_id, kek)
    assert first == second


def test_get_dek_unwraps_after_cache_clear(tmp_path):
    """Process restart simulation — clearing the cache should pull
    the same DEK back out of storage via the KEK."""
    conn, tenant_id = _setup_test_db(tmp_path)
    kek = secrets.token_bytes(32)
    vault.clear_dek_cache()

    original = vault.get_dek(conn, tenant_id, kek)
    vault.clear_dek_cache()
    after_restart = vault.get_dek(conn, tenant_id, kek)
    assert original == after_restart


def test_get_dek_rejects_unknown_tenant(tmp_path):
    conn, _ = _setup_test_db(tmp_path)
    kek = secrets.token_bytes(32)
    vault.clear_dek_cache()
    with pytest.raises(ValueError, match="unknown tenant_id"):
        vault.get_dek(conn, 999, kek)


def test_encrypt_for_tenant_round_trip(tmp_path):
    conn, tenant_id = _setup_test_db(tmp_path)
    kek = secrets.token_bytes(32)
    vault.clear_dek_cache()

    plaintext = b"a photograph, encrypted for one tenant only"
    blob = vault.encrypt_for_tenant(conn, tenant_id, kek, plaintext)
    assert vault.decrypt_for_tenant(conn, tenant_id, kek, blob) == plaintext


def test_encrypt_for_tenant_separate_tenants_cannot_decrypt_each_other(tmp_path):
    """Per-tenant DEKs mean tenant A's encrypted file can't be read
    even if you point the decrypt at tenant B."""
    conn, tenant_a = _setup_test_db(tmp_path)
    cur = conn.execute("INSERT INTO tenants (name) VALUES ('Other')")
    tenant_b = cur.lastrowid
    conn.commit()
    kek = secrets.token_bytes(32)
    vault.clear_dek_cache()

    blob = vault.encrypt_for_tenant(conn, tenant_a, kek, b"private to A")
    with pytest.raises(Exception):
        vault.decrypt_for_tenant(conn, tenant_b, kek, blob)


# ── End-to-end: photos on disk are ciphertext ───────────────────────


def test_uploaded_photos_are_ciphertext_on_disk(client):
    """The ethos says casual disk access can't read user photos.  Verify
    that any photo written through the standard upload path lands as a
    blob carrying the ENCRYPTED_MARKER prefix, not raw JPEG bytes."""
    import io
    from PIL import Image as _Image
    from pathlib import Path

    client.post("/boxes", data={"name": "Box"})
    real_jpg = io.BytesIO()
    _Image.new("RGB", (200, 200), color=(80, 60, 200)).save(real_jpg, format="JPEG")
    client.post(
        "/boxes/1/items",
        data={"name": "thing"},
        files={"photo": ("p.jpg", io.BytesIO(real_jpg.getvalue()), "image/jpeg")},
    )

    upload_dir = Path(client.app_module.UPLOAD_DIR) / str(client.test_tenant_id)
    files = list(upload_dir.glob("*.jpg"))
    assert files, "no upload landed on disk"
    for f in files:
        body = f.read_bytes()
        assert body.startswith(client.app_module.vault.ENCRYPTED_MARKER), \
            f"{f.name} stored cleartext — encryption-at-rest broken"
        assert b"\xff\xd8\xff" not in body[:10], \
            f"{f.name} starts with JPEG magic — encryption broke down"


def test_thumbs_are_also_ciphertext_on_disk(client):
    """Thumbnails carry the same risk as sources — verify they're
    encrypted too."""
    import io
    from PIL import Image as _Image
    from pathlib import Path

    client.post("/boxes", data={"name": "Box"})
    real_jpg = io.BytesIO()
    _Image.new("RGB", (1600, 1200), color=(80, 60, 200)).save(real_jpg, format="JPEG")
    client.post(
        "/boxes/1/items",
        data={"name": "thing"},
        files={"photo": ("p.jpg", io.BytesIO(real_jpg.getvalue()), "image/jpeg")},
    )

    upload_dir = Path(client.app_module.UPLOAD_DIR) / str(client.test_tenant_id)
    thumbs = list(upload_dir.glob("*_thumb.jpg"))
    assert thumbs, "no thumb pre-generated"
    for t in thumbs:
        body = t.read_bytes()
        assert body.startswith(client.app_module.vault.ENCRYPTED_MARKER), \
            f"thumb {t.name} stored cleartext"
