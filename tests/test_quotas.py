"""Phase 10 — per-tenant quota enforcement.

Tests verify three layers:

1. **Cap resolution** — plan defaults + per-tenant overrides merge
   the way ``dao.quotas.get_caps`` claims.
2. **Soft warning** — at 80–99% the response carries
   ``X-Quota-Warning``.
3. **Hard 429** — at ≥ 100% the AI / upload routes refuse the
   request before doing any expensive work.
"""

from __future__ import annotations


def _seed_usage(client, *, surface: str, kind: str,
                units: int, cost_micros: int = 0,
                count: int = 1) -> None:
    """Insert ``count`` synthetic usage_events rows.  Lets a test
    drive a tenant up to a cap without spinning the real AI/upload
    pipeline."""
    with client.app_module.db() as conn:
        for _ in range(count):
            conn.execute(
                "INSERT INTO usage_events "
                "(tenant_id, surface, kind, units, cost_micros) "
                "VALUES (?, ?, ?, ?, ?)",
                (client.test_tenant_id, surface, kind, units, cost_micros),
            )
        conn.commit()


def _set_cap(client, **caps) -> None:
    """Force a particular cap on the tenant for the test.  Goes
    through the operator override surface so we exercise the same
    code path the /admin editor uses."""
    from dao import Actor, quotas as dao_quotas
    op = Actor(
        email="op@example.com", tenant_id=None, role=None,
        is_operator=True, memberships=(), shares=(),
    )
    dao_quotas.set_overrides(op, client.test_tenant_id, **caps)


# ── Cap resolution ──────────────────────────────────────────────────


def test_plan_default_caps_apply_with_no_overrides(client):
    """Without an override row, a 'pro' tenant gets the pro plan
    defaults from ``_PLAN_DEFAULTS``."""
    from dao import quotas as dao_quotas
    caps = dao_quotas.get_caps(client.test_tenant_id)
    # Conftest creates the test tenant with plan='pro'.
    assert caps["monthly_ai_calls"] == 50_000
    assert caps["monthly_upload_bytes"] == 100 * 1024 * 1024 * 1024
    assert caps["daily_ai_cost_micros"] == 50_000_000


def test_overrides_replace_defaults(client):
    """Per-tenant override on a column overrides only that field;
    the rest fall through to the plan defaults."""
    _set_cap(client, monthly_ai_calls=42)
    from dao import quotas as dao_quotas
    caps = dao_quotas.get_caps(client.test_tenant_id)
    assert caps["monthly_ai_calls"] == 42
    # Untouched fields still come from plan defaults.
    assert caps["monthly_upload_bytes"] == 100 * 1024 * 1024 * 1024


def test_override_with_negative_clears_cap(client):
    """``set_overrides(monthly_ai_calls=-1)`` removes the override
    so the field reverts to the plan default."""
    _set_cap(client, monthly_ai_calls=42)
    _set_cap(client, monthly_ai_calls=-1)
    from dao import quotas as dao_quotas
    caps = dao_quotas.get_caps(client.test_tenant_id)
    assert caps["monthly_ai_calls"] == 50_000  # back to pro default


def test_daily_ai_cost_override_lives_in_json_blob(client):
    """``daily_ai_cost_micros`` arrived after the schema, so it
    rides in the ``overrides_json`` column.  Confirm the override
    persists + reads back."""
    _set_cap(client, daily_ai_cost_micros=12_345)
    from dao import quotas as dao_quotas
    assert dao_quotas.get_caps(client.test_tenant_id)["daily_ai_cost_micros"] == 12_345


# ── Hard 429 ────────────────────────────────────────────────────────


def test_upload_quota_exceeded_returns_429(client):
    """Pre-flight upload quota check rejects an over-cap upload
    before save_photo_bytes does the encode pass."""
    _set_cap(client, monthly_upload_bytes=1024)  # 1 KB
    # Land an existing usage_event that uses 900 bytes so a fresh
    # 200-byte upload would push us over.
    _seed_usage(client, surface="upload", kind="upload_bytes",
                units=900)
    raw = b"\xff\xd8\xff\xe0" + b"x" * 200 + b"\xff\xd9"
    # Drive through save_photo_bytes directly — same path the
    # ingest + add-item routes use.
    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        client.app_module.save_photo_bytes(
            client.test_tenant_id, raw, "test.jpg",
        )
    assert exc.value.status_code == 429


def test_ai_quota_exceeded_blocks_ingest(client, tmp_path):
    """When the monthly AI call cap is reached, /ingest refuses
    fresh photos with 429 — the hard ceiling against runaway MCP
    agents the user explicitly asked for."""
    _set_cap(client, monthly_ai_calls=2)
    _seed_usage(client, surface="ai", kind="gemini_detect",
                units=1, count=2)
    # Build a tiny synthetic JPEG payload.
    raw = b"\xff\xd8\xff\xe0" + b"x" * 100 + b"\xff\xd9"
    files = {"photos": ("a.jpg", raw, "image/jpeg")}
    r = client.post("/ingest", files=files, follow_redirects=False)
    assert r.status_code == 429
    assert "AI quota" in r.text


def test_uncapped_with_negative_cap(client):
    """Setting cap = -1 = unset means the surface is uncapped (the
    plan default returns); a -1-removed override after a giant
    seeded usage value lets the surface keep working."""
    # Seed huge usage, then unset the cap (defaults are pro = 50k).
    _set_cap(client, monthly_ai_calls=2)
    _seed_usage(client, surface="ai", kind="gemini_detect",
                units=1, count=10)  # Already over the override.
    # Now unset the override.
    _set_cap(client, monthly_ai_calls=-1)
    # The pro default is 50k, far above 10 — quota check passes.
    from dao import quotas as dao_quotas
    dao_quotas.check_or_raise(client.test_tenant_id, "ai",
                              units_about_to_record=1)


# ── Soft warning header ────────────────────────────────────────────


def test_x_quota_warning_header_at_80_percent(client):
    """Browsing /usage with 80% upload usage gets an
    ``X-Quota-Warning`` header; under 80% gets nothing."""
    _set_cap(client, monthly_upload_bytes=100)
    _seed_usage(client, surface="upload", kind="upload_bytes",
                units=85)
    r = client.get("/usage")
    warning = r.headers.get("X-Quota-Warning")
    assert warning is not None
    assert "monthly_upload_bytes" in warning


def test_no_x_quota_warning_under_80_percent(client):
    """Quiet path: the header doesn't get stamped on responses
    where every cap is well under 80%."""
    _set_cap(client, monthly_upload_bytes=1024 * 1024 * 1024)
    _seed_usage(client, surface="upload", kind="upload_bytes",
                units=1024)  # ~0% of 1 GB
    r = client.get("/usage")
    assert "X-Quota-Warning" not in r.headers


# ── Operator override (admin surface) ──────────────────────────────


def test_set_overrides_audits(client):
    """Operator quota overrides leave an audit_log row keyed to
    the targeted tenant."""
    _set_cap(client, monthly_ai_calls=42, daily_ai_cost_micros=1234)
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT actor_email, action, target_id FROM audit_log "
            "WHERE action = 'quota.override'"
        ).fetchone()
    assert row["actor_email"] == "op@example.com"
    assert row["target_id"] == client.test_tenant_id
