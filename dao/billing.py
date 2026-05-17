"""Stripe billing — Pro tier upgrade flow + entitlement webhook.

The Pro tier is "same features, higher caps" — quota
differentiation lives in :mod:`dao.quotas` (`_PLAN_DEFAULTS`).
This module handles the *boundary* between Stripe and stash:

* Create a Checkout session for a Free tenant who wants to upgrade.
* Create a Customer Portal session for an existing subscriber so
  they can manage their own subscription without us building a
  portal UI.
* Process Stripe webhook events to flip ``tenants.plan`` and the
  subscription metadata columns on the tenants row.

Configuration lives in three env vars — missing any of them puts
stash in "billing disabled" mode (the Upgrade CTA hides, the
webhook returns 503).  Same pattern as the B2 surface.

  STRIPE_SECRET_KEY     — the platform secret key
  STRIPE_WEBHOOK_SECRET — webhook signing secret for /webhooks/stripe
  STRIPE_PRICE_ID_PRO   — the Stripe Price object id for Pro
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import obs
from dao._base import Actor, NotFoundError, db, require_role


_log = obs.get_logger("dao.billing")


# ── Configuration discovery ────────────────────────────────────────


class BillingNotConfiguredError(RuntimeError):
    """Raised when a billing route fires without the required env
    vars.  The route layer translates this into 503 so the operator
    sees a clean "set STRIPE_SECRET_KEY" message rather than a
    Python traceback."""


def _config() -> dict:
    """Read the Stripe env vars.  Raises ``BillingNotConfiguredError``
    when any are missing — the upgrade flow checks this up front so
    a free tenant doesn't see an Upgrade button that 500s on click."""
    secret = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    webhook = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    price = os.environ.get("STRIPE_PRICE_ID_PRO", "").strip()
    if not secret or not webhook or not price:
        raise BillingNotConfiguredError(
            "Set STRIPE_SECRET_KEY + STRIPE_WEBHOOK_SECRET + "
            "STRIPE_PRICE_ID_PRO to enable billing.",
        )
    return {
        "secret_key": secret,
        "webhook_secret": webhook,
        "price_id": price,
    }


def is_configured() -> bool:
    """Cheap predicate for the UI layer.  Hides the Upgrade CTA
    when False so users don't tap into a 503."""
    try:
        _config()
        return True
    except BillingNotConfiguredError:
        return False


def _stripe():
    """Lazy import + apikey-stamp the SDK.  Lazy because the import
    pulls in the whole stripe namespace at runtime; we only need it
    on the upgrade + webhook code paths."""
    import stripe
    cfg = _config()
    stripe.api_key = cfg["secret_key"]
    return stripe, cfg


# Module-level cache: env value → resolved Stripe Price id.  Keyed
# on the operator-supplied input so a re-read of the env var (with
# the same value) is free.  Only the product→price lookup needs
# the network round-trip; explicit ``price_*`` ids pass through.
_PRICE_RESOLUTION_CACHE: dict[str, str] = {}


def _resolve_price_id(stripe_module, configured: str) -> str:
    """Resolve a Stripe Price id from ``STRIPE_PRICE_ID_PRO``.

    Easy operator mix-up: the Stripe Dashboard's product page shows
    both the product id (``prod_*``) and the price ids attached to
    it (``price_*``) right next to each other, and pasting the
    product id is the natural "I see Stash Pro, I'll grab that id"
    move.  Checkout sessions need the Price id though — the price
    is what carries the amount, interval, and currency the customer
    actually pays.

    If ``configured`` already looks like a Price id (the common
    case) we pass it through unchanged.  If it's a Product id
    (``prod_*``), we look up the product's ``default_price`` and
    use that — a one-time API call per process startup.  Everything
    else (typo, deleted object, wrong-mode key) we pass through so
    Stripe surfaces its own error naturally.

    Cached on the configured value so reloads of ``_config()``
    don't re-hit the network."""
    if configured in _PRICE_RESOLUTION_CACHE:
        return _PRICE_RESOLUTION_CACHE[configured]
    if not configured.startswith("prod_"):
        _PRICE_RESOLUTION_CACHE[configured] = configured
        return configured
    # It's a product id.  Pull the product + use its default price.
    try:
        product = stripe_module.Product.retrieve(configured)
    except Exception as exc:
        # Don't poison the cache on a transient failure — a retry
        # should be able to succeed.
        raise BillingNotConfiguredError(
            f"STRIPE_PRICE_ID_PRO is set to product '{configured}' "
            f"but that product can't be retrieved from Stripe: "
            f"{exc}.  Double-check it exists in the same Stripe "
            f"account as STRIPE_SECRET_KEY (live vs test mode is "
            f"a common mismatch).",
        ) from exc
    # Stripe's ``StripeObject`` is dict-like but doesn't expose
    # ``.get()`` — that attribute access goes through __getattr__
    # which raises ``AttributeError: get`` because there's no
    # ``get`` key in the response.  Use ``getattr`` with a default
    # so a product without ``default_price`` falls through to the
    # clearer error below instead of crashing the request.
    default_price = getattr(product, "default_price", None)
    if not default_price:
        raise BillingNotConfiguredError(
            f"STRIPE_PRICE_ID_PRO is set to product '{configured}' "
            f"but that product has no default price.  Either set "
            f"STRIPE_PRICE_ID_PRO to the Price id (starts with "
            f"'price_') from the product's Pricing section in the "
            f"Stripe Dashboard, or open the product in Stripe and "
            f"set one of its prices as the default.",
        )
    # ``default_price`` is normally a string id, but with ``expand``
    # parameters it can be a nested object (StripeObject or dict).
    # Handle every shape rather than assume.
    if isinstance(default_price, str):
        resolved = default_price
    else:
        resolved = getattr(default_price, "id", None)
        if resolved is None and isinstance(default_price, dict):
            resolved = default_price.get("id")
    if not resolved or not str(resolved).startswith("price_"):
        raise BillingNotConfiguredError(
            f"Product '{configured}' has a default_price of "
            f"'{resolved}' which doesn't look like a Price id.  "
            f"Re-set STRIPE_PRICE_ID_PRO to a concrete 'price_*' "
            f"value from the Stripe Dashboard.",
        )
    _log.info(
        "billing.price_resolved product_id=%s -> price_id=%s",
        configured, resolved,
    )
    _PRICE_RESOLUTION_CACHE[configured] = resolved
    return resolved


# ── Checkout + portal sessions ─────────────────────────────────────


def create_checkout_session(
    actor: Actor, *, success_url: str, cancel_url: str,
) -> str:
    """Return the URL the user's browser should redirect to so they
    can complete payment on Stripe's hosted Checkout.  Creates (or
    reuses) the Stripe Customer linked to the tenant, then opens a
    Checkout session for the Pro Price.

    Maintainer-only.  A Free-tier readonly member shouldn't be able
    to flip the tenant onto a paid plan without the maintainer's
    consent.
    """
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError("no active tenant")
    stripe, cfg = _stripe()
    price_id = _resolve_price_id(stripe, cfg["price_id"])
    customer_id = _ensure_stripe_customer(actor.tenant_id, actor.email)
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        # client_reference_id lets the webhook map an event back to
        # the tenant even before the subscription row exists — Stripe
        # only stamps ``customer`` on later events, but
        # ``client_reference_id`` rides on the initial
        # checkout.session.completed event.
        client_reference_id=str(actor.tenant_id),
        allow_promotion_codes=True,
        # Stripe Tax does the multi-jurisdiction sales-tax math.
        # Required setup (on Stripe's side):
        #   1. Stripe Dashboard → Settings → Tax → enable + register
        #      your business addresses where you have nexus (e.g.
        #      MA for us).
        #   2. Each Price in the dashboard needs a tax category set
        #      (Stash subscriptions are "Software as a Service /
        #      digital service").
        # With those in place this flag flips Stripe Checkout into
        # tax-aware mode: the customer's address determines the
        # rate; tax is shown as a line item; Stripe collects it on
        # our behalf and we don't see the money or owe the remit.
        automatic_tax={"enabled": True},
        # Tax-aware checkout needs the customer's address; Stripe
        # collects it during the flow when this is set to "auto".
        customer_update={"address": "auto"},
    )
    _log.info(
        "billing.checkout_created tenant_id=%s session_id=%s",
        actor.tenant_id, session.id,
    )
    return session.url


def create_portal_session(actor: Actor, *, return_url: str) -> str:
    """Return the Customer Portal URL so the user can manage their
    subscription (cancel, update card, see invoices) without us
    building any of that UI."""
    require_role(actor, "maintainer")
    if actor.tenant_id is None:
        raise NotFoundError("no active tenant")
    stripe, _ = _stripe()
    with db() as conn:
        row = conn.execute(
            "SELECT stripe_customer_id FROM tenants WHERE id = ?",
            (actor.tenant_id,),
        ).fetchone()
    if row is None or not row["stripe_customer_id"]:
        raise NotFoundError(
            "Tenant has no Stripe customer yet — upgrade first.",
        )
    session = stripe.billing_portal.Session.create(
        customer=row["stripe_customer_id"],
        return_url=return_url,
    )
    return session.url


def _ensure_stripe_customer(tenant_id: int, actor_email: str) -> str:
    """Find or create the Stripe Customer mapped to the tenant.
    Stamps the id back onto ``tenants.stripe_customer_id`` so the
    next upgrade attempt reuses it.

    Stripe's idempotency is per-key, not per-customer — we don't
    pass an idempotency key here because the lookup-before-create
    already prevents duplicates."""
    stripe, _ = _stripe()
    with db() as conn:
        row = conn.execute(
            "SELECT name, stripe_customer_id FROM tenants WHERE id = ?",
            (tenant_id,),
        ).fetchone()
    if row is None:
        raise NotFoundError(f"tenant {tenant_id}")
    if row["stripe_customer_id"]:
        return row["stripe_customer_id"]
    customer = stripe.Customer.create(
        email=actor_email,
        name=row["name"],
        metadata={"stash_tenant_id": str(tenant_id)},
    )
    with db() as conn:
        conn.execute(
            "UPDATE tenants SET stripe_customer_id = ? WHERE id = ?",
            (customer.id, tenant_id),
        )
        conn.commit()
    _log.info(
        "billing.customer_created tenant_id=%s customer_id=%s",
        tenant_id, customer.id,
    )
    return customer.id


# ── Webhook handler ────────────────────────────────────────────────


# Statuses Stripe sends that mean "subscription is healthy enough
# to entitle the customer to Pro."  Anything else (canceled,
# unpaid, incomplete, past_due after grace) flips back to free.
# We treat trialing the same as active because the user has access
# to the product during a trial; the operator is exposed to the
# Stripe-side cost regardless.
_PRO_ACTIVE_STATUSES = {"active", "trialing"}


def process_webhook_event(payload: bytes, signature: str) -> dict:
    """Verify a Stripe webhook payload's signature, then process
    the event.  Returns a small dict describing what changed so the
    route layer can audit-log + render a sane response.

    Untrusted payload — never trust the body before verification.
    Stripe's library raises ``stripe.error.SignatureVerificationError``
    on a bad signature; the route handler turns that into 400.

    Events we handle:
      * checkout.session.completed — the subscription has been
        purchased.  Stamp the customer + subscription id onto the
        tenant, plan='pro', subscription_status='active'.
      * customer.subscription.updated — status / period_end may have
        changed; mirror them.
      * customer.subscription.deleted — subscription canceled or
        expired; plan back to 'free'.
      * invoice.payment_failed — log + leave plan='pro' (Stripe will
        retry; ``customer.subscription.updated`` will downgrade if
        it eventually moves to ``past_due`` -> ``canceled``).
    """
    stripe, cfg = _stripe()
    event = stripe.Webhook.construct_event(
        payload, signature, cfg["webhook_secret"],
    )
    et = event["type"]
    data = event["data"]["object"]
    _log.info("billing.webhook event_type=%s id=%s", et, event["id"])

    if et == "checkout.session.completed":
        return _handle_checkout_completed(data)
    if et == "customer.subscription.updated":
        return _handle_subscription_updated(data)
    if et == "customer.subscription.deleted":
        return _handle_subscription_deleted(data)
    if et == "invoice.payment_failed":
        return {"action": "noop", "reason": "payment_failed_logged",
                "event_type": et}
    return {"action": "noop", "reason": "unhandled_event_type",
            "event_type": et}


def _handle_checkout_completed(session: dict) -> dict:
    """Initial purchase.  Stamp every field we got, flip plan to
    pro.  client_reference_id tells us which tenant (set when we
    created the Checkout session)."""
    tenant_id_raw = session.get("client_reference_id")
    if not tenant_id_raw:
        _log.warning(
            "billing.webhook.checkout.no_client_ref session=%s",
            session.get("id"),
        )
        return {"action": "skip", "reason": "no_client_reference"}
    try:
        tenant_id = int(tenant_id_raw)
    except (TypeError, ValueError):
        return {"action": "skip", "reason": "bad_client_reference"}
    customer_id = session.get("customer")
    subscription_id = session.get("subscription")
    with db() as conn:
        conn.execute(
            "UPDATE tenants SET plan = 'pro', "
            "                    stripe_customer_id = COALESCE(?, stripe_customer_id), "
            "                    stripe_subscription_id = ?, "
            "                    subscription_status = 'active' "
            "WHERE id = ?",
            (customer_id, subscription_id, tenant_id),
        )
        obs.write_audit(
            conn,
            tenant_id=tenant_id,
            actor_email="stripe.webhook",
            action="billing.upgraded",
            target_kind="tenant",
            target_id=tenant_id,
            metadata={
                "subscription_id": subscription_id,
                "customer_id": customer_id,
            },
        )
        conn.commit()
    _log.info(
        "billing.upgraded tenant_id=%s subscription_id=%s",
        tenant_id, subscription_id,
    )
    return {"action": "upgraded", "tenant_id": tenant_id}


def _handle_subscription_updated(sub: dict) -> dict:
    """Status / period change.  Look up the tenant by Stripe
    subscription id (or customer id as fallback) and mirror the
    new state."""
    sub_id = sub.get("id")
    customer_id = sub.get("customer")
    status = sub.get("status")
    period_end = _iso_from_ts(sub.get("current_period_end"))
    tenant_id = _tenant_for_subscription(sub_id, customer_id)
    if tenant_id is None:
        return {"action": "skip", "reason": "tenant_not_found",
                "subscription_id": sub_id}
    plan = "pro" if status in _PRO_ACTIVE_STATUSES else "free"
    with db() as conn:
        conn.execute(
            "UPDATE tenants SET plan = ?, "
            "                    subscription_status = ?, "
            "                    subscription_current_period_end = ? "
            "WHERE id = ?",
            (plan, status, period_end, tenant_id),
        )
        obs.write_audit(
            conn,
            tenant_id=tenant_id,
            actor_email="stripe.webhook",
            action="billing.subscription_updated",
            target_kind="tenant",
            target_id=tenant_id,
            metadata={"status": status, "plan": plan,
                      "subscription_id": sub_id},
        )
        conn.commit()
    return {"action": "synced", "tenant_id": tenant_id,
            "plan": plan, "status": status}


def _handle_subscription_deleted(sub: dict) -> dict:
    """Cancellation / expiry — back to free."""
    sub_id = sub.get("id")
    customer_id = sub.get("customer")
    tenant_id = _tenant_for_subscription(sub_id, customer_id)
    if tenant_id is None:
        return {"action": "skip", "reason": "tenant_not_found"}
    with db() as conn:
        conn.execute(
            "UPDATE tenants SET plan = 'free', "
            "                    subscription_status = 'canceled' "
            "WHERE id = ?",
            (tenant_id,),
        )
        obs.write_audit(
            conn,
            tenant_id=tenant_id,
            actor_email="stripe.webhook",
            action="billing.downgraded",
            target_kind="tenant",
            target_id=tenant_id,
            metadata={"subscription_id": sub_id, "reason": "canceled"},
        )
        conn.commit()
    return {"action": "downgraded", "tenant_id": tenant_id}


def _tenant_for_subscription(sub_id: str | None,
                              customer_id: str | None) -> Optional[int]:
    """Find the tenant a webhook event maps to.  Prefer the
    subscription id (stamped at checkout completion); fall back to
    the customer id (always present)."""
    with db() as conn:
        if sub_id:
            row = conn.execute(
                "SELECT id FROM tenants WHERE stripe_subscription_id = ?",
                (sub_id,),
            ).fetchone()
            if row:
                return row["id"]
        if customer_id:
            row = conn.execute(
                "SELECT id FROM tenants WHERE stripe_customer_id = ?",
                (customer_id,),
            ).fetchone()
            if row:
                return row["id"]
    return None


def _iso_from_ts(ts: int | None) -> str | None:
    """Convert a Stripe Unix epoch timestamp to ISO-8601 for storage.
    Stripe sends seconds-since-epoch; we want a sortable string."""
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


# ── Read paths ─────────────────────────────────────────────────────


def subscription_for_tenant(tenant_id: int) -> dict | None:
    """Surface the subscription metadata for the /usage page.
    Returns None when the tenant has no Stripe linkage at all."""
    with db() as conn:
        row = conn.execute(
            "SELECT plan, stripe_customer_id, stripe_subscription_id, "
            "       subscription_status, subscription_current_period_end "
            "FROM tenants WHERE id = ?",
            (tenant_id,),
        ).fetchone()
    if row is None:
        return None
    if not row["stripe_customer_id"] and row["plan"] == "free":
        return None
    return dict(row)
