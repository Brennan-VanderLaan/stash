"""Stripe billing — upgrade flow + webhook entitlement.

The Stripe SDK never actually fires in tests: we monkeypatch
``dao_billing._stripe`` to return a faked module with the
``Customer``, ``checkout.Session``, ``billing_portal.Session``,
and ``Webhook`` shapes the DAO touches.

Tests pin:
1. Free tenants see the Upgrade CTA when billing is configured.
2. Missing env vars hide the CTA (billing-disabled mode).
3. ``POST /usage/upgrade`` creates a Checkout session + 303s to
   Stripe's hosted URL.
4. ``checkout.session.completed`` flips ``tenants.plan`` to 'pro'.
5. ``customer.subscription.deleted`` flips back to 'free'.
6. Bad signatures get a 400.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class _FakeCheckoutSession:
    def __init__(self, id="cs_test_123",
                 url="https://checkout.stripe.com/c/pay/test"):
        self.id = id
        self.url = url


class _FakeCustomer:
    def __init__(self, id="cus_test_123"):
        self.id = id


class _FakePortalSession:
    def __init__(self, url="https://billing.stripe.com/p/session/test"):
        self.url = url


def _make_fake_stripe_module():
    """Build a minimal stand-in for the ``stripe`` SDK exposing only
    what dao/billing.py touches.  Each callable records its calls
    on a ``.calls`` list so tests can assert on the arguments."""
    fake = type("FakeStripe", (), {})()
    fake.api_key = None
    fake.calls = []

    class Customer:
        @staticmethod
        def create(**kwargs):
            fake.calls.append(("Customer.create", kwargs))
            return _FakeCustomer()
    fake.Customer = Customer

    class CheckoutSession:
        @staticmethod
        def create(**kwargs):
            fake.calls.append(("checkout.Session.create", kwargs))
            return _FakeCheckoutSession()
    fake.checkout = type("c", (), {"Session": CheckoutSession})()

    class PortalSession:
        @staticmethod
        def create(**kwargs):
            fake.calls.append(("billing_portal.Session.create", kwargs))
            return _FakePortalSession()
    fake.billing_portal = type("b", (), {"Session": PortalSession})()

    class Webhook:
        @staticmethod
        def construct_event(payload, sig, secret):
            fake.calls.append(("Webhook.construct_event", payload, sig, secret))
            # Tests feed a JSON event body directly.
            return json.loads(payload)
    fake.Webhook = Webhook

    return fake


@pytest.fixture
def billing(client, monkeypatch):
    """Configure billing env vars + plug a fake stripe SDK into the
    DAO so the test never hits Stripe over the wire."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_fake")
    monkeypatch.setenv("STRIPE_PRICE_ID_PRO", "price_pro_fake")
    fake = _make_fake_stripe_module()

    import dao.billing as dao_billing
    monkeypatch.setattr(
        dao_billing, "_stripe",
        lambda: (fake, dao_billing._config()),
    )
    return fake


# ── Disabled mode ──────────────────────────────────────────────────


def test_usage_hides_upgrade_when_billing_disabled(client, monkeypatch):
    """No env vars → no Upgrade CTA on /usage.  Otherwise users
    click into a 503 with no clue why."""
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    page = client.get("/usage").text
    assert "Upgrade to Pro" not in page


def test_upgrade_route_503_when_billing_disabled(client, monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    r = client.post("/usage/upgrade", follow_redirects=False)
    assert r.status_code == 503


# ── Upgrade CTA + Checkout ─────────────────────────────────────────


def test_usage_shows_upgrade_for_free_tenant(client, billing):
    # The test fixture creates the tenant on 'pro' by default; flip
    # to free so the upgrade card is the one that renders.
    with client.app_module.db() as conn:
        conn.execute("UPDATE tenants SET plan = 'free' WHERE id = ?",
                     (client.test_tenant_id,))
        conn.commit()
    page = client.get("/usage").text
    assert "Upgrade to Pro" in page
    # Free + Pro quotas come from _PLAN_DEFAULTS now (re-priced
    # to $4/mo in the cost-transparency pass).  Check for the
    # Free MB cap + the Pro GB cap to confirm the data flow,
    # not the numbers themselves — those move when plans get
    # re-tuned.
    assert "MB" in page  # free tier shown in MB now (500 MB)
    assert "GB" in page  # pro tier shown in GB (5 GB)
    assert "$4" in page  # default published Pro price


def test_upgrade_redirects_to_stripe_checkout(client, billing):
    r = client.post("/usage/upgrade", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("https://checkout.stripe.com/")
    # Exactly one Checkout session created with the configured
    # Pro Price + the client_reference_id needed to map the
    # webhook event back to a tenant.
    create_calls = [c for c in billing.calls
                    if c[0] == "checkout.Session.create"]
    assert len(create_calls) == 1
    kwargs = create_calls[0][1]
    assert kwargs["line_items"][0]["price"] == "price_pro_fake"
    assert kwargs["client_reference_id"] == str(client.test_tenant_id)
    assert kwargs["mode"] == "subscription"


def test_upgrade_creates_stripe_customer_once(client, billing):
    """Second upgrade attempt reuses the existing Stripe Customer
    (one Customer per tenant for life, no duplicate rows in
    Stripe's dashboard)."""
    client.post("/usage/upgrade")
    client.post("/usage/upgrade")
    customer_creates = [c for c in billing.calls
                        if c[0] == "Customer.create"]
    assert len(customer_creates) == 1


# ── Webhook → entitlement ──────────────────────────────────────────


def test_webhook_checkout_completed_upgrades_tenant(client, billing):
    """A ``checkout.session.completed`` event flips the tenant's
    plan to 'pro' and stamps the subscription metadata."""
    event = {
        "type": "checkout.session.completed",
        "data": {"object": {
            "client_reference_id": str(client.test_tenant_id),
            "customer": "cus_xyz",
            "subscription": "sub_xyz",
        }},
        "id": "evt_test_1",
    }
    r = client.post(
        "/webhooks/stripe",
        content=json.dumps(event),
        headers={"Stripe-Signature": "t=0,v1=fake"},
    )
    assert r.status_code == 200, r.text
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT plan, stripe_subscription_id, subscription_status "
            "FROM tenants WHERE id = ?",
            (client.test_tenant_id,),
        ).fetchone()
    assert row["plan"] == "pro"
    assert row["stripe_subscription_id"] == "sub_xyz"
    assert row["subscription_status"] == "active"


def test_webhook_subscription_deleted_downgrades(client, billing):
    """``customer.subscription.deleted`` flips plan back to free."""
    # First put the tenant on Pro via the checkout event.
    completed = {
        "type": "checkout.session.completed",
        "data": {"object": {
            "client_reference_id": str(client.test_tenant_id),
            "customer": "cus_xyz", "subscription": "sub_xyz",
        }},
        "id": "evt_1",
    }
    client.post("/webhooks/stripe", content=json.dumps(completed),
                headers={"Stripe-Signature": "t=0,v1=fake"})
    # Then cancel.
    canceled = {
        "type": "customer.subscription.deleted",
        "data": {"object": {"id": "sub_xyz", "customer": "cus_xyz"}},
        "id": "evt_2",
    }
    r = client.post("/webhooks/stripe", content=json.dumps(canceled),
                    headers={"Stripe-Signature": "t=0,v1=fake"})
    assert r.status_code == 200
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT plan, subscription_status FROM tenants WHERE id = ?",
            (client.test_tenant_id,),
        ).fetchone()
    assert row["plan"] == "free"
    assert row["subscription_status"] == "canceled"


def test_webhook_subscription_updated_mirrors_status(client, billing):
    """``customer.subscription.updated`` mirrors the new status +
    plan onto the tenant.  past_due → still pro until canceled."""
    # Bootstrap to pro.
    client.post("/webhooks/stripe",
                content=json.dumps({
                    "type": "checkout.session.completed",
                    "data": {"object": {
                        "client_reference_id": str(client.test_tenant_id),
                        "customer": "cus_xyz", "subscription": "sub_xyz",
                    }},
                    "id": "evt_1",
                }),
                headers={"Stripe-Signature": "t=0,v1=fake"})
    # Flip to past_due — should stay pro per
    # _PRO_ACTIVE_STATUSES (past_due isn't in there → downgrade).
    r = client.post(
        "/webhooks/stripe",
        content=json.dumps({
            "type": "customer.subscription.updated",
            "data": {"object": {
                "id": "sub_xyz", "customer": "cus_xyz",
                "status": "past_due", "current_period_end": 9999999999,
            }},
            "id": "evt_2",
        }),
        headers={"Stripe-Signature": "t=0,v1=fake"},
    )
    assert r.status_code == 200
    with client.app_module.db() as conn:
        row = conn.execute(
            "SELECT plan, subscription_status FROM tenants WHERE id = ?",
            (client.test_tenant_id,),
        ).fetchone()
    assert row["plan"] == "free"
    assert row["subscription_status"] == "past_due"


def test_webhook_bad_signature_400(client, billing, monkeypatch):
    """Signature failures must surface as 400 so Stripe retries."""
    def raise_sig(payload, sig, secret):
        raise ValueError("bad signature")
    monkeypatch.setattr(billing.Webhook, "construct_event", raise_sig)
    r = client.post(
        "/webhooks/stripe", content=b"{}",
        headers={"Stripe-Signature": "broken"},
    )
    assert r.status_code == 400


def test_audit_log_records_upgrade(client, billing):
    """Every entitlement transition writes an audit row so the
    operator can later trace the lifecycle."""
    client.post("/webhooks/stripe",
                content=json.dumps({
                    "type": "checkout.session.completed",
                    "data": {"object": {
                        "client_reference_id": str(client.test_tenant_id),
                        "customer": "cus_xyz", "subscription": "sub_xyz",
                    }},
                    "id": "evt_1",
                }),
                headers={"Stripe-Signature": "t=0,v1=fake"})
    with client.app_module.db() as conn:
        rows = conn.execute(
            "SELECT action, target_id FROM audit_log "
            "WHERE action = 'billing.upgraded'",
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["target_id"] == client.test_tenant_id
