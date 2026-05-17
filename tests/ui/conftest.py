"""Test infrastructure for Playwright-driven UI regression tests.

The rest of the suite uses FastAPI's TestClient (in-process, no real
HTTP).  That catches Python bugs but can't see the bugs JavaScript and
CSS produce — feedback #37 / #41 / #46 lived in the CSS specificity
of a single rule and survived three rounds of "fix attempts" because
nothing in the tree ever rendered the page in a real browser.

This conftest plugs that gap: a session-scoped fixture spawns
``uvicorn app:app`` in a subprocess against a temp DB, exposes the
base URL, and tears down at the end.  Tests get the standard
``page`` / ``browser_context`` fixtures from ``pytest-playwright``
plus opt-in seed fixtures that stamp test data straight into the
running server's SQLite via the canonical schema.

Install with::

    pip install -r requirements-dev.txt
    playwright install chromium

Skip locally if you only need the headless tests:
``pytest -m "not ui"`` (every test in this directory is auto-marked).
"""
from __future__ import annotations

import base64
import io
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


# ── Project root (so the subprocess can import app.py) ──────────────


ROOT = Path(__file__).resolve().parent.parent.parent


# ── pytest auto-mark ────────────────────────────────────────────────


def pytest_collection_modifyitems(config, items):
    """Auto-mark every test in this directory ``ui`` so a fast-iteration
    developer can ``pytest -m "not ui"`` to skip the browser-driven
    pass without touching individual test files."""
    here = Path(__file__).resolve().parent
    for item in items:
        try:
            item_dir = Path(item.fspath).resolve().parent
        except Exception:  # noqa: BLE001
            continue
        if here in item_dir.parents or item_dir == here:
            item.add_marker(pytest.mark.ui)


def _free_port() -> int:
    """Pick a free TCP port by binding to 0 and reading back what the
    kernel assigned.  Race-prone in theory; in practice the window
    between releasing the port and uvicorn binding it is sub-second
    and CI machines aren't competing for it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── Live server fixture ─────────────────────────────────────────────


@pytest.fixture(scope="session")
def live_server(tmp_path_factory) -> dict:
    """Spawn uvicorn against a temp DB; yield ``{url, db_path}``.

    Session-scoped — one server boot for the whole UI run.  Tests
    share the DB; if isolation matters, name your seeded rows with a
    test-local prefix or wipe the relevant tables in a setup hook."""
    tmp = tmp_path_factory.mktemp("ui-stash")
    db_path = tmp / "stash.db"
    uploads = tmp / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    port = _free_port()

    env = {
        **os.environ,
        "STASH_DB": str(db_path),
        "STASH_UPLOADS": str(uploads),
        # Per-tenant DEK is wrapped by KEK — generate a fresh one
        # for this session so we don't accidentally inherit a real
        # operator KEK from the user's shell.
        "STASH_KEK": base64.b64encode(secrets.token_bytes(32)).decode(),
        # Local-loopback uvicorn never serves HTTPS — disable the
        # bearer-over-HTTPS guard.
        "STASH_REQUIRE_HTTPS_TOKENS": "false",
        # The UI test session actor is BOTH a tenant maintainer
        # AND an operator.  Most UI tests touch the customer-side
        # surface (the maintainer role is what they need); the
        # operator flag is additive — it doesn't change customer
        # routes, just unlocks /admin so the admin-layering tests
        # can render.  Tests that want a strictly-non-operator
        # actor can spin up a separate context with a different
        # X-Forwarded-Email value.
        "STASH_OPERATOR_EMAILS": TEST_EMAIL,
        # Quieter uvicorn logs so a failing UI test's pytest output
        # isn't drowned in request lines.
        "STASH_LOG_LEVEL": "WARNING",
        # Tests don't talk to Gemini; the AI surfaces still need a
        # key string to import cleanly without raising.
        "GEMINI_API_KEY": "fake-test-key",
    }

    proc = subprocess.Popen(  # noqa: S603
        [
            sys.executable, "-m", "uvicorn", "app:app",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--log-level", "warning",
            "--no-access-log",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    base_url = f"http://127.0.0.1:{port}"
    # Poll a public route (no auth needed) until ready.  /about is
    # served by the auth-bypass list so it answers 200 without
    # X-Forwarded-Email.
    deadline = time.time() + 20.0
    last_err: BaseException | None = None
    while time.time() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
            pytest.fail(
                f"uvicorn exited prematurely with code {proc.returncode}\n"
                f"--- output ---\n{output}",
            )
        try:
            urllib.request.urlopen(f"{base_url}/about", timeout=1.0)
            break
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_err = exc
            time.sleep(0.25)
    else:
        proc.terminate()
        pytest.fail(f"uvicorn never accepted requests: {last_err}")

    try:
        yield {"url": base_url, "db_path": str(db_path), "tmp": str(tmp)}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ── Auth header injection ────────────────────────────────────────────


# Default test actor.  Mirrors the in-process suite's TEST_EMAIL so a
# single seed pattern works in both.
TEST_EMAIL = "ui-test@example.com"


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    """Stamp ``X-Forwarded-Email`` on every request from every page so
    the actor middleware resolves to the seeded tenant member.

    ``pytest-playwright`` calls this fixture once per session to build
    the kwargs passed to ``browser.new_context``.  We merge our header
    overrides on top of whatever the plugin gives us so flags like
    ``--browser firefox`` still flow through."""
    return {
        **browser_context_args,
        "extra_http_headers": {
            "X-Forwarded-Email": TEST_EMAIL,
        },
    }


# ── Seed helpers ────────────────────────────────────────────────────


def _db(live_server) -> sqlite3.Connection:
    """Open a direct SQLite connection to the live server's DB.
    Bypasses the app module entirely so seed fixtures don't have to
    worry about app's import-time env-var resolution."""
    conn = sqlite3.connect(live_server["db_path"])
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_tenant(live_server) -> int:
    """Create the default UI test tenant + member if not already
    present.  Idempotent — repeated calls return the same tenant_id.

    Also marks every onboarding tour as already-seen so a freshly-
    rendered page doesn't fire the welcome overlay and intercept
    the playwright click that the test is trying to perform.  Tour
    state is keyed by ``actor_email``; we stamp a high version so a
    future tour-version bump doesn't reset the marker."""
    with _db(live_server) as conn:
        row = conn.execute(
            "SELECT id FROM tenants WHERE name = 'UI Test' LIMIT 1"
        ).fetchone()
        if row is not None:
            tenant_id = int(row["id"])
        else:
            cur = conn.execute(
                "INSERT INTO tenants (name, plan) VALUES ('UI Test', 'pro')"
            )
            tenant_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO tenant_members "
                "(tenant_id, email, role, joined_at) "
                "VALUES (?, ?, 'maintainer', CURRENT_TIMESTAMP)",
                (tenant_id, TEST_EMAIL),
            )
            conn.commit()
        # Suppress onboarding tour overlays.  The tour module's
        # registry is the source of truth for feature ids — import
        # it lazily here (rather than at module top) so the conftest
        # can be imported in environments where dao_tours isn't on
        # the path.  Stamping a high version ensures a future bump
        # to a tour doesn't reset the marker mid-suite.
        sys.path.insert(0, str(ROOT))
        try:
            from dao import tours as dao_tours
            features = [t["feature"] for t in dao_tours.TOURS]
        finally:
            if sys.path and sys.path[0] == str(ROOT):
                sys.path.pop(0)
        for feature in features:
            conn.execute(
                "INSERT OR IGNORE INTO tour_seen "
                "(actor_email, feature, version, seen_at) "
                "VALUES (?, ?, 999, CURRENT_TIMESTAMP)",
                (TEST_EMAIL, feature),
            )
        conn.commit()
    return tenant_id


@pytest.fixture(scope="session")
def seeded_tenant(live_server) -> int:
    """Lightweight seed: tenant + maintainer member only.  Returns the
    tenant_id.  Use this for tests that hit routes which only need an
    authenticated actor (e.g. /home, /usage)."""
    return _ensure_tenant(live_server)


@pytest.fixture(scope="session")
def populated_admin(live_server, seeded_tenant) -> dict:
    """Heavier admin-side seed: the canonical UI Test tenant plus
    half a dozen extra tenants so the /admin page renders a
    realistic tenant card grid + outstanding-invite list + tokens
    table.  Catches the "renders fine on a blank page, breaks
    when there's data" regression class — feedback #64
    (mint-link popup behind tenant cards), #65 (handles table
    needs to scale), #63 (floor-settings popup behind floorplan).

    The test session actor is already promoted to operator at
    server boot (see ``STASH_OPERATOR_EMAILS`` in ``live_server``
    above).  This fixture's job is just to seed the data.
    Returns ``{tenant_id, extra_tenant_ids}``."""
    tenant_id = seeded_tenant
    extra_ids: list[int] = []
    with _db(live_server) as conn:
        # Multiple tenants so the .admin-tenant-grid has a real
        # row of cards beneath the section header.  Mix plans +
        # soft-delete state so the cards have visual variety
        # (and so soft-deleted cards' ``opacity: 0.7`` stacking
        # context is exercised).
        for i, (name, plan, deleted) in enumerate([
            ("Echo House", "pro", False),
            ("Foxtrot Flat", "free", False),
            ("Golf Garage", "pro", False),
            ("Hotel Hostel", "free", True),
            ("India Inn", "pro", False),
            ("Juliet Junction", "free", False),
        ]):
            row = conn.execute(
                "SELECT id FROM tenants WHERE name = ?", (name,),
            ).fetchone()
            if row:
                extra_ids.append(int(row["id"]))
                continue
            cur = conn.execute(
                "INSERT INTO tenants (name, plan, deleted_at) "
                "VALUES (?, ?, ?)",
                (name, plan,
                 "2026-04-01 00:00:00" if deleted else None),
            )
            extra_ids.append(int(cur.lastrowid))
        # Outstanding onboarding-link invite — exercises the
        # "open_bootstrap_invites" branch in the section header
        # so the second <details> renders alongside the mint
        # action.  Stacking-context torture for the mint-link
        # popup.
        existing_inv = conn.execute(
            "SELECT 1 FROM tenant_bootstrap_invites "
            "WHERE created_by_email = ? AND consumed_at IS NULL",
            (TEST_EMAIL,),
        ).fetchone()
        if existing_inv is None:
            conn.execute(
                "INSERT INTO tenant_bootstrap_invites "
                "(token, plan, role, created_by_email, "
                " created_at, expires_at) "
                "VALUES (?, 'free', 'maintainer', ?, "
                "        '2026-05-17 00:00:00', "
                "        '2026-06-17 00:00:00')",
                (secrets.token_urlsafe(32), TEST_EMAIL),
            )
        conn.commit()
    return {"tenant_id": tenant_id, "extra_tenant_ids": extra_ids}


@pytest.fixture(scope="session")
def populated_floorplan(live_server, seeded_tenant) -> dict:
    """Floorplan seeded with multiple rooms + boxes so the
    location page renders a real overlay, real box tiles, real
    sidebar.  Catches the "modal renders behind the floorplan
    image" regression class — feedback #63: "Replace floorplan
    and Delete this floor are showing up behind the floorplan
    image at all times".

    Distinct from ``seeded_floorplan`` (which has one room, one
    box) so existing tests stay deterministic.  Returns the new
    location's ids."""
    tenant_id = seeded_tenant
    with _db(live_server) as conn:
        existing = conn.execute(
            "SELECT id FROM locations "
            "WHERE tenant_id = ? AND name = 'Populated Home'",
            (tenant_id,),
        ).fetchone()
        if existing is not None:
            location_id = int(existing["id"])
        else:
            cur = conn.execute(
                "INSERT INTO locations (name, tenant_id) "
                "VALUES ('Populated Home', ?)",
                (tenant_id,),
            )
            location_id = int(cur.lastrowid)
            cur = conn.execute(
                "INSERT INTO floors "
                "(name, location_id, tenant_id, floorplan) "
                "VALUES ('Ground', ?, ?, 'fake-populated.png')",
                (location_id, tenant_id),
            )
            floor_id = int(cur.lastrowid)
            # Five rooms scattered across the floor.  Each has at
            # least one box.  The location.html template renders
            # room rects + box tiles + the floor-settings dialog
            # — and the boxes' photos / mosaic surfaces are what
            # surfaced the "popup behind image" stacking bugs.
            for i, (rn, x, y) in enumerate([
                ("Kitchen",      0.05, 0.10),
                ("Living Room",  0.30, 0.10),
                ("Bedroom",      0.60, 0.10),
                ("Garage",       0.05, 0.55),
                ("Basement",     0.40, 0.55),
            ]):
                rc = conn.execute(
                    "INSERT INTO rooms "
                    "(name, floor_id, location_id, "
                    " x, y, w, h, tenant_id) "
                    "VALUES (?, ?, ?, ?, ?, 0.22, 0.32, ?)",
                    (rn, floor_id, location_id, x, y, tenant_id),
                )
                room_id = int(rc.lastrowid)
                conn.execute(
                    "INSERT INTO boxes "
                    "(name, room_id, tenant_id, location) "
                    "VALUES (?, ?, ?, ?)",
                    (f"{rn} stash", room_id, tenant_id, rn),
                )
            conn.commit()
        floor_row = conn.execute(
            "SELECT id FROM floors WHERE location_id = ?",
            (location_id,),
        ).fetchone()
        floor_id = int(floor_row["id"])
    return {
        "tenant_id": tenant_id,
        "location_id": location_id,
        "floor_id": floor_id,
    }


@pytest.fixture(scope="session")
def seeded_floorplan(live_server, seeded_tenant) -> dict:
    """Heavyweight seed: tenant + location + floor (with floorplan
    image name set) + room + box.  Returns the row ids the test needs
    to construct URLs.  Idempotent at the row level — the seed creates
    rows once per session and reuses them on subsequent fixture calls.

    The floorplan ``image`` column holds a filename that ``/uploads/X``
    will 404 on (we don't actually encrypt + write the image — the
    UI test only cares that ``current_floor.floorplan`` is truthy so
    the floorplan-card branch in templates/location.html renders).
    The img tag's load failure doesn't interfere with the dialog
    state we're testing."""
    tenant_id = seeded_tenant
    with _db(live_server) as conn:
        existing = conn.execute(
            "SELECT l.id AS location_id, f.id AS floor_id, "
            "       r.id AS room_id, b.id AS box_id "
            "FROM locations l "
            "JOIN floors f ON f.location_id = l.id "
            "JOIN rooms r ON r.floor_id = f.id "
            "JOIN boxes b ON b.room_id = r.id "
            "WHERE l.tenant_id = ? AND l.name = 'UI Test Home' LIMIT 1",
            (tenant_id,),
        ).fetchone()
        if existing is not None:
            return dict(existing) | {"tenant_id": tenant_id}

        cur = conn.execute(
            "INSERT INTO locations (name, tenant_id) "
            "VALUES ('UI Test Home', ?)",
            (tenant_id,),
        )
        location_id = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO floors "
            "(name, location_id, tenant_id, floorplan) "
            "VALUES ('Ground', ?, ?, 'fake-floorplan.png')",
            (location_id, tenant_id),
        )
        floor_id = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO rooms "
            "(name, floor_id, location_id, x, y, w, h, tenant_id) "
            "VALUES ('Living Room', ?, ?, 0.1, 0.1, 0.4, 0.4, ?)",
            (floor_id, location_id, tenant_id),
        )
        room_id = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO boxes (name, room_id, tenant_id) "
            "VALUES ('UI Test Box', ?, ?)",
            (room_id, tenant_id),
        )
        box_id = int(cur.lastrowid)
        conn.commit()

    return {
        "tenant_id": tenant_id,
        "location_id": location_id,
        "floor_id": floor_id,
        "room_id": room_id,
        "box_id": box_id,
    }
