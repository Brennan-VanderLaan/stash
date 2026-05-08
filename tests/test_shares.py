"""Phase 6 — object shares end to end.

Coverage targets, in spec order:

* DAO: create / revoke / idempotent re-create + role widen.
* Cascade-on-add: a box share grants access to current + future
  items in that box.
* Follows-on-move: an item share sticks to the item across box
  moves; a box share scopes by box (item moving out loses it).
* Dedupe with membership: max(membership_role, share_role).
* Paused on soft-delete: a soft-deleted granting tenant disappears
  from the recipient's share list and DAO access checks.

HTTP layer:

* /shared renders for a share-only recipient (middleware bypass).
* /shared/box/{id} read-only view.
* Outbound revoke from /usage cuts access immediately.
"""

from __future__ import annotations

import base64
import importlib
import secrets
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── Setup helpers ───────────────────────────────────────────────────


def _bootstrap(tmp_path, monkeypatch):
    """Two tenants, both with a maintainer.  T1 has a box + item we
    can share into a recipient with no membership."""
    monkeypatch.setenv("STASH_DB", str(tmp_path / "stash.db"))
    monkeypatch.setenv("STASH_UPLOADS", str(tmp_path / "uploads"))
    monkeypatch.setenv(
        "STASH_KEK",
        base64.b64encode(secrets.token_bytes(32)).decode(),
    )
    monkeypatch.delenv("STASH_OPERATOR_EMAILS", raising=False)
    if "app" in sys.modules:
        del sys.modules["app"]
    import app as app_module
    importlib.reload(app_module)
    import vault
    vault.clear_dek_cache()

    with app_module.db() as conn:
        cur = conn.execute(
            "INSERT INTO tenants (name, plan) VALUES ('T1', 'pro')"
        )
        t1 = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO tenants (name, plan) VALUES ('T2', 'pro')"
        )
        t2 = cur.lastrowid
        conn.execute(
            "INSERT INTO tenant_members "
            "(tenant_id, email, role, joined_at) "
            "VALUES (?, 'owner@t1.example', 'maintainer', "
            " CURRENT_TIMESTAMP)",
            (t1,),
        )
        conn.execute(
            "INSERT INTO tenant_members "
            "(tenant_id, email, role, joined_at) "
            "VALUES (?, 'owner@t2.example', 'maintainer', "
            " CURRENT_TIMESTAMP)",
            (t2,),
        )
        cur = conn.execute(
            "INSERT INTO boxes (name, location, notes, tenant_id) "
            "VALUES ('Kitchen', 'A', '', ?)",
            (t1,),
        )
        box_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO items (box_id, name, notes, tenant_id) "
            "VALUES (?, 'Whisk', '', ?)",
            (box_id, t1),
        )
        item_id = cur.lastrowid
        conn.commit()
    return app_module, dict(t1=t1, t2=t2, box_id=box_id, item_id=item_id)


def _actor(email, *, tenant_id=None, role=None, is_operator=False,
           memberships=(), shares=()):
    from dao import Actor
    return Actor(
        email=email,
        tenant_id=tenant_id,
        role=role,
        is_operator=is_operator,
        memberships=memberships,
        shares=shares,
    )


# ── DAO surface ─────────────────────────────────────────────────────


def test_create_box_share_then_recipient_sees_it(tmp_path, monkeypatch):
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    from dao import shares as dao_shares
    owner = _actor("owner@t1.example", tenant_id=ids["t1"],
                   role="maintainer",
                   memberships=((ids["t1"], "maintainer"),))
    dao_shares.create(owner, target_kind="box", target_id=ids["box_id"],
                      recipient_email="friend@example.com")
    listed = dao_shares.list_for_recipient("friend@example.com")
    assert len(listed) == 1
    assert listed[0]["target_kind"] == "box"
    assert listed[0]["target_id"] == ids["box_id"]
    assert listed[0]["tenant_name"] == "T1"
    assert listed[0]["target_label"] == "Kitchen"


def test_create_share_for_other_tenant_is_404(tmp_path, monkeypatch):
    """A maintainer of T1 must not be able to share T2's box even
    if they happen to know its id."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    from dao import shares as dao_shares, NotFoundError
    # Make a box on T2 to attempt to share.
    with app_mod.db() as conn:
        cur = conn.execute(
            "INSERT INTO boxes (name, location, notes, tenant_id) "
            "VALUES ('Theirs', 'B', '', ?)",
            (ids["t2"],),
        )
        t2_box = cur.lastrowid
        conn.commit()
    owner = _actor("owner@t1.example", tenant_id=ids["t1"],
                   role="maintainer",
                   memberships=((ids["t1"], "maintainer"),))
    with pytest.raises(NotFoundError):
        dao_shares.create(owner, target_kind="box", target_id=t2_box,
                          recipient_email="x@example.com")


def test_create_share_idempotent_widens_role(tmp_path, monkeypatch):
    """Creating the same (target, recipient) twice updates role
    rather than 500'ing on the unique-ish triple, and a second
    create after revoke resurrects."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    from dao import shares as dao_shares
    owner = _actor("owner@t1.example", tenant_id=ids["t1"],
                   role="maintainer",
                   memberships=((ids["t1"], "maintainer"),))
    a = dao_shares.create(owner, target_kind="box",
                          target_id=ids["box_id"],
                          recipient_email="friend@example.com",
                          role="readonly")
    b = dao_shares.create(owner, target_kind="box",
                          target_id=ids["box_id"],
                          recipient_email="friend@example.com",
                          role="maintainer")
    # Same row, role widened.
    assert a["id"] == b["id"]
    assert b["role"] == "maintainer"
    # Revoke + recreate resurrects rather than minting a new row.
    dao_shares.revoke(owner, a["id"])
    c = dao_shares.create(owner, target_kind="box",
                          target_id=ids["box_id"],
                          recipient_email="friend@example.com",
                          role="readonly")
    assert c["id"] == a["id"]
    listed = dao_shares.list_outbound(owner)
    assert len(listed) == 1


def test_box_share_cascades_to_items(tmp_path, monkeypatch):
    """The cascade-on-add rule: a box share grants the same role on
    every item currently in the box (and any added later)."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    from dao import shares as dao_shares
    owner = _actor("owner@t1.example", tenant_id=ids["t1"],
                   role="maintainer",
                   memberships=((ids["t1"], "maintainer"),))
    dao_shares.create(owner, target_kind="box",
                      target_id=ids["box_id"],
                      recipient_email="friend@example.com",
                      role="readonly")
    # Recipient actor is purely share-driven, no membership.
    rec_shares = dao_shares.shares_for_email("friend@example.com")
    rec = _actor("friend@example.com", shares=rec_shares)
    role = dao_shares.effective_role_for_item(rec, ids["item_id"])
    assert role == "readonly"
    # Add a future item; the cascade still applies.
    with app_mod.db() as conn:
        cur = conn.execute(
            "INSERT INTO items (box_id, name, notes, tenant_id) "
            "VALUES (?, 'Spatula', '', ?)",
            (ids["box_id"], ids["t1"]),
        )
        new_item = cur.lastrowid
        conn.commit()
    role2 = dao_shares.effective_role_for_item(rec, new_item)
    assert role2 == "readonly"


def test_item_share_follows_move_box_share_does_not(tmp_path, monkeypatch):
    """Per-item share sticks to the item; per-box share scopes by
    box, so an item moving out loses access via the box share."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    from dao import shares as dao_shares
    owner = _actor("owner@t1.example", tenant_id=ids["t1"],
                   role="maintainer",
                   memberships=((ids["t1"], "maintainer"),))
    # Two recipients, one per share kind.
    dao_shares.create(owner, target_kind="box",
                      target_id=ids["box_id"],
                      recipient_email="boxshare@example.com")
    dao_shares.create(owner, target_kind="item",
                      target_id=ids["item_id"],
                      recipient_email="itemshare@example.com")

    # Move the item to a brand-new box on the same tenant.
    with app_mod.db() as conn:
        cur = conn.execute(
            "INSERT INTO boxes (name, location, notes, tenant_id) "
            "VALUES ('Garage', 'B', '', ?)",
            (ids["t1"],),
        )
        new_box = cur.lastrowid
        conn.execute(
            "UPDATE items SET box_id = ? WHERE id = ?",
            (new_box, ids["item_id"]),
        )
        conn.commit()

    bs_shares = dao_shares.shares_for_email("boxshare@example.com")
    bs = _actor("boxshare@example.com", shares=bs_shares)
    is_shares = dao_shares.shares_for_email("itemshare@example.com")
    isr = _actor("itemshare@example.com", shares=is_shares)

    # Box-share recipient: lost access to the item that moved out.
    assert dao_shares.effective_role_for_item(bs, ids["item_id"]) is None
    # Item-share recipient: still has access regardless of move.
    assert dao_shares.effective_role_for_item(isr, ids["item_id"]) == "readonly"


def test_dedupe_takes_max_role(tmp_path, monkeypatch):
    """A recipient who's also a member of the granting tenant: the
    effective role is max(membership, share).  A readonly share
    can never narrow a maintainer membership."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    from dao import shares as dao_shares
    owner = _actor("owner@t1.example", tenant_id=ids["t1"],
                   role="maintainer",
                   memberships=((ids["t1"], "maintainer"),))
    # Make Bob a *readonly* member of T1.  Then share the box to Bob
    # at maintainer role.  Effective should be maintainer.
    with app_mod.db() as conn:
        conn.execute(
            "INSERT INTO tenant_members "
            "(tenant_id, email, role, joined_at) "
            "VALUES (?, 'bob@example.com', 'readonly', CURRENT_TIMESTAMP)",
            (ids["t1"],),
        )
        conn.commit()
    dao_shares.create(owner, target_kind="box",
                      target_id=ids["box_id"],
                      recipient_email="bob@example.com",
                      role="maintainer")
    bob = _actor(
        "bob@example.com", tenant_id=ids["t1"], role="readonly",
        memberships=((ids["t1"], "readonly"),),
        shares=dao_shares.shares_for_email("bob@example.com"),
    )
    assert dao_shares.effective_role_for_box(bob, ids["box_id"]) == "maintainer"

    # Inverse: maintainer membership + readonly share — membership
    # wins, role stays maintainer.
    dao_shares.revoke(owner, dao_shares.list_outbound(owner)[0]["id"])
    dao_shares.create(owner, target_kind="box",
                      target_id=ids["box_id"],
                      recipient_email="bob@example.com",
                      role="readonly")
    with app_mod.db() as conn:
        conn.execute(
            "UPDATE tenant_members SET role = 'maintainer' "
            "WHERE email = 'bob@example.com'"
        )
        conn.commit()
    bob = _actor(
        "bob@example.com", tenant_id=ids["t1"], role="maintainer",
        memberships=((ids["t1"], "maintainer"),),
        shares=dao_shares.shares_for_email("bob@example.com"),
    )
    assert dao_shares.effective_role_for_box(bob, ids["box_id"]) == "maintainer"


def test_share_paused_on_soft_delete(tmp_path, monkeypatch):
    """Granting tenant in soft-delete: the share rows survive but
    don't surface to the recipient."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    from dao import shares as dao_shares
    owner = _actor("owner@t1.example", tenant_id=ids["t1"],
                   role="maintainer",
                   memberships=((ids["t1"], "maintainer"),))
    dao_shares.create(owner, target_kind="box",
                      target_id=ids["box_id"],
                      recipient_email="friend@example.com")
    assert dao_shares.list_for_recipient("friend@example.com")
    # Soft-delete T1.  The share is paused.
    with app_mod.db() as conn:
        conn.execute(
            "UPDATE tenants SET deleted_at = CURRENT_TIMESTAMP "
            "WHERE id = ?", (ids["t1"],),
        )
        conn.commit()
    assert dao_shares.list_for_recipient("friend@example.com") == []
    rec = _actor("friend@example.com",
                 shares=dao_shares.shares_for_email("friend@example.com"))
    assert dao_shares.effective_role_for_box(rec, ids["box_id"]) is None
    # Reactivate: share resumes.
    with app_mod.db() as conn:
        conn.execute(
            "UPDATE tenants SET deleted_at = NULL WHERE id = ?",
            (ids["t1"],),
        )
        conn.commit()
    assert dao_shares.list_for_recipient("friend@example.com")


# ── HTTP layer ──────────────────────────────────────────────────────


def test_http_recipient_can_view_shared_box(tmp_path, monkeypatch):
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    owner_h = {"X-Forwarded-Email": "owner@t1.example"}
    rec_h = {"X-Forwarded-Email": "friend@example.com"}

    with TestClient(app_mod.app, headers=owner_h) as oc:
        # Mint share via the box-detail share button.
        r = oc.post(
            f"/boxes/{ids['box_id']}/share",
            data={"recipient_email": "friend@example.com",
                  "role": "readonly"},
            follow_redirects=False,
        )
        assert r.status_code == 303

    with TestClient(app_mod.app, headers=rec_h) as rc:
        # /shared renders + lists the box.
        r = rc.get("/shared")
        assert r.status_code == 200
        assert "Kitchen" in r.text
        # /shared/box/{id} renders the items grid.
        r = rc.get(f"/shared/box/{ids['box_id']}")
        assert r.status_code == 200
        assert "Kitchen" in r.text
        assert "Whisk" in r.text
        # / renders for a share-only actor (no membership → tenant_id
        # is None → list_with_counts returns []) but doesn't 403 them
        # out of the app entirely.  The Shared tab appears in the nav
        # so they can find /shared.
        r = rc.get("/")
        assert r.status_code == 200
        assert "/shared" in r.text  # Shared tab is in the nav.


def test_share_recipient_file_allowlist_excludes_other_files(
    tmp_path, monkeypatch,
):
    """A box-share recipient can only fetch files belonging to
    items in *that* box, not any other file in T1's tenant
    directory.  Closes the previously-noted widening regression."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    # Two boxes in T1: "Kitchen" (the existing one with item 1)
    # and "Garage" (a brand-new box with its own item + photo).
    # Share Kitchen with friend@example.com.  Friend must reach
    # Kitchen's photos, NOT Garage's.
    with app_mod.db() as conn:
        conn.execute(
            "UPDATE items SET photo='kitchen.jpg', source_photo='kitchen.jpg' "
            "WHERE id = ?",
            (ids["item_id"],),
        )
        cur = conn.execute(
            "INSERT INTO boxes (name, location, notes, tenant_id) "
            "VALUES ('Garage', 'B', '', ?)",
            (ids["t1"],),
        )
        garage_id = cur.lastrowid
        conn.execute(
            "INSERT INTO items (box_id, name, photo, source_photo, tenant_id) "
            "VALUES (?, 'Drill', 'garage.jpg', 'garage.jpg', ?)",
            (garage_id, ids["t1"]),
        )
        conn.commit()
    # Drop ciphertext-shaped placeholders into the tenant's upload
    # dir for both files so the existence check inside
    # _resolve_serve_tenant has something to find.
    upload_dir = Path(tmp_path / "uploads" / str(ids["t1"]))
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / "kitchen.jpg").write_bytes(b"k")
    (upload_dir / "garage.jpg").write_bytes(b"g")

    from dao import shares as dao_shares
    owner = _actor("owner@t1.example", tenant_id=ids["t1"],
                   role="maintainer",
                   memberships=((ids["t1"], "maintainer"),))
    dao_shares.create(owner, target_kind="box",
                      target_id=ids["box_id"],
                      recipient_email="friend@example.com")

    rec = _actor("friend@example.com",
                 shares=dao_shares.shares_for_email("friend@example.com"))
    allowed = dao_shares.file_allowlist_for_actor(rec)
    assert "kitchen.jpg" in allowed
    assert "garage.jpg" not in allowed
    # Thumb companion of the allowed file is in the set too.
    assert "kitchen_thumb.jpg" in allowed


def test_share_box_allowlist_picks_up_new_items(tmp_path, monkeypatch):
    """A box share should grant file access to items added to the
    box AFTER the share was minted (cascade-on-add applies to
    files, not just role)."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    with app_mod.db() as conn:
        conn.execute(
            "UPDATE items SET photo='original.jpg', source_photo='original.jpg' "
            "WHERE id = ?",
            (ids["item_id"],),
        )
        conn.commit()
    from dao import shares as dao_shares
    owner = _actor("owner@t1.example", tenant_id=ids["t1"],
                   role="maintainer",
                   memberships=((ids["t1"], "maintainer"),))
    dao_shares.create(owner, target_kind="box",
                      target_id=ids["box_id"],
                      recipient_email="friend@example.com")
    # Add a new item to the shared box AFTER the share exists.
    with app_mod.db() as conn:
        conn.execute(
            "INSERT INTO items (box_id, name, photo, source_photo, tenant_id) "
            "VALUES (?, 'Late arrival', 'fresh.jpg', 'fresh.jpg', ?)",
            (ids["box_id"], ids["t1"]),
        )
        conn.commit()
    rec = _actor("friend@example.com",
                 shares=dao_shares.shares_for_email("friend@example.com"))
    allowed = dao_shares.file_allowlist_for_actor(rec)
    assert "original.jpg" in allowed
    assert "fresh.jpg" in allowed


def test_share_box_allowlist_drops_files_after_item_moves_out(
    tmp_path, monkeypatch,
):
    """Per follows-on-move: when an item moves out of a shared
    box, its file drops out of the allow-list on the next
    request.  Mirrors the role-side rule from
    test_item_share_follows_move_box_share_does_not."""
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    with app_mod.db() as conn:
        conn.execute(
            "UPDATE items SET photo='movable.jpg', source_photo='movable.jpg' "
            "WHERE id = ?",
            (ids["item_id"],),
        )
        cur = conn.execute(
            "INSERT INTO boxes (name, location, notes, tenant_id) "
            "VALUES ('Other', 'B', '', ?)",
            (ids["t1"],),
        )
        other_box = cur.lastrowid
        conn.commit()
    from dao import shares as dao_shares
    owner = _actor("owner@t1.example", tenant_id=ids["t1"],
                   role="maintainer",
                   memberships=((ids["t1"], "maintainer"),))
    dao_shares.create(owner, target_kind="box",
                      target_id=ids["box_id"],
                      recipient_email="friend@example.com")
    rec = _actor("friend@example.com",
                 shares=dao_shares.shares_for_email("friend@example.com"))
    assert "movable.jpg" in dao_shares.file_allowlist_for_actor(rec)
    # Move the item to a different box.
    with app_mod.db() as conn:
        conn.execute(
            "UPDATE items SET box_id = ? WHERE id = ?",
            (other_box, ids["item_id"]),
        )
        conn.commit()
    # Recompute — now the file is gone from the allow-list.
    assert "movable.jpg" not in dao_shares.file_allowlist_for_actor(rec)


def test_http_revoke_cuts_access(tmp_path, monkeypatch):
    app_mod, ids = _bootstrap(tmp_path, monkeypatch)
    owner_h = {"X-Forwarded-Email": "owner@t1.example"}
    rec_h = {"X-Forwarded-Email": "friend@example.com"}

    with TestClient(app_mod.app, headers=owner_h) as oc:
        oc.post(
            f"/boxes/{ids['box_id']}/share",
            data={"recipient_email": "friend@example.com",
                  "role": "readonly"},
            follow_redirects=False,
        )
        # Find the share id from the outbound DAO listing.
        from dao import shares as dao_shares
        owner = _actor("owner@t1.example", tenant_id=ids["t1"],
                       role="maintainer",
                       memberships=((ids["t1"], "maintainer"),))
        outbound = dao_shares.list_outbound(owner)
        share_id = outbound[0]["id"]
        oc.post(f"/shares/{share_id}/revoke", follow_redirects=False)

    with TestClient(app_mod.app, headers=rec_h) as rc:
        r = rc.get(f"/shared/box/{ids['box_id']}", follow_redirects=False)
        # No membership + no share = back to the global 403 wall.
        assert r.status_code == 403
