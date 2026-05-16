"""Stars + leaderboard mechanics.

Stars are an alias for "shipped (``status='done'``) feedback rows
submitted by you".  No new column, no migration — the count IS
the query.  These tests pin:

* ``dao.feedback.stars_for_actor`` correctly counts only done rows
  for the given email.
* ``dao.feedback.leaderboard`` returns top-N by star count and
  respects the ignore list (operators stay off their own podium).
* The ``/leaderboard`` route renders the celebratory page for any
  signed-in tenant member.
* ``/usage`` surfaces "Your contributions" + each piece of
  feedback the user has submitted (irrespective of status).
"""

from __future__ import annotations


def _seed_feedback(client, *, body: str, status: str = "open",
                   actor_email: str | None = None):
    """Insert a feedback row directly so we can pre-stage rows in
    various status states.  Bypasses the widget POST."""
    email = actor_email or client.test_email
    with client.app_module.db() as conn:
        cur = conn.execute(
            "INSERT INTO feedback (tenant_id, actor_email, body, status) "
            "VALUES (?, ?, ?, ?)",
            (client.test_tenant_id, email, body, status),
        )
        conn.commit()
        return cur.lastrowid


# ── DAO ────────────────────────────────────────────────────────────


def test_stars_for_actor_counts_only_done(client):
    """Only ``status='done'`` feedback grants a star.  Open,
    accepted, and rejected items don't count."""
    from dao import feedback as dao_feedback
    me = client.test_email
    _seed_feedback(client, body="open",     status="open",     actor_email=me)
    _seed_feedback(client, body="accepted", status="accepted", actor_email=me)
    _seed_feedback(client, body="rejected", status="rejected", actor_email=me)
    _seed_feedback(client, body="done1",    status="done",     actor_email=me)
    _seed_feedback(client, body="done2",    status="done",     actor_email=me)
    assert dao_feedback.stars_for_actor(me) == 2


def test_stars_for_unknown_actor_is_zero(client):
    """Empty / unknown email returns 0 stars without raising."""
    from dao import feedback as dao_feedback
    assert dao_feedback.stars_for_actor("") == 0
    assert dao_feedback.stars_for_actor("nobody@example.com") == 0


def test_leaderboard_ranks_by_done_count_desc(client):
    """The top of the leaderboard is whoever has the most
    shipped items.  Ties break alphabetically by email for
    determinism."""
    from dao import feedback as dao_feedback
    for body in ("a", "b", "c"):
        _seed_feedback(
            client, body=body, status="done",
            actor_email="prolific@example.com",
        )
    for body in ("a", "b"):
        _seed_feedback(
            client, body=body, status="done",
            actor_email="silver@example.com",
        )
    _seed_feedback(
        client, body="one", status="done",
        actor_email="bronze@example.com",
    )
    # Open / rejected rows for these emails should NOT count.
    _seed_feedback(
        client, body="open", status="open",
        actor_email="prolific@example.com",
    )
    rows = dao_feedback.leaderboard(limit=3)
    assert [r["actor_email"] for r in rows] == [
        "prolific@example.com",
        "silver@example.com",
        "bronze@example.com",
    ]
    assert [r["stars"] for r in rows] == [3, 2, 1]


def test_leaderboard_excludes_listed_emails(client):
    """The operator email lives on the ignore list so they don't
    trophy themselves on their own platform."""
    from dao import feedback as dao_feedback
    for _ in range(5):
        _seed_feedback(
            client, body="op shipped this", status="done",
            actor_email="op@example.com",
        )
    _seed_feedback(
        client, body="a friend's bug", status="done",
        actor_email="friend@example.com",
    )
    rows = dao_feedback.leaderboard(
        exclude_emails=("op@example.com",), limit=3,
    )
    assert [r["actor_email"] for r in rows] == ["friend@example.com"]


def test_list_for_actor_returns_all_statuses_newest_first(client):
    """``list_for_actor`` is the "what did I send in?" view —
    every status, every row, newest first."""
    from dao import feedback as dao_feedback
    me = client.test_email
    for body, status in (
        ("first",  "done"),
        ("second", "rejected"),
        ("third",  "accepted"),
        ("fourth", "open"),
    ):
        _seed_feedback(client, body=body, status=status, actor_email=me)
    rows = dao_feedback.list_for_actor(me)
    bodies = [r["body"] for r in rows]
    # Newest-first.
    assert bodies == ["fourth", "third", "second", "first"]


# ── Route ───────────────────────────────────────────────────────────


def test_leaderboard_page_renders_for_authed_user(client):
    """/leaderboard is reachable from any signed-in tenant member.
    Page lists the top contributors + the viewer's own star
    count."""
    _seed_feedback(
        client, body="x", status="done",
        actor_email="contrib@example.com",
    )
    r = client.get("/leaderboard")
    assert r.status_code == 200
    assert "Leaderboard" in r.text or "🌟" in r.text
    # Top contributor email's local-part shows up in the podium.
    assert "contrib" in r.text


def test_star_tier_table_has_distinct_titles_per_threshold(client):
    """The per-viewer celebration copy on /leaderboard fans out
    over 20+ tiers (feedback #36).  Pin a sampling at known
    thresholds so a future "let me trim some" PR trips."""
    from app import _star_tier
    # Bench (0).
    assert "bench" in _star_tier(0)["title"].lower()
    # First star — singular grammar matters.
    assert "1 star" in _star_tier(1)["title"]
    assert "first" in _star_tier(1)["title"].lower()
    # Mid-range tiers each render their own message; titles
    # are distinct (no two adjacent counts collapse to the
    # same copy).
    titles = {_star_tier(n)["title"] for n in (
        0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 15, 20, 25, 30, 50, 75, 100, 150,
    )}
    assert len(titles) >= 18, (
        "expected each milestone threshold to surface a distinct title"
    )
    # The {n}-templated tiers interpolate the actual count, not
    # the threshold value.
    assert "47 stars" in _star_tier(47)["title"]   # falls in the 30-tier bucket
    assert "201 stars" in _star_tier(201)["title"] # 150+ bucket


def test_leaderboard_renders_tier_copy_inline(client):
    """The /leaderboard page actually renders the tier-specific
    title + body for the viewer's current count."""
    from dao import feedback as dao_feedback
    me = client.test_email
    # Seed a single shipped feedback to land in the "1 star" tier.
    for _ in range(1):
        with client.app_module.db() as conn:
            conn.execute(
                "INSERT INTO feedback (tenant_id, actor_email, body, status) "
                "VALUES (?, ?, 'thanks', 'done')",
                (client.test_tenant_id, me),
            )
            conn.commit()
    page = client.get("/leaderboard").text
    assert "1 star" in page
    assert "first one" in page.lower()


def test_usage_renders_your_contributions_card(client):
    """``/usage#contributions`` shows the user's star count + a
    list of their feedback submissions with status badges."""
    me = client.test_email
    _seed_feedback(client, body="shipped fix", status="done", actor_email=me)
    _seed_feedback(client, body="open thing",  status="open", actor_email=me)
    page = client.get("/usage").text
    assert 'id="contributions"' in page
    assert "shipped fix" in page
    assert "open thing" in page
    # Star count surface text.
    assert "1 star" in page or "stars" in page
