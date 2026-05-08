"""DAO-layer tests.

Exercise the data-access functions directly (no FastAPI routes) so a
regression in the layer surfaces here, not via downstream route
breakage.  The goal is two-fold:

* prove tenancy isolation — tenant A cannot see tenant B's rows
  through any read method.
* prove role enforcement — readonly actors can't call any mutation
  method.
"""

from __future__ import annotations

import pytest


from dao import (
    Actor,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    require_role,
)
from dao import boxes as boxes_dao
from dao import items as items_dao
from dao import tenants as tenants_dao


def _maintainer(client, tenant_id: int) -> Actor:
    return Actor(
        email=client.test_email,
        tenant_id=tenant_id,
        role="maintainer",
        is_operator=False,
        memberships=((tenant_id, "maintainer"),),
    )


def _readonly(client, tenant_id: int) -> Actor:
    return Actor(
        email="readonly@example.com",
        tenant_id=tenant_id,
        role="readonly",
        is_operator=False,
        memberships=((tenant_id, "readonly"),),
    )


def _another_tenant(client, name: str = "Other") -> int:
    """Helper: create a second tenant alongside the one conftest already
    set up.  Returns its id."""
    with client.app_module.db() as conn:
        cur = conn.execute(
            "INSERT INTO tenants (name, plan) VALUES (?, 'pro')", (name,)
        )
        conn.commit()
    return cur.lastrowid


# ── Tenancy isolation ───────────────────────────────────────────────


def test_boxes_list_is_scoped_to_active_tenant(client):
    """Two tenants, each with one box → each actor sees only their
    own."""
    a = _maintainer(client, client.test_tenant_id)
    b_tenant = _another_tenant(client)
    b = _maintainer(client, b_tenant)

    boxes_dao.create(a, name="Tenant A's box")
    boxes_dao.create(b, name="Tenant B's box")

    a_view = boxes_dao.list_with_counts(a)
    b_view = boxes_dao.list_with_counts(b)

    assert {row["name"] for row in a_view} == {"Tenant A's box"}
    assert {row["name"] for row in b_view} == {"Tenant B's box"}


def test_box_get_by_id_404s_across_tenants(client):
    """Tenant A asks for tenant B's box id → NotFoundError, not a
    leak."""
    a = _maintainer(client, client.test_tenant_id)
    b_tenant = _another_tenant(client)
    b = _maintainer(client, b_tenant)

    other_box = boxes_dao.create(b, name="theirs")
    with pytest.raises(NotFoundError):
        boxes_dao.get_by_id(a, other_box)


def test_items_list_for_box_is_tenant_scoped(client):
    a = _maintainer(client, client.test_tenant_id)
    b_tenant = _another_tenant(client)
    b = _maintainer(client, b_tenant)

    a_box = boxes_dao.create(a, name="A")
    b_box = boxes_dao.create(b, name="B")
    items_dao.create(a, a_box, name="a-item")
    items_dao.create(b, b_box, name="b-item")

    # The call returns rows scoped to the actor's tenant; passing
    # the other tenant's box id yields no rows.
    assert [it["name"] for it in items_dao.list_for_box(a, a_box)] == ["a-item"]
    assert items_dao.list_for_box(a, b_box) == []


# ── Role enforcement ────────────────────────────────────────────────


def test_readonly_actor_cannot_create_box(client):
    """Spec § Roles: readonly cannot mutate."""
    actor = _readonly(client, client.test_tenant_id)
    with pytest.raises(ForbiddenError):
        boxes_dao.create(actor, name="should fail")


def test_readonly_actor_cannot_delete_item(client):
    a = _maintainer(client, client.test_tenant_id)
    box = boxes_dao.create(a, name="box")
    item = items_dao.create(a, box, name="item")

    ro = _readonly(client, client.test_tenant_id)
    with pytest.raises(ForbiddenError):
        items_dao.delete(ro, item)


def test_require_role_unknown_minimum_raises_value_error():
    actor = Actor(email="x@y", tenant_id=1, role="maintainer",
                  is_operator=False, memberships=((1, "maintainer"),))
    with pytest.raises(ValueError):
        require_role(actor, "wizard")


# ── Optimistic concurrency ──────────────────────────────────────────


def test_box_update_with_matching_if_match_succeeds(client):
    a = _maintainer(client, client.test_tenant_id)
    box_id = boxes_dao.create(a, name="orig")
    new_version = boxes_dao.update(
        a, box_id,
        name="updated", location="", notes="", room_id=None,
        if_match=1,
    )
    assert new_version == 2
    assert boxes_dao.get_by_id(a, box_id)["version"] == 2


def test_box_update_with_stale_if_match_raises_conflict(client):
    a = _maintainer(client, client.test_tenant_id)
    box_id = boxes_dao.create(a, name="orig")
    boxes_dao.update(a, box_id, name="first edit",
                     location="", notes="", room_id=None, if_match=1)
    # Second edit with the now-stale version 1 → 409
    with pytest.raises(ConflictError):
        boxes_dao.update(a, box_id, name="second edit",
                         location="", notes="", room_id=None, if_match=1)


def test_box_update_without_if_match_skips_concurrency_check(client):
    """``if_match=None`` is the legacy path used by routes that
    haven't been migrated to optimistic concurrency yet — accept any
    current version, just bump."""
    a = _maintainer(client, client.test_tenant_id)
    box_id = boxes_dao.create(a, name="orig")
    boxes_dao.update(a, box_id, name="x", location="", notes="", room_id=None)
    boxes_dao.update(a, box_id, name="y", location="", notes="", room_id=None)
    assert boxes_dao.get_by_id(a, box_id)["version"] == 3


# ── Membership lookup ──────────────────────────────────────────────


def test_memberships_for_email_returns_active_membership(client):
    rows = tenants_dao.memberships_for_email(client.test_email)
    assert (client.test_tenant_id, "maintainer") in rows


def test_memberships_for_unknown_email_is_empty(client):
    assert tenants_dao.memberships_for_email("nobody@example.com") == ()
