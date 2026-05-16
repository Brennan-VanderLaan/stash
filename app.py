import json
import os
import sqlite3
import base64
import secrets
from pathlib import Path
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

import labels
import vault
import vision
from dao import Actor, ConflictError, ForbiddenError, NotFoundError
from dao import api_tokens as dao_api_tokens
from dao import audit as dao_audit
from dao import backups as dao_backups
from dao import billing as dao_billing
from dao import boxes as dao_boxes
from dao import feedback as dao_feedback
from dao import floors as dao_floors
from dao import handles as dao_handles
from dao import ingest_jobs as dao_ingest_jobs
from dao import invites as dao_invites
from dao import items as dao_items
from dao import locations as dao_locations
from dao import oauth as dao_oauth
from dao import pending_items as dao_pending
from dao import quotas as dao_quotas
from dao import rooms as dao_rooms
from dao import shares as dao_shares
from dao import tags as dao_tags
from dao import tenants as dao_tenants
from dao import tours as dao_tours
from dao import usage as dao_usage
import obs

load_dotenv()
obs.setup_logging()

ROOT = Path(__file__).parent
DB_PATH = Path(os.environ.get("STASH_DB", ROOT / "stash.db"))
UPLOAD_DIR = Path(os.environ.get("STASH_UPLOADS", ROOT / "uploads"))
UPLOAD_DIR.mkdir(exist_ok=True)
(ROOT / "static").mkdir(exist_ok=True)
(ROOT / "templates").mkdir(exist_ok=True)

VERSION = os.environ.get("STASH_VERSION", "dev")
GIT_SHA = os.environ.get("STASH_GIT_SHA", "")
WATCHTOWER_URL = os.environ.get("WATCHTOWER_URL", "").rstrip("/")
WATCHTOWER_TOKEN = os.environ.get("WATCHTOWER_TOKEN", "")
# At-rest encryption key.  Loaded once at import so a misconfigured or
# missing key fails fast instead of corrupting on first write.  The KEK
# wraps every tenant's DEK; losing it is total data loss, so it lives
# in env (not the DB or the uploads directory) and gets backed up to a
# different bucket than the data.  See vault.py + spec § "Encryption
# at rest".
_KEK = vault.get_kek()
# Public-facing base URL for QR codes on printed labels. Set in production
# (deploy/.env via the compose stack) to e.g. https://stash.example.com.
# Empty in local dev — labels fall back to the `stash:box:N` custom scheme.
PUBLIC_URL = os.environ.get("STASH_PUBLIC_URL", "").rstrip("/")


def _load_changelog_html() -> str:
    """Render CHANGELOG.md to HTML. Cached at import time — only changes on container restart."""
    path = ROOT / "CHANGELOG.md"
    if not path.exists():
        return ""
    try:
        import markdown as _md
        return _md.markdown(path.read_text(), extensions=["fenced_code", "tables"])
    except Exception:
        # Fall back to escaped plain text so a render error never breaks the page.
        from html import escape
        return f"<pre>{escape(path.read_text())}</pre>"


CHANGELOG_HTML = _load_changelog_html()


def _trigger_watchtower_update() -> None:
    """POST to watchtower's HTTP API to force an immediate scan+update.
    Runs as a background task so the HTTP response returns before our own
    container is potentially restarted."""
    if not WATCHTOWER_URL:
        return
    import urllib.request
    req = urllib.request.Request(
        f"{WATCHTOWER_URL}/v1/update",
        method="POST",
        headers={"Authorization": f"Bearer {WATCHTOWER_TOKEN}"} if WATCHTOWER_TOKEN else {},
    )
    try:
        urllib.request.urlopen(req, timeout=120).read()
    except Exception:
        # The container can be killed mid-call once watchtower pulls a new image —
        # that's expected, not an error worth surfacing.
        pass

app = FastAPI()
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")


# ── Sassy error pages (404, 401, 429, …) ─────────────────────────────
#
# Browsers that navigate into a dead URL get a Siberian Forest cat or
# a wise tortoise scoffing at them; API clients (Accept: !text/html,
# /api/*, /mcp/*) keep the JSON ``{"detail": "..."}`` contract so
# nothing programmatic breaks.  Per-status copy lives in a dict so a
# new status code is one entry plus a quip — no new template work.
#
# Tone rules: sassy, never mean.  Lean into the cat-is-judging-you /
# turtle-is-unbothered split.  No emoji walls, no tech jargon in the
# user-visible quip — keep the apology short and the personality long.
_ERROR_COPY = {
    401: {
        "mascot": "cat",
        "headline": "Papers, please.",
        "quip": (
            "The cat does not recognize you.  Show valid credentials and "
            "she may, eventually, allow you back in.  Maybe."
        ),
        "signature": "the cat, unblinking",
    },
    403: {
        "mascot": "cat",
        "headline": "She doesn't think so.",
        "quip": (
            "Big fluffy paw across the doorway.  You may be a Person of "
            "Importance somewhere — but not here, not today."
        ),
        "signature": "the cat, with regrets (none, actually)",
    },
    404: {
        "mascot": "turtle",
        "headline": "You took a wrong turn at the pond.",
        "quip": (
            "Whatever you were looking for is not here.  Possibly never was.  "
            "The tortoise has been here for forty years and would have noticed."
        ),
        "signature": "the tortoise, contemplating a leaf",
    },
    405: {
        "mascot": "cat",
        "headline": "Wrong door.",
        "quip": (
            "That URL exists, but it does not answer to that knock.  "
            "Try the front entrance like everyone else."
        ),
        "signature": "the cat, unimpressed",
    },
    413: {
        "mascot": "cat",
        "headline": "That is a LOT of file.",
        "quip": (
            "Even the cat — who has personally knocked an entire dinner "
            "off the counter — thinks that's excessive.  Try a smaller upload."
        ),
        "signature": "the cat, raising one judgmental eyebrow",
    },
    422: {
        "mascot": "cat",
        "headline": "She read the form. She has notes.",
        "quip": (
            "Some required field is missing or off.  The cat is willing to "
            "wait while you fix it.  She has nowhere else to be."
        ),
        "signature": "the cat, tail twitching",
    },
    429: {
        "mascot": "turtle",
        "headline": "Slow your roll.",
        "quip": (
            "You're moving faster than the tortoise's worldview can tolerate.  "
            "Step away from the keyboard, look out a window, return refreshed."
        ),
        "signature": "the tortoise, who got there eventually",
    },
    500: {
        "mascot": "cat",
        "headline": "She knocked something off the shelf.",
        "quip": (
            "The cat made direct eye contact and pushed something important "
            "off the desk.  Engineers have been notified.  Try again in a moment."
        ),
        "signature": "the cat, deeply unsorry",
    },
    503: {
        "mascot": "turtle",
        "headline": "Currently napping.",
        "quip": (
            "The tortoise is taking a moment.  Service will resume when the "
            "tortoise feels like it.  Probably soon."
        ),
        "signature": "the tortoise, eyes closed, vibing",
    },
}

_DEFAULT_ERROR_COPY = {
    "mascot": "cat",
    "headline": "Something fluffy has gone wrong.",
    "quip": (
        "The cat blames the turtle.  The turtle blames the cat.  "
        "Engineers are mediating."
    ),
    "signature": "house management",
}


def _wants_html(request: Request) -> bool:
    """Decide whether to render HTML or fall back to JSON.

    JSON wins for: /api/*, /mcp/*, ``Accept: application/json`` (no
    HTML accepted), and explicit ``X-Requested-With: XMLHttpRequest``.
    Everything else (browser nav, HTMX, plain ``<a>`` clicks) gets
    the sassy HTML page."""
    path = request.url.path
    if path.startswith("/api/") or path.startswith("/mcp"):
        return False
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return False
    accept = request.headers.get("accept", "") or ""
    if "text/html" in accept:
        return True
    if "application/json" in accept:
        return False
    # Browsers send ``*/*`` on direct navigation; treat that as HTML.
    return True


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    status = exc.status_code
    copy = _ERROR_COPY.get(status, _DEFAULT_ERROR_COPY)
    if not _wants_html(request):
        return JSONResponse(
            status_code=status,
            content={"detail": exc.detail or copy["headline"]},
            headers=getattr(exc, "headers", None) or {},
        )
    referer = request.headers.get("referer", "")
    detail = None
    # We surface ``exc.detail`` only when it's a string and not the
    # default Starlette message — those defaults ("Not Found", etc.)
    # would clash with our headline.  Custom raise-site messages
    # (``HTTPException(429, "AI quota exceeded …")``) are exactly the
    # kind of thing the user wants to see.
    raw = exc.detail
    if isinstance(raw, str) and raw and raw.lower() not in (
        "not found", "unauthorized", "forbidden", "method not allowed",
        "internal server error", "service unavailable",
    ):
        detail = raw
    response = templates.TemplateResponse(
        request, "error.html",
        {
            "status": status,
            "headline": copy["headline"],
            "quip": copy["quip"],
            "mascot": copy["mascot"],
            "signature": copy.get("signature", ""),
            "detail": detail,
            "back_url": referer,
        },
        status_code=status,
        headers=getattr(exc, "headers", None) or {},
    )
    return response

# /api/v1 — bearer-auth JSON API (phase 11).  Routes live in api.py
# so the surface stays self-contained for the eventual MCP server +
# any future agent wrappers.  Bearer auth runs in the global
# current_actor middleware so every handler sees a populated
# request.state.actor by the time it fires.
import api as _api_module  # noqa: E402  (must follow `app = FastAPI()`)
app.include_router(_api_module.router)

# /mcp — Model Context Protocol Streamable HTTP endpoint (phase 18).
# Implements spec rev 2025-11-25.  Bearer auth piggybacks on the
# same current_actor path as /api/v1.  Surface contract is in
# spec.md § Architecture · Agent / MCP integration.
import mcp_server as _mcp_server  # noqa: E402

# Public alias so tests can override at module level if needed.
_MCP_ENABLED = os.environ.get(
    "STASH_MCP_ENABLED", "true",
).strip().lower() not in ("false", "0", "no", "off")


# ── Localization seams ───────────────────────────────────────────────
# v1 ships English-only.  The seams land now (jinja2.ext.i18n + a
# NullTranslations identity catalog + a babel-driven date filter) so
# wrapping a string in `_()` or `{% trans %}` is a no-op today and
# becomes a translation task tomorrow.  Adding a real locale is a PR
# that drops a `.po` file under `locale/<lang>/LC_MESSAGES/messages.po`
# plus a one-line registration here; nothing else in the codebase
# changes.  See spec § "Localization".
import gettext as _gettext
from babel.dates import format_datetime as _babel_format_datetime
from babel.dates import format_date as _babel_format_date

# Active translations object.  NullTranslations passes source strings
# through unchanged — perfect for an English-only deployment that's
# wrapping strings for future-proofing.  When a `.po` file lands, swap
# this for `gettext.translation(...)`.
_translations = _gettext.NullTranslations()
_DEFAULT_LOCALE = os.environ.get("STASH_DEFAULT_LOCALE", "en")

templates.env.add_extension("jinja2.ext.i18n")
templates.env.install_gettext_translations(_translations, newstyle=True)


def _(message: str) -> str:
    """Mark a string for translation. v1 is English-only so this is the
    identity, but every user-visible string in Python code should still
    flow through here so `pybabel extract` finds them later."""
    return _translations.gettext(message)


def _format_datetime(dt, fmt: str = "medium", locale: str | None = None) -> str:
    """Locale-aware datetime formatter for templates. Accepts strings (ISO),
    datetimes, or None (returns empty)."""
    if dt is None or dt == "":
        return ""
    if isinstance(dt, str):
        from datetime import datetime
        try:
            dt = datetime.fromisoformat(dt.replace(" ", "T"))
        except ValueError:
            return dt  # render as-is if we can't parse
    return _babel_format_datetime(dt, format=fmt, locale=locale or _DEFAULT_LOCALE)


def _format_date(dt, fmt: str = "medium", locale: str | None = None) -> str:
    if dt is None or dt == "":
        return ""
    if isinstance(dt, str):
        from datetime import datetime
        try:
            dt = datetime.fromisoformat(dt.replace(" ", "T"))
        except ValueError:
            return dt
    return _babel_format_date(dt, format=fmt, locale=locale or _DEFAULT_LOCALE)


templates.env.filters["datetime"] = _format_datetime
templates.env.filters["date"] = _format_date


# ── Actor middleware ─────────────────────────────────────────────────
# Resolves X-Forwarded-Email (set by oauth2-proxy) into a `request.state.actor`
# carrying the active tenant + role + operator flag.  Replaces the old
# STASH_ALLOWED_EMAILS / FULLY_PUBLIC pair: the new gate is tenant_members.
# An email with no membership and no operator status gets a 403; everything
# downstream (routes, DAO once it lands) reads the actor off request.state.
#
# Operator emails (STASH_OPERATOR_EMAILS) get is_operator=True regardless
# of tenant membership.  /admin routes (lifecycle, metadata, vendor cost
# panel — see spec § "Operator surface") gate on this flag; tenant-data
# routes do NOT — operators access tenant data only via an explicit
# maintainer invite from the user, by design.
_OPERATOR_EMAILS = frozenset(
    e.strip().lower()
    for e in os.environ.get("STASH_OPERATOR_EMAILS", "").split(",")
    if e.strip()
)
# Leaderboard ignore list — emails kept off the /leaderboard
# rankings.  Operator typically wants to be excluded (so they
# don't trophy themselves on their own platform); other names a
# deploy might want hidden (bots, test accounts) can be listed
# here too.  Defaults to the operator set when unset so the
# common case Just Works.
_LEADERBOARD_IGNORE_EMAILS = frozenset(
    e.strip().lower()
    for e in os.environ.get(
        "STASH_LEADERBOARD_IGNORE_EMAILS",
        os.environ.get("STASH_OPERATOR_EMAILS", ""),
    ).split(",")
    if e.strip()
)
# Actor lives in dao/_base.py so DAO methods can take it without a
# circular dao→app import.  Imported at the top of this module.


def _invite_token_in_path(path: str) -> str | None:
    """Pull the token segment out of ``/invite/<token>`` and
    ``/invite/<token>/accept``.  Returns None for any other path so
    the middleware bypass only fires on the redemption surface —
    not e.g. ``/api/invite/...`` or a future
    ``/invite/<token>/<extra>`` route.

    Tight match: ``parts == ["invite", token]`` or
    ``parts == ["invite", token, "accept"]``.  Anything else falls
    through to the auth wall.  Token charset is limited to the
    url-safe alphabet ``secrets.token_urlsafe`` produces."""
    parts = path.strip("/").split("/")
    if len(parts) not in (2, 3):
        return None
    if parts[0] != "invite":
        return None
    if len(parts) == 3 and parts[2] != "accept":
        return None
    token = parts[1]
    if not token:
        return None
    if not all(c.isalnum() or c in "-_" for c in token):
        return None
    return token


_LOG_ROUTE = obs.get_logger("route")


# Default: trust X-Forwarded-Proto (we ship behind Caddy, which
# strips inbound copies + sets the real one).  Set this env var
# to "false" in dev / for stand-alone deploys without a proxy.
_REQUIRE_HTTPS_TOKENS = os.environ.get(
    "STASH_REQUIRE_HTTPS_TOKENS", "true",
).strip().lower() not in ("false", "0", "no", "off")


# Token-leak scanner pattern.  ``stash_<43 url-safe chars>`` is the
# exact shape ``secrets.token_urlsafe(32)`` produces; the regex is
# slightly lenient (40-50 chars) so a future format tweak doesn't
# silently disable the guard.
import re as _re
_TOKEN_LEAK_PATTERN = _re.compile(r"stash_[A-Za-z0-9_\-]{40,50}")


def _scan_request_for_token_leak(request: Request) -> tuple[str, str] | None:
    """Look for a stash_-prefixed token plaintext in places it has
    no business being.

    Returns ``(plaintext, where)`` if found — ``where`` is one of
    ``"url"``, ``"header"`` (and the header name is logged from
    the caller).  None when nothing leaks.

    Scanned surfaces:
    * URL query string — clients sometimes mistakenly do
      ``?token=stash_xxx`` and then the URL ends up in proxy
      logs, browser history, etc.
    * Non-Authorization headers — anything in another header.
      (The Authorization header is the *only* legitimate place;
      tokens elsewhere are a misuse signal.)
    * Body scanning is deliberately omitted — too hot of a path
      to parse on every request, and cookie/POST-body leaks are
      rarer than URL/header ones.
    """
    qs = request.url.query
    if "stash_" in qs:
        m = _TOKEN_LEAK_PATTERN.search(qs)
        if m:
            return m.group(0), "url"
    for k, v in request.headers.items():
        if k.lower() == "authorization":
            continue
        if "stash_" in v:
            m = _TOKEN_LEAK_PATTERN.search(v)
            if m:
                return m.group(0), f"header:{k.lower()}"
    return None


# Defense-in-depth response headers.  Caddy sets some of these at
# the edge in production deploys, but stamping them in the app too
# means the protection holds for any future deployment topology
# (LB without security headers, k8s ingress, etc.) without
# remembering to copy the directives across.
_SECURITY_HEADERS = {
    # Refuse to render a stash response inside an iframe — kills
    # clickjacking on the few state-mutating GET-shaped flows.
    "X-Frame-Options": "DENY",
    # Don't let browsers sniff /uploads/<name> as text/html when it
    # claims image/jpeg.  Pairs with the explicit ``image/jpeg``
    # media_type in serve_upload to keep stored-XSS shut.
    "X-Content-Type-Options": "nosniff",
    # Trim the Referer to the origin only on cross-origin nav.
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # A baseline CSP — locks resource loads to same-origin (templates
    # don't pull from CDNs), drops object/embed/applet, explicitly
    # forbids iframes (X-Frame-Options is the modern-browser path;
    # this is the older-browser fallback), and pins form submissions
    # to the same origin so a stolen form action can't redirect a
    # POST off-site.  Inline scripts are allowed because the existing
    # templates have small snippets; tightening to nonces lands when
    # there's bandwidth.
    "Content-Security-Policy": (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "frame-src 'none'; "
        "object-src 'none'; "
        "base-uri 'self'"
    ),
    # Lock the platform-level surfaces we never use.  An XSS that
    # tried ``navigator.geolocation.getCurrentPosition`` or popped
    # up a microphone prompt would be silently denied.  FLoC /
    # interest-cohort opts out of Google's old behavioural-ad pool.
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(), "
        "interest-cohort=(), payment=(), usb=(), bluetooth=(), "
        "magnetometer=(), gyroscope=(), accelerometer=()"
    ),
    # Cross-Origin-Opener-Policy: same-origin isolates the browsing
    # context so a popup opened from a malicious page can't peer
    # into ``window.opener`` and probe stash state.  Pairs with
    # CORP below to prevent cross-site image embedding.
    "Cross-Origin-Opener-Policy": "same-origin",
    # CORP refuses cross-origin loads of /uploads/{name} etc. — a
    # rogue site cannot embed a tenant's photo via <img src=...>
    # to confirm it exists.  All in-app references are same-origin
    # so this is invisible to legitimate use.
    "Cross-Origin-Resource-Policy": "same-origin",
}


def _is_https_request(request: Request) -> bool:
    """True iff the request reached us over HTTPS (directly or via
    a proxy that set X-Forwarded-Proto)."""
    scheme = (
        request.headers.get("X-Forwarded-Proto")
        or request.url.scheme
        or ""
    ).strip().lower()
    return scheme == "https"


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for k, v in _SECURITY_HEADERS.items():
        # Don't clobber a header a route deliberately set.
        response.headers.setdefault(k, v)
    # HSTS only on HTTPS — sending it over plaintext would be a
    # spec violation and breaks local dev on ``http://testserver``.
    # Production also gets this at the edge via Caddy; this is the
    # defense-in-depth copy.
    if _is_https_request(request):
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains; preload",
        )
    return response


# Paths that bypass current_actor entirely.  These either don't
# need identity (healthz, OAuth discovery) or are pure
# server-to-server endpoints that authenticate themselves
# (/oauth/token via client_secret + PKCE; /oauth/register is
# DCR which is intentionally public per RFC 7591).  Anything
# outside this set hits the auth wall.
#
# Note for production deploys behind oauth2-proxy: these paths
# also need to be listed in OAUTH2_PROXY_SKIP_AUTH_ROUTES so the
# proxy forwards the request to stash without a Google session
# cookie check.  See deploy/docker-compose.yml + .env.example.
_AUTH_BYPASS_EXACT = frozenset((
    "/healthz",
    "/.well-known/oauth-authorization-server",
    "/oauth/token",
    "/oauth/register",
    # ``/`` renders a public marketing landing for unauthenticated
    # visitors.  The authenticated dashboard moved to ``/home``.
    # See public_landing() + index() in this file.
    "/",
))

# Prefix-based bypass: RFC 9728 protected-resource metadata can
# live at the root or path-suffixed form (e.g.
# ``/.well-known/oauth-protected-resource/mcp``); both have to
# pass the wall.  Static /about/* pages also bypass because
# Stripe (and any KYC-grade financial partner) requires the
# business name, description, contact, refund + cancellation
# policy, and sub-processor list to be publicly reachable
# without a login.  ``/static/`` bypasses too — these are
# stash's own CSS/JS/vendor assets, no tenant data; without
# the bypass the public /about pages render unstyled because
# the browser sees Google's login redirect instead of CSS.
# Tightly scoped to deliberate prefixes so adding a new bypass
# surface remains a code change.
_AUTH_BYPASS_PREFIXES = (
    "/.well-known/oauth-protected-resource",
    "/about/",
    "/about",
    "/static/",
)


def _path_bypasses_auth(path: str) -> bool:
    if path in _AUTH_BYPASS_EXACT:
        return True
    return any(path.startswith(p) for p in _AUTH_BYPASS_PREFIXES)


# Backwards-compatible alias preserved for the auth-coverage
# pinning test in tests/test_auth_coverage.py.  Kept as a
# frozenset so the test's set-equality assertion remains
# meaningful even with the new prefix surface.
_AUTH_BYPASS_PATHS = frozenset(
    list(_AUTH_BYPASS_EXACT) + list(_AUTH_BYPASS_PREFIXES)
)


@app.middleware("http")
async def current_actor(request: Request, call_next):
    # Defensive scan: if a stash-shaped token appears in the URL
    # query string or any header *other* than Authorization, treat
    # it as a leak — auto-revoke and 401 regardless of whether
    # auth would otherwise succeed.  Runs BEFORE the bypass check
    # so that a leak in ``/?token=...`` or ``/about/foo?token=...``
    # is still caught even though those paths skip the rest of the
    # auth wall.  A token in the URL is always a misuse signal.
    leak = _scan_request_for_token_leak(request)
    if leak is not None:
        leaked_plaintext, where = leak
        token_row = dao_api_tokens.lookup_by_plaintext(leaked_plaintext)
        if token_row is not None and token_row["revoked_at"] is None:
            reason = "leaked_in_url" if where == "url" else "leaked_in_header"
            dao_api_tokens.revoke_for_leak(
                token_row["id"], reason,
                request_path=str(request.url),
            )
        _LOG_ROUTE.error(
            "auth.token_leak where=%s path=%s known=%s",
            where, request.url.path, token_row is not None,
        )
        return Response(
            "Unauthorized — a stash bearer token was detected in the "
            "request URL or a non-Authorization header and has been "
            "revoked.  Mint a fresh token from /usage and send it via "
            "the Authorization header only.",
            status_code=401,
            media_type="text/plain",
        )

    # Healthcheck + a tiny set of unauthenticated probes bypass the
    # auth wall entirely.  Container HEALTHCHECKs and external
    # probes hit /healthz without any identity headers; the route's
    # response carries no tenant data, so a bypass is safe.
    if _path_bypasses_auth(request.url.path):
        return await call_next(request)

    # Stamp a fresh request_id first so every log line emitted under
    # this request — including the 403 + invite-bypass paths below —
    # carries it.  Trust an inbound X-Request-Id when present (lets
    # an upstream proxy correlate access logs with our records), but
    # cap length so a hostile header can't bloat memory.
    incoming = (request.headers.get("X-Request-Id") or "").strip()[:64]
    request_id = incoming if incoming else obs.new_request_id()
    request.state.request_id = request_id
    rid_token = obs.bind_request_id(request_id)

    import time as _time
    started = _time.monotonic()

    # Bearer auth (phase 11): if Authorization: Bearer <token> is
    # present, resolve via the api_tokens DAO and short-circuit the
    # X-Forwarded-Email path.  Tokens carry a tenant_id + role,
    # nothing else — no operator flag, no memberships beyond the
    # token's own tenant.  Routes that need user-specific identity
    # (audit-log actor_email) will see ``api_token:<id>`` instead.
    auth_header = (request.headers.get("Authorization") or "").strip()
    if auth_header.lower().startswith("bearer "):
        token_plaintext = auth_header[7:].strip()

        # HTTPS gate: if the request didn't reach us over HTTPS,
        # the bearer is travelling in cleartext (or has been at some
        # earlier hop).  Auto-revoke + 401 with the reason logged.
        # Behind Caddy, ``X-Forwarded-Proto`` carries the original
        # client scheme; for stand-alone deploys without a proxy,
        # ``request.url.scheme`` is the truth.  Operators on dev /
        # local can disable via STASH_REQUIRE_HTTPS_TOKENS=false.
        if _REQUIRE_HTTPS_TOKENS:
            forwarded_proto = (
                request.headers.get("X-Forwarded-Proto")
                or request.url.scheme or ""
            ).strip().lower()
            if forwarded_proto != "https":
                token_row = dao_api_tokens.lookup_by_plaintext(token_plaintext)
                if token_row is not None and token_row["revoked_at"] is None:
                    dao_api_tokens.revoke_for_leak(
                        token_row["id"], "seen_over_http",
                        request_path=str(request.url),
                    )
                _LOG_ROUTE.error(
                    "auth.bearer_over_http forwarded_proto=%r path=%s",
                    forwarded_proto, request.url.path,
                )
                return Response(
                    "Unauthorized — bearer tokens require HTTPS.  "
                    "The presented token has been revoked because it "
                    "was sent over plaintext HTTP.  Mint a fresh "
                    "token from /usage and use it on the HTTPS endpoint.",
                    status_code=401,
                    media_type="text/plain",
                )

        # Audience binding for OAuth-issued tokens: the canonical
        # /mcp resource on this deployment is the only audience an
        # MCP-flow token is valid for.  Legacy user-minted tokens
        # have NULL audience and pass any path.
        path = request.url.path
        expected_audience = None
        if path.startswith("/mcp"):
            base = (PUBLIC_URL or
                    f"{request.url.scheme}://{request.url.netloc}").rstrip("/")
            expected_audience = f"{base}/mcp"

        token_row = dao_api_tokens.authenticate(
            token_plaintext, expected_audience=expected_audience,
        )
        if token_row is None:
            _LOG_ROUTE.warning(
                "auth.bearer_invalid path=%s", request.url.path,
            )
            # Spec § §"Authorization Server Discovery": an MCP
            # client that gets a 401 with WWW-Authenticate carrying
            # ``resource_metadata`` discovers the AS automatically.
            # Stamp the header on /mcp 401s so claude.ai-style
            # clients bootstrap into the OAuth flow without any
            # operator hand-holding.
            headers = {}
            if path.startswith("/mcp"):
                base = (PUBLIC_URL or
                        f"{request.url.scheme}://{request.url.netloc}"
                        ).rstrip("/")
                headers["WWW-Authenticate"] = (
                    f'Bearer resource_metadata='
                    f'"{base}/.well-known/oauth-protected-resource", '
                    f'scope="mcp"'
                )
            return Response(
                "Unauthorized — bearer token unknown, revoked, "
                "expired, or for a different audience.",
                status_code=401,
                media_type="text/plain",
                headers=headers,
            )
        actor_email = f"api_token:{token_row['id']}"
        # Honour operator status on bearer-auth too: a token minted
        # by an operator email (see _OPERATOR_EMAILS) carries that
        # operator scope on every request.  This is what lets the
        # operator-MCP tools work from an MCP client that authn's
        # with the same api_token surface as the tenant tools.
        creator = (token_row.get("created_by_email") or "").strip().lower()
        token_is_operator = bool(creator) and creator in _OPERATOR_EMAILS
        request.state.actor = Actor(
            email=actor_email,
            tenant_id=token_row["tenant_id"],
            role=token_row["role"],
            is_operator=token_is_operator,
            memberships=((token_row["tenant_id"], token_row["role"]),),
            shares=(),
        )
        actor_tokens = obs.bind_actor(actor_email, token_row["tenant_id"])
        response: Response | None = None
        try:
            response = await call_next(request)
            return response
        finally:
            if response is not None:
                response.headers["X-Request-Id"] = request_id
            duration_ms = int((_time.monotonic() - started) * 1000)
            status = response.status_code if response is not None else 500
            _LOG_ROUTE.info(
                "%s %s -> %d in %dms (api_token=%s)",
                request.method, request.url.path, status, duration_ms,
                token_row["id"],
            )
            obs.reset_tokens(*actor_tokens, rid_token)

    email = (request.headers.get("X-Forwarded-Email") or "").strip().lower()
    is_operator = bool(email) and email in _OPERATOR_EMAILS

    memberships = dao_tenants.memberships_for_email(email) if email else ()
    shares = dao_shares.shares_for_email(email) if email else ()

    actor_tokens: tuple = ()
    response: Response | None = None
    try:
        # Invite-bypass: a signed-in email with no current membership
        # is still allowed onto /invite/<token> so they can land on
        # the redemption page.  The redemption itself only succeeds
        # if the token is valid + un-consumed (DAO enforces).  Spec §
        # "Sign-up + onboarding" path #2 — "sign-in following an
        # invite link".
        if not memberships and not is_operator and email and not shares:
            token = _invite_token_in_path(request.url.path)
            if token and dao_invites.get_by_token(token) is not None:
                request.state.actor = Actor(
                    email=email, tenant_id=None, role=None,
                    is_operator=False, memberships=(), shares=(),
                )
                actor_tokens = obs.bind_actor(email, None)
                _LOG_ROUTE.info(
                    "auth.invite_bypass path=%s", request.url.path,
                )
                response = await call_next(request)
                return response

        # Share-only access: an email with no membership but at least
        # one active share is allowed in.  Their actor has tenant_id
        # + role None; the routes that handle share-target pages
        # consult ``shares`` directly via the DAO's effective-role
        # helpers.
        if not memberships and not is_operator and not shares:
            _LOG_ROUTE.warning(
                "auth.denied email=%r path=%s",
                email or "<missing>", request.url.path,
            )
            # If the request looks like a browser (HTML in Accept),
            # serve the friendly no_tenant.html page that explains
            # what happened + how to redeem an invite.  This used
            # to be a plain-text "Forbidden" response; the friendly
            # version matters more now that oauth2-proxy lets every
            # Google account through and this page is what randos
            # see if they happen to sign in.  JSON / API callers
            # keep the terse text response so they have a clean
            # error message to relay.
            accept = (request.headers.get("accept") or "").lower()
            if email and "text/html" in accept:
                try:
                    response = templates.TemplateResponse(
                        request, "no_tenant.html",
                        {
                            "email": email,
                            "business_name": _public_business_name(),
                            "contact_email": _public_contact_email(),
                            "public_url": PUBLIC_URL,
                        },
                        status_code=403,
                    )
                    return response
                except Exception:
                    # Template-render failure must not lock the
                    # user out of the explanation; fall through
                    # to the plain-text response so the 403 still
                    # ships SOMETHING they can read.
                    pass
            response = Response(
                "Forbidden — your email is not a member of any "
                "tenant on this stash.",
                status_code=403,
                media_type="text/plain",
            )
            return response

        # Tenant-switcher cookie: respect the user's preferred
        # active tenant if the cookie's tenant_id is one they're
        # genuinely a member of.  Falls back to memberships[0]
        # silently when missing/invalid so a stale cookie can't
        # lock anyone out — the worst case is a brief "wrong
        # tenant" view before the next switch.
        active_tenant_id, active_role = (
            memberships[0] if memberships else (None, None)
        )
        preferred = request.cookies.get("stash_active_tenant")
        if preferred and memberships:
            try:
                preferred_id = int(preferred)
            except ValueError:
                preferred_id = None
            if preferred_id is not None:
                for tid, role in memberships:
                    if tid == preferred_id:
                        active_tenant_id = tid
                        active_role = role
                        break
        request.state.tenant_names = (
            dao_tenants.tenant_names_for_email(email) if email else {}
        )
        request.state.actor = Actor(
            email=email, tenant_id=active_tenant_id, role=active_role,
            is_operator=is_operator, memberships=memberships,
            shares=shares,
        )
        actor_tokens = obs.bind_actor(email, active_tenant_id)
        response = await call_next(request)
        return response
    finally:
        # Stamp the request id on the response so a log-grep-and-go
        # workflow ("user said request 1a2b failed") works without
        # them digging through devtools.
        if response is not None:
            response.headers["X-Request-Id"] = request_id
        duration_ms = int((_time.monotonic() - started) * 1000)
        status = response.status_code if response is not None else 500
        # Skip the noisy /thumbs and /uploads paths — they fire many
        # per page render and the per-request log is more useful for
        # the mutation-and-render surface.
        path = request.url.path
        is_noisy_path = (
            path.startswith("/thumbs/")
            or path.startswith("/uploads/")
            or path.startswith("/static/")
        )
        if not is_noisy_path:
            _LOG_ROUTE.info(
                "%s %s -> %d in %dms",
                request.method, path, status, duration_ms,
            )
            # Soft-quota warning header (phase 10): ≥80% on any cap
            # surfaces here.  Skip the noisy paths so we don't pay
            # the readback per thumbnail.
            actor_post = getattr(request.state, "actor", None)
            if (response is not None and actor_post is not None
                    and actor_post.tenant_id is not None):
                _stamp_quota_warning_header(actor_post.tenant_id, response)
        obs.reset_tokens(*actor_tokens, rid_token)


def _stamp_quota_warning_header(tenant_id: int, response: Response) -> None:
    """Stamp ``X-Quota-Warning`` on the response when any cap is in
    the 80–99% band.  No-op outside that band so quiet sessions
    don't carry an empty header.  Cheap enough for the per-request
    path: two SUM queries against an indexed table."""
    try:
        caps = dao_quotas.get_caps(tenant_id)
        used = dao_quotas.usage_for_tenant(tenant_id)
    except Exception:  # noqa: BLE001 — telemetry never fails the response
        return
    warnings: list[str] = []
    for key in ("monthly_ai_calls", "monthly_upload_bytes",
                "daily_ai_cost_micros"):
        cap = caps.get(key)
        if not cap:
            continue
        band = dao_quotas.warning_band(used.get(key, 0), cap)
        if band == "warning":
            warnings.append(
                f"{key}={dao_quotas.percent(used.get(key, 0), cap)}%"
            )
    if warnings:
        response.headers["X-Quota-Warning"] = ", ".join(warnings)


def _static_version() -> str:
    """Content hash of style.css, used for cache-busting. Picks up file changes
    without requiring a server restart — recomputed per request (tiny file, cheap)."""
    css = ROOT / "static" / "style.css"
    if not css.exists():
        return "0"
    import hashlib
    return hashlib.sha1(css.read_bytes()).hexdigest()[:8]


templates.env.globals["static_version"] = _static_version


def _sparkline_svg(values, *, width: int = 100, height: int = 24) -> str:
    """Inline-SVG sparkline.  Server-rendered (no JS) so the markup
    is part of the response payload and the meter is visible
    immediately.  Empty / all-zero series renders as a flat
    baseline so the user sees "I have telemetry, nothing happened"
    instead of a layout hole."""
    vals = [max(0.0, float(v or 0)) for v in (values or [])]
    if not vals:
        return ""
    peak = max(vals) or 1.0
    n = len(vals)
    if n == 1:
        # One point can't make a line; centre a dot so the row's
        # not visually blank.
        return (
            f'<svg viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}" '
            f'class="sparkline" aria-hidden="true">'
            f'<circle cx="{width / 2}" cy="{height / 2}" r="1.5" '
            f'fill="currentColor"/></svg>'
        )
    pad = 1.5
    span_w = width - 2 * pad
    span_h = height - 2 * pad
    pts = []
    for i, v in enumerate(vals):
        x = pad + (i / (n - 1)) * span_w
        y = height - pad - (v / peak) * span_h
        pts.append(f"{x:.1f},{y:.1f}")
    polyline = " ".join(pts)
    # Last point as a dot so "where we are now" is unambiguous —
    # otherwise the trend's right edge fades into the chart's
    # right margin.
    last_x, last_y = pts[-1].split(",")
    return (
        f'<svg viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" '
        f'class="sparkline" aria-hidden="true">'
        f'<polyline points="{polyline}" fill="none" '
        f'stroke="currentColor" stroke-width="1.4" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{last_x}" cy="{last_y}" r="2" '
        f'fill="currentColor"/></svg>'
    )


templates.env.globals["sparkline"] = _sparkline_svg


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Multi-writer concurrency tuning. With multi-tenant ingest fanning
    # out to pending_items + tags + thumbs writes in quick succession,
    # the default rollback journal hits "database is locked" under load.
    # WAL lets readers proceed during writes, busy_timeout absorbs short
    # contention without raising, and synchronous=NORMAL pairs cleanly
    # with WAL for the throughput bump (durability still anchored by the
    # WAL checkpoint). See spec § "SQLite concurrency".
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db():
    with db() as conn:
        conn.executescript("""
        -- ── Multi-tenancy core ────────────────────────────────────────
        -- See spec.md § "Architecture · Schema additions".  Every owned
        -- table joins back to a tenant; members are (tenant_id, email)
        -- pairs with a role; invites + object_shares are the two
        -- sharing mechanisms.  audit_log captures user-visible events
        -- (tenant_id NULL for operator cross-tenant actions).
        CREATE TABLE IF NOT EXISTS tenants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            plan TEXT NOT NULL DEFAULT 'free',
            -- Lifecycle: soft-delete first, hard-delete by cron after grace.
            deleted_at TEXT,
            hard_delete_after TEXT,
            archived_backup_key TEXT,
            archived_backup_until TEXT,
            -- Wrapped DEK (envelope encryption); populated in roadmap step 2.
            wrapped_dek BLOB,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS tenant_members (
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            email TEXT NOT NULL COLLATE NOCASE,
            role TEXT NOT NULL,
            -- NULL locale = use Accept-Language / deployment default.
            locale TEXT,
            invited_by_email TEXT,
            invited_at TEXT,
            joined_at TEXT,
            PRIMARY KEY (tenant_id, email)
        );
        CREATE INDEX IF NOT EXISTS idx_tenant_members_email ON tenant_members(email);

        CREATE TABLE IF NOT EXISTS tenant_invites (
            token TEXT PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            email TEXT NOT NULL COLLATE NOCASE,
            role TEXT NOT NULL,
            created_by_email TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT,
            consumed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tenant_invites_email ON tenant_invites(email);

        -- Per-tenant API tokens (phase 11).  Bearer auth on /api/v1.
        -- ``token_hash`` is sha256(plaintext) — the plaintext is shown
        -- exactly once at mint time and never stored, so a DB leak
        -- doesn't expose live tokens.  ``last_used_at`` lets a user
        -- audit which tokens are actually in flight; revocation is
        -- by setting ``revoked_at``.  Role pins what the bearer can
        -- do (typically ``maintainer``); future scopes (read-only,
        -- ai-only) plug in via the ``scopes`` JSON column.
        CREATE TABLE IF NOT EXISTS api_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'maintainer',
            scopes TEXT,
            created_by_email TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_used_at TEXT,
            revoked_at TEXT,
            -- Short tag for *why* the token was killed
            -- (``manual``, ``seen_over_http``, ``leaked_in_url``,
            -- ``operator_revoke``).  Surfaces in /admin so the
            -- operator can spot a misconfigured client without
            -- digging through the audit log.
            revoked_reason TEXT,
            -- Operator-driven temporary pause; auth fails while
            -- the column is non-null but a future operator action
            -- can clear it to resume.  Permanent kill is
            -- ``revoked_at``; suspension is reversible.
            suspended_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_api_tokens_tenant ON api_tokens(tenant_id);

        -- ── OAuth 2.1 authorization server (phase 19) ───────────────
        -- Stash acts as both resource server *and* authorization
        -- server for MCP per spec rev 2025-11-25.  See spec.md §
        -- "OAuth 2.1 authorization" for the contract.

        -- Registered OAuth clients.  ``client_id`` is either a
        -- DCR-assigned random string or a pre-registered name an
        -- operator has approved.  Public clients (browser-based
        -- like claude.ai) carry NULL ``client_secret_hash`` and
        -- rely on PKCE.
        CREATE TABLE IF NOT EXISTS oauth_clients (
            client_id TEXT PRIMARY KEY,
            client_secret_hash TEXT,
            name TEXT NOT NULL,
            -- JSON array of registered redirect URIs.  Each
            -- /authorize request validates exact-match against
            -- this list (open-redirect mitigation).
            redirect_uris TEXT NOT NULL,
            is_public INTEGER NOT NULL DEFAULT 1,
            registered_by_email TEXT,
            registered_at TEXT DEFAULT CURRENT_TIMESTAMP,
            revoked_at TEXT
        );

        -- Short-lived authorization codes the user produces by
        -- approving the consent page.  TTL 60 s — long enough for
        -- the redirect to bounce through claude.ai's callback,
        -- short enough that a leaked code is mostly stale.  PKCE
        -- challenge stored verbatim; verifier compared at /token.
        CREATE TABLE IF NOT EXISTS oauth_authorization_codes (
            code TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            code_challenge TEXT NOT NULL,
            code_challenge_method TEXT NOT NULL DEFAULT 'S256',
            scope TEXT,
            resource TEXT NOT NULL,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            user_email TEXT NOT NULL,
            role TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_oauth_codes_client
            ON oauth_authorization_codes(client_id);

        -- Refresh tokens for the OAuth flow.  Same hash-only
        -- storage as api_tokens — plaintext leaves once, in the
        -- /token response.  Spec mandates rotation on every use
        -- for public clients; ``consumed_at`` is set when a refresh
        -- successfully exchanges for a new pair.
        CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
            token_hash TEXT PRIMARY KEY,
            oauth_client_id TEXT NOT NULL,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            user_email TEXT NOT NULL,
            role TEXT NOT NULL,
            scope TEXT,
            resource TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_oauth_refresh_client
            ON oauth_refresh_tokens(oauth_client_id);

        CREATE TABLE IF NOT EXISTS object_shares (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            -- 'box' | 'item'.  Discriminator over the join target.
            target_kind TEXT NOT NULL,
            target_id INTEGER NOT NULL,
            recipient_email TEXT NOT NULL COLLATE NOCASE,
            role TEXT NOT NULL,
            created_by_email TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            revoked_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_object_shares_target ON object_shares(target_kind, target_id);
        CREATE INDEX IF NOT EXISTS idx_object_shares_recipient ON object_shares(recipient_email);
        -- Partial UNIQUE on the *active* triple — same shape spec §
        -- "Sharing model" implied.  Two concurrent share-creates for
        -- the same (kind, id, email) raced into duplicate active
        -- rows under the previous schema; the constraint forces the
        -- DAO's idempotent UPDATE-OR-INSERT path to be the only way
        -- to land an active share.
        CREATE UNIQUE INDEX IF NOT EXISTS idx_object_shares_active
            ON object_shares(target_kind, target_id, recipient_email)
            WHERE revoked_at IS NULL;

        CREATE TABLE IF NOT EXISTS usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            -- 'ai' | 'upload' | 'backup' | 'core'.
            surface TEXT NOT NULL,
            -- 'gemini_detect', 'gemini_art', 'anthropic_match',
            -- 'upload_bytes', 'backup_bytes', etc.
            kind TEXT NOT NULL,
            units INTEGER NOT NULL,
            cost_micros INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_usage_events_tenant_surface
            ON usage_events(tenant_id, surface, created_at);

        -- High-frequency counters (currently just downloads).  One
        -- row per (tenant_id, day, surface, kind) instead of one row
        -- per serve, so a grid view that fetches 50 thumbs writes
        -- ONE UPSERT per kind rather than 50 INSERTs.  The
        -- bandwidth + storage panels on /usage read these in
        -- preference to ``usage_events`` for download_bytes; the
        -- AI / upload / backup surfaces stay event-keyed because
        -- audit + per-call detail matters more than throughput.
        CREATE TABLE IF NOT EXISTS usage_rollups (
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            day TEXT NOT NULL,        -- YYYY-MM-DD UTC
            surface TEXT NOT NULL,    -- 'download'
            kind TEXT NOT NULL,       -- 'download_bytes'
            units INTEGER NOT NULL DEFAULT 0,
            cost_micros INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (tenant_id, day, surface, kind)
        );
        CREATE INDEX IF NOT EXISTS idx_usage_rollups_tenant_day
            ON usage_rollups(tenant_id, day);

        CREATE TABLE IF NOT EXISTS quotas (
            tenant_id INTEGER PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
            monthly_ai_calls INTEGER,
            monthly_upload_bytes INTEGER,
            backup_storage_bytes INTEGER,
            -- JSON blob for plan-specific overrides; NULL = inherit plan defaults.
            overrides_json TEXT
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            -- NULL on operator cross-tenant actions that don't belong to a
            -- single tenant (e.g. global DR import).
            tenant_id INTEGER,
            actor_email TEXT,
            action TEXT NOT NULL,
            target_kind TEXT,
            target_id INTEGER,
            metadata_json TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_audit_log_tenant ON audit_log(tenant_id, created_at);

        -- In-app feedback widget: every page carries a floating button
        -- that opens a "tell us what's wrong" form.  Submissions land
        -- here; operators triage on /admin (status: open → accepted /
        -- rejected / done).  Screenshot is the encrypted filename
        -- (same encryption pipeline as photos) so a hostile
        -- screenshot can't leak cross-tenant on disk.
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER,
            actor_email TEXT,
            body TEXT NOT NULL,
            screenshot TEXT,
            source_url TEXT,
            user_agent TEXT,
            viewport_w INTEGER,
            viewport_h INTEGER,
            status TEXT NOT NULL DEFAULT 'open',
            operator_notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT,
            resolved_by TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback(status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_feedback_tenant ON feedback(tenant_id, created_at DESC);

        -- Public leaderboard handles.  Stars (= shipped feedback
        -- rows) are tied to ``actor_email``; the original /leaderboard
        -- shipped rendering the email's local-part on the public
        -- podium, which is a privacy bug (feedback #30 — "we can't
        -- show user emails on the leaderboard, if someone gets a
        -- star they stay completely anonymous until they set a
        -- username / handle").  Handles are explicit opt-in:
        -- nothing renders publicly until the user picks one.  An
        -- operator can revoke a handle (set ``revoked_at``) at any
        -- time — meant for "no ists or isms" enforcement on a
        -- public-facing surface.
        CREATE TABLE IF NOT EXISTS feedback_handles (
            actor_email TEXT PRIMARY KEY,
            handle TEXT NOT NULL,
            handle_lower TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT,
            revoked_at TEXT,
            revoked_by TEXT,
            revoked_reason TEXT
        );
        -- Uniqueness on ACTIVE handles only — a revoked one can be
        -- re-claimed by someone else or kept in place as audit
        -- evidence.  SQLite's partial index handles this directly.
        CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_handles_unique
          ON feedback_handles(handle_lower)
          WHERE revoked_at IS NULL;

        -- ── App data tables (existing) ────────────────────────────────
        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            floorplan TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS floors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            floorplan TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_floors_location ON floors(location_id);
        CREATE TABLE IF NOT EXISTS rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
            floor_id INTEGER REFERENCES floors(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            -- Bounding box on the floorplan as fractions 0..1 of image dims so
            -- coordinates survive re-uploading at a different resolution.
            x REAL NOT NULL DEFAULT 0,
            y REAL NOT NULL DEFAULT 0,
            w REAL NOT NULL DEFAULT 0,
            h REAL NOT NULL DEFAULT 0,
            color TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_rooms_location ON rooms(location_id);
        -- idx_rooms_floor is created in migrate_db, after the floor_id column
        -- has been ALTER-added on legacy schemas.
        CREATE TABLE IF NOT EXISTS boxes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            location TEXT,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            box_id INTEGER NOT NULL REFERENCES boxes(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            notes TEXT,
            photo TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ingest_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            photo TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            item_count INTEGER,
            error TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS pending_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            photo TEXT,
            suggested_box_id INTEGER REFERENCES boxes(id) ON DELETE SET NULL,
            suggested_new_box_name TEXT,
            suggested_new_box_location TEXT,
            suggestion_reason TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS item_tags (
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            value TEXT,
            PRIMARY KEY (item_id, tag_id)
        );
        CREATE TABLE IF NOT EXISTS pending_item_tags (
            pending_item_id INTEGER NOT NULL REFERENCES pending_items(id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            value TEXT,
            PRIMARY KEY (pending_item_id, tag_id)
        );
        CREATE INDEX IF NOT EXISTS idx_item_tags_tag ON item_tags(tag_id);
        CREATE INDEX IF NOT EXISTS idx_pending_item_tags_tag ON pending_item_tags(tag_id);
        """)


def _add_column_if_missing(conn, table: str, column: str, col_def: str) -> None:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")


def migrate_db():
    with db() as conn:
        _add_column_if_missing(conn, "boxes", "last_audited_at", "TEXT")
        _add_column_if_missing(conn, "items", "is_missing", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "items", "last_seen_at", "TEXT")
        _add_column_if_missing(conn, "pending_items", "previous_box_name", "TEXT")
        # Bounding box from Gemini vision (0-1000 normalized coords)
        _add_column_if_missing(conn, "pending_items", "bbox_y_min", "INTEGER")
        _add_column_if_missing(conn, "pending_items", "bbox_x_min", "INTEGER")
        _add_column_if_missing(conn, "pending_items", "bbox_y_max", "INTEGER")
        _add_column_if_missing(conn, "pending_items", "bbox_x_max", "INTEGER")
        # Source photo preserved on items for crop undo / re-crop
        _add_column_if_missing(conn, "items", "source_photo", "TEXT")
        # Generated label background art (Nano Banana 2). Filename in UPLOAD_DIR.
        _add_column_if_missing(conn, "boxes", "background_art", "TEXT")
        # Floorplan-driven room association. boxes.location stays as a free-text
        # fallback for now, but room_id is the source of truth going forward.
        _add_column_if_missing(
            conn, "boxes", "room_id",
            "INTEGER REFERENCES rooms(id) ON DELETE SET NULL",
        )
        # Multi-floor: rooms now hang off a floor instead of straight off a
        # location. Original location_id is kept as a denormalized hint.
        _add_column_if_missing(
            conn, "rooms", "floor_id",
            "INTEGER REFERENCES floors(id) ON DELETE CASCADE",
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rooms_floor ON rooms(floor_id)")
        # Per-box color override. Falls back to the room color when null,
        # so boxes can choose to break out from their room's hue.
        _add_column_if_missing(conn, "boxes", "color", "TEXT")
        # Per-box label orientation (Avery shipping-label pivot).
        # ``landscape`` = QR + text read left-to-right when the
        # label sits with its long axis horizontal.  ``portrait``
        # rotates the content 90° within the same physical cell
        # for tall narrow box sides.  Default landscape — matches
        # how 5523 sheets feed through a printer.
        _add_column_if_missing(
            conn, "boxes", "label_orientation",
            "TEXT NOT NULL DEFAULT 'landscape'",
        )
        # Stripe billing (phase: Pro tier).  The Pro tier is "same
        # features, higher caps" — quota differentiation is already
        # in dao/quotas.py's ``_PLAN_DEFAULTS``.  Subscription state
        # rides on the tenants row so a webhook update is one
        # UPDATE.  Missing columns on a legacy schema add as NULL;
        # the upgrade flow handles "no Stripe state yet" implicitly.
        _add_column_if_missing(conn, "tenants", "stripe_customer_id", "TEXT")
        _add_column_if_missing(conn, "tenants", "stripe_subscription_id", "TEXT")
        _add_column_if_missing(conn, "tenants", "subscription_status", "TEXT")
        _add_column_if_missing(
            conn, "tenants", "subscription_current_period_end", "TEXT",
        )
        # Audit session start timestamp.  The Tinder-style swipe UI
        # at /boxes/{id}/audit lets a user pause + resume — we know
        # an item has been audited in the current session if its
        # ``items.last_seen_at`` is >= this column.  Stamped on
        # "Start audit"; cleared on "Finish".  No separate audit
        # session table needed.
        _add_column_if_missing(conn, "boxes", "last_audit_started_at", "TEXT")
        # First-run onboarding tour state.  One row per (user, feature)
        # pair the user has completed.  ``version`` lets the operator
        # bump a tour and force every user to re-see the updated copy
        # by raising the registered version in tours.py; rows below the
        # new version are treated as "not seen".  Per-user (by email)
        # rather than per-tenant because the tour state is a UX
        # preference of the human, not the organization.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tour_seen (
                actor_email TEXT NOT NULL,
                feature     TEXT NOT NULL,
                version     INTEGER NOT NULL DEFAULT 1,
                seen_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (actor_email, feature)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tour_seen_actor ON tour_seen(actor_email)"
        )
        # Packing-session hint (carried from /ingest's box picker).
        # The session lives entirely in the page form — leave the
        # /ingest page or reload it and there's no session to clean
        # up — so the only persistence is this per-job hint that
        # threads through to ``pending_items.suggested_box_id`` when
        # the worker creates pending items.  Pre-fills the sort
        # queue's box selection so accepting a packing-session item
        # is one tap; user still gets the sort review pass to
        # accept/reject AI names + crops.
        _add_column_if_missing(
            conn, "ingest_jobs", "target_box_id",
            "INTEGER REFERENCES boxes(id) ON DELETE SET NULL",
        )
        # Detection scope hint from the /ingest form: 'auto' (default),
        # 'single' (one item per photo), or 'many' (crowded pile).
        # The worker reads it back when calling vision.detect_items so
        # the prompt matches user intent — fixes the "took a photo of
        # ONE thing and Gemini returned dozens of fake items" failure
        # mode.  Also persisted so retries replay with the same scope.
        _add_column_if_missing(
            conn, "ingest_jobs", "scope",
            "TEXT NOT NULL DEFAULT 'auto'",
        )
        # Token-revocation hardening (phase 11+).  Reason is a short
        # tag; suspended_at is a separate temporal field so an
        # operator can pause-and-resume without losing the original
        # mint metadata.  Auth fails for both states.
        _add_column_if_missing(conn, "api_tokens", "revoked_reason", "TEXT")
        _add_column_if_missing(conn, "api_tokens", "suspended_at", "TEXT")
        # OAuth 2.1 augments (phase 19).  Tokens issued via the
        # OAuth flow carry an audience (the resource URI), an
        # expiry, and a link back to the issuing oauth_clients row.
        # User-minted tokens (the original phase-11 surface) keep
        # NULL on all three — the auth path treats NULL audience as
        # "valid for any tenant-scoped request" so existing tokens
        # don't break on the upgrade.
        _add_column_if_missing(conn, "api_tokens", "oauth_client_id", "TEXT")
        _add_column_if_missing(conn, "api_tokens", "audience", "TEXT")
        _add_column_if_missing(conn, "api_tokens", "expires_at", "TEXT")
        # Backfill source_photo for items created before this column existed
        conn.execute(
            "UPDATE items SET source_photo = photo WHERE source_photo IS NULL"
        )
        _migrate_legacy_locations(conn)
        _migrate_locations_to_floors(conn)

        # Multi-tenancy: every owned table gets a tenant_id pointing at
        # tenants(id).  Nullable in the column DDL because SQLite doesn't
        # support adding NOT NULL via ALTER without a DEFAULT we can't
        # safely supply pre-backfill — the DAO enforces non-null on writes
        # once it lands.  See spec.md § "Schema migrations to existing
        # tables".
        for tbl in (
            "locations", "floors", "rooms", "boxes", "items",
            "tags", "item_tags", "pending_item_tags",
            "pending_items", "ingest_jobs",
        ):
            _add_column_if_missing(
                conn, tbl, "tenant_id",
                "INTEGER REFERENCES tenants(id) ON DELETE CASCADE",
            )
        # Hot-path indexes — every later query that filters by tenant_id
        # rides on these.
        for idx_sql in (
            "CREATE INDEX IF NOT EXISTS idx_boxes_tenant ON boxes(tenant_id)",
            "CREATE INDEX IF NOT EXISTS idx_items_tenant_box ON items(tenant_id, box_id)",
            "CREATE INDEX IF NOT EXISTS idx_locations_tenant ON locations(tenant_id)",
            "CREATE INDEX IF NOT EXISTS idx_floors_tenant ON floors(tenant_id)",
            "CREATE INDEX IF NOT EXISTS idx_rooms_tenant ON rooms(tenant_id)",
            "CREATE INDEX IF NOT EXISTS idx_pending_items_tenant ON pending_items(tenant_id)",
            "CREATE INDEX IF NOT EXISTS idx_ingest_jobs_tenant ON ingest_jobs(tenant_id)",
            "CREATE INDEX IF NOT EXISTS idx_tags_tenant ON tags(tenant_id)",
        ):
            conn.execute(idx_sql)

        # Versioning column for the optimistic-concurrency story
        # (roadmap step 4).  Defaults to 1 so existing rows behave as
        # "version 1" on the first edit after the migration lands.
        for tbl in ("boxes", "items", "rooms", "floors", "locations"):
            _add_column_if_missing(conn, tbl, "version", "INTEGER NOT NULL DEFAULT 1")
            _add_column_if_missing(conn, tbl, "updated_at", "TEXT")

        _migrate_to_multi_tenant(conn)
        conn.commit()
    # Filesystem migration runs outside the DB transaction (file IO can
    # take a while on big stashes; we don't want a long transaction
    # holding writes for everyone).  Idempotent — re-runs are no-ops
    # because already-migrated files live in tenant subdirs and carry
    # the encryption marker.
    _migrate_uploads_to_encrypted_tenant_dirs()


def _migrate_uploads_to_encrypted_tenant_dirs() -> None:
    """One-shot relocate + encrypt for pre-phase-2 cleartext uploads.

    Walks UPLOAD_DIR's flat root and for each cleartext blob:
      1. Looks up which tenant owns the file (any DB column reference).
      2. Encrypts it with that tenant's DEK.
      3. Writes the ciphertext to UPLOAD_DIR/{tenant_id}/{name}.
      4. Roundtrip-verifies the new file before deleting the original.

    Idempotent — files already inside a tenant subdir are skipped, and
    files in the flat root that already start with the encryption
    marker just get moved (no re-encryption).  Orphans (no DB
    reference) stay where they are; the orphan sweep cleans them up
    later.

    Crashes mid-migration are safe: each file is processed in
    isolation, with the cleartext original surviving until the new
    encrypted blob has been roundtrip-verified.

    Logs to stdout: a banner before the scan, per-tenant
    encrypted/relocated/orphaned/failed counts after.  When the
    function is a no-op (no flat-root files left to process) it stays
    quiet."""
    log = obs.get_logger("migrate")

    if not UPLOAD_DIR.exists():
        return

    # Quick pre-check: anything in the flat root at all?  If not, the
    # migration is a no-op and we don't need to log a thing.
    flat_root_files = [
        e for e in UPLOAD_DIR.iterdir()
        if e.is_file() and e.suffix != ".tmp"
    ]
    if not flat_root_files:
        return

    log.warning(
        "[migrate] phase-2 filesystem migration: %d cleartext file(s) in "
        "UPLOAD_DIR root — relocating into per-tenant subdirs and encrypting",
        len(flat_root_files),
    )

    # Build a (filename → tenant_id) map from every reference column,
    # plus the thumb companion of each.
    file_owners: dict[str, int] = {}
    with db() as conn:
        for sql in (
            "SELECT tenant_id, photo FROM items WHERE photo IS NOT NULL",
            "SELECT tenant_id, source_photo FROM items WHERE source_photo IS NOT NULL",
            "SELECT tenant_id, photo FROM pending_items WHERE photo IS NOT NULL",
            "SELECT tenant_id, photo FROM ingest_jobs WHERE photo IS NOT NULL",
            "SELECT tenant_id, background_art FROM boxes WHERE background_art IS NOT NULL",
            "SELECT tenant_id, floorplan FROM floors WHERE floorplan IS NOT NULL",
            "SELECT tenant_id, floorplan FROM locations WHERE floorplan IS NOT NULL",
        ):
            for tid, name in conn.execute(sql).fetchall():
                if tid is None or not name:
                    continue
                file_owners[name] = tid
                file_owners[f"{Path(name).stem}_thumb.jpg"] = tid

    encrypted = relocated_only = orphaned = failed = tmp_swept = 0
    by_tenant: dict[int, int] = {}

    for entry in flat_root_files:
        if entry.suffix == ".tmp":
            # Atomic-rename intermediate from an interrupted write.
            try:
                entry.unlink()
                tmp_swept += 1
            except FileNotFoundError:
                pass
            continue
        tenant_id = file_owners.get(entry.name)
        if tenant_id is None:
            # Orphan — leave for /maintenance/cleanup to deal with so a
            # bug here can't accidentally delete an unmapped file the
            # operator might want to rescue.
            orphaned += 1
            continue
        target = _tenant_file(tenant_id, entry.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            # Already migrated under this name (an earlier interrupted
            # run got this far).  Drop the original.
            try:
                entry.unlink()
            except FileNotFoundError:
                pass
            continue
        try:
            blob = entry.read_bytes()
            if vault.looks_encrypted(blob):
                # Pre-encrypted (operator manually placed it, perhaps).
                # Just move it.
                entry.rename(target)
                relocated_only += 1
                by_tenant[tenant_id] = by_tenant.get(tenant_id, 0) + 1
                continue
            ciphertext = _encrypt_for(tenant_id, blob)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_bytes(ciphertext)
            roundtrip = _decrypt_for(tenant_id, tmp.read_bytes())
            if roundtrip != blob:
                tmp.unlink()
                failed += 1
                log.error(
                    "[migrate] roundtrip-verify failed for %s; cleartext "
                    "original kept in place for manual inspection",
                    entry.name,
                )
                continue  # something went wrong; original stays put
            os.replace(tmp, target)
            entry.unlink()
            encrypted += 1
            by_tenant[tenant_id] = by_tenant.get(tenant_id, 0) + 1
        except Exception as exc:
            # Don't crash startup on a single bad file.  An operator
            # can investigate via the filesystem if photos go missing.
            failed += 1
            log.error(
                "[migrate] failed to encrypt %s: %s; cleartext kept "
                "in place for manual inspection",
                entry.name, exc,
            )
            continue

    log.warning(
        "[migrate] phase-2 filesystem migration done: "
        "encrypted=%d relocated_only=%d orphaned=%d failed=%d tmp_swept=%d "
        "by_tenant=%s",
        encrypted, relocated_only, orphaned, failed, tmp_swept, by_tenant,
    )
    if orphaned:
        log.warning(
            "[migrate] %d file(s) in UPLOAD_DIR root had no DB reference and "
            "were left in place; run /maintenance/cleanup once you've "
            "confirmed there's nothing in there worth rescuing",
            orphaned,
        )
    if failed:
        log.warning(
            "[migrate] %d file(s) failed to migrate — cleartext originals "
            "are still in UPLOAD_DIR root.  Investigate before re-running.",
            failed,
        )


def _first_email_from_env(var: str) -> str:
    """Pluck the first non-empty email out of a comma-separated env var.
    Falls back to '' if none."""
    for part in os.environ.get(var, "").split(","):
        e = part.strip().lower()
        if e:
            return e
    return ""


def _migrate_to_multi_tenant(conn) -> None:
    """One-time: existing data gets folded into a Personal tenant.

    Runs whenever migrate_db sees rows in the app tables and no tenants
    yet exist.  The bootstrap email comes from STASH_BOOTSTRAP_MEMBER_EMAIL
    (preferred), falling back to the first entry in STASH_ALLOWED_EMAILS
    so existing single-user deploys upgrade without extra config.

    Idempotent: any subsequent run is a no-op once tenants exist.
    Backfill of NULL tenant_id rows happens unconditionally because a
    legacy route inserting after the migration could leave fresh NULLs."""
    if conn.execute("SELECT 1 FROM tenants LIMIT 1").fetchone():
        # Sweep any NULLs that snuck in from legacy routes — once the
        # DAO migration (roadmap step 3-4) is complete this becomes a
        # no-op.
        for tbl in (
            "locations", "floors", "rooms", "boxes", "items",
            "tags", "item_tags", "pending_item_tags",
            "pending_items", "ingest_jobs",
        ):
            conn.execute(
                f"UPDATE {tbl} SET tenant_id = (SELECT id FROM tenants ORDER BY id LIMIT 1) "
                f"WHERE tenant_id IS NULL"
            )
        return

    has_data = any(
        conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone()
        for t in ("boxes", "items", "locations", "ingest_jobs", "pending_items")
    )
    if not has_data:
        # Fresh DB — first user creates a tenant via the sign-up flow
        # (roadmap step 5).  Nothing to migrate yet.
        return

    bootstrap_email = (
        os.environ.get("STASH_BOOTSTRAP_MEMBER_EMAIL", "").strip().lower()
        or _first_email_from_env("STASH_ALLOWED_EMAILS")
    )

    cur = conn.execute(
        "INSERT INTO tenants (name, plan) VALUES ('Personal', 'pro')"
    )
    tenant_id = cur.lastrowid

    if bootstrap_email:
        conn.execute(
            "INSERT INTO tenant_members "
            "(tenant_id, email, role, joined_at) "
            "VALUES (?, ?, 'maintainer', CURRENT_TIMESTAMP)",
            (tenant_id, bootstrap_email),
        )
        conn.execute(
            "INSERT INTO audit_log (tenant_id, actor_email, action, metadata_json) "
            "VALUES (?, ?, 'tenant.bootstrap', ?)",
            (tenant_id, bootstrap_email, '{"source": "migrate_to_multi_tenant"}'),
        )

    for tbl in (
        "locations", "floors", "rooms", "boxes", "items",
        "tags", "item_tags", "pending_item_tags",
        "pending_items", "ingest_jobs",
    ):
        conn.execute(
            f"UPDATE {tbl} SET tenant_id = ? WHERE tenant_id IS NULL",
            (tenant_id,),
        )


def _migrate_locations_to_floors(conn) -> None:
    """Convert each existing location-with-floorplan into a single 'Main floor'
    so the floor selector has something to point at and existing rooms get
    attached to a floor. Idempotent — only runs if no floors exist yet."""
    if conn.execute("SELECT 1 FROM floors LIMIT 1").fetchone():
        return
    locs = conn.execute(
        "SELECT id, floorplan FROM locations WHERE floorplan IS NOT NULL"
    ).fetchall()
    for loc in locs:
        cur = conn.execute(
            "INSERT INTO floors (location_id, name, floorplan, sort_order) "
            "VALUES (?, 'Main floor', ?, 0)",
            (loc["id"], loc["floorplan"]),
        )
        floor_id = cur.lastrowid
        conn.execute(
            "UPDATE rooms SET floor_id = ? WHERE location_id = ? AND floor_id IS NULL",
            (floor_id, loc["id"]),
        )


def _migrate_legacy_locations(conn) -> None:
    """One-shot conversion of free-text `boxes.location` strings into a default
    Location + Rooms structure. Idempotent — only runs if no locations exist yet,
    so re-running on an already-migrated DB is a no-op."""
    has_any_location = conn.execute("SELECT 1 FROM locations LIMIT 1").fetchone()
    if has_any_location:
        return
    legacy = [
        r["location"].strip() for r in conn.execute(
            "SELECT DISTINCT location FROM boxes "
            "WHERE location IS NOT NULL AND TRIM(location) != ''"
        ).fetchall()
    ]
    if not legacy:
        return
    cur = conn.execute(
        "INSERT INTO locations (name) VALUES (?)", ("Default location",),
    )
    location_id = cur.lastrowid
    for room_name in legacy:
        cur = conn.execute(
            "INSERT INTO rooms (location_id, name, x, y, w, h) VALUES (?, ?, 0, 0, 0, 0)",
            (location_id, room_name),
        )
        room_id = cur.lastrowid
        conn.execute(
            "UPDATE boxes SET room_id = ? WHERE TRIM(location) = ?",
            (room_id, room_name),
        )


# init_db / migrate_db are called at the *bottom* of this module — after
# every helper they need (filesystem migration, encryption helpers,
# path utilities) is defined.  Keep this comment so a future contributor
# doesn't move them back up here for tidiness.


MAX_IMAGE_DIM = 2048
JPEG_QUALITY = 85
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB; matches Caddy's request_body cap

# Refuse to decode anything that would expand to > 50M pixels (~50MB raw RGB).
# Defends against PNG/TIFF "decompression bombs" — small files that allocate
# huge amounts of memory when decoded.
from PIL import Image as _PilImage
_PilImage.MAX_IMAGE_PIXELS = 50_000_000


def save_photo(tenant_id: int, photo: UploadFile | None) -> str | None:
    if not photo or not photo.filename:
        return None
    return save_photo_bytes(tenant_id, photo.file.read(), photo.filename)


def save_photo_bytes(tenant_id: int, data: bytes, filename: str) -> str:
    """Re-encode as JPEG with EXIF orientation baked in and longest side capped.

    Encrypts the resulting bytes with the tenant's DEK and writes to
    UPLOAD_DIR/{tenant_id}/{name}.

    On PIL failure we still write the bytes (test fixtures use synthetic JPEG
    headers PIL can't decode), but ALWAYS as `.jpg` — never honor the caller's
    extension. Combined with `X-Content-Type-Options: nosniff` at the edge, this
    closes the stored-XSS path where an authenticated user uploads `evil.html`.

    Also writes the companion thumbnail in the same pass so the first grid
    view of a fresh upload doesn't have to lazy-generate it.

    Records an upload usage event with the *post-encode* byte count.
    The cap check above runs against the raw bytes, but billing /
    storage cost is the encoded size that actually lands on disk —
    use that so the meter reflects what's eating B2 quota.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Upload too large")
    # Per-tenant monthly-byte cap (phase 10).  Pre-flight check so
    # a quota-exceeded upload doesn't burn the encode pass before
    # rejecting.  We use the post-encode count for billing later
    # but the raw size is a safe upper bound for the gate.
    try:
        dao_quotas.check_or_raise(
            tenant_id, "upload",
            units_about_to_record=len(data),
        )
    except dao_quotas.QuotaExceeded as exc:
        raise HTTPException(
            429,
            f"Upload would exceed monthly cap "
            f"({exc.used} > {exc.cap} bytes).  "
            "Quota resets on the 1st of next UTC month.",
        )
    from PIL import Image, ImageOps
    import io as _io
    try:
        img = Image.open(_io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        if max(img.size) > MAX_IMAGE_DIM:
            img.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM), Image.LANCZOS)
        name = f"{secrets.token_hex(8)}.jpg"
        out = _io.BytesIO()
        img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        encoded = out.getvalue()
        _write_encrypted(tenant_id, name, encoded)
        # Pre-generate the thumb from the in-memory image — cheaper than
        # opening the file again, and covers the new-upload-then-immediately-
        # render case without paying the lazy-gen cost on the first request.
        _save_thumb_from_image(tenant_id, img, name)
        dao_usage.record(tenant_id, "upload", "upload_bytes", units=len(encoded))
        return name
    except HTTPException:
        raise
    except Exception:
        name = f"{secrets.token_hex(8)}.jpg"
        _write_encrypted(tenant_id, name, data)
        dao_usage.record(tenant_id, "upload", "upload_bytes", units=len(data))
        return name


def _save_thumb_from_image(tenant_id: int, img, name: str) -> None:
    """Encrypt + write a thumbnail for `name` from an already-decoded PIL image."""
    from PIL import Image as _Image
    import io as _io
    try:
        thumb_img = img.copy()
        if max(thumb_img.size) > THUMB_MAX_DIM:
            thumb_img.thumbnail((THUMB_MAX_DIM, THUMB_MAX_DIM), _Image.LANCZOS)
        out = _io.BytesIO()
        thumb_img.save(out, format="JPEG", quality=80, optimize=True)
        thumb_name = _tenant_thumb(tenant_id, name).name
        _write_encrypted(tenant_id, thumb_name, out.getvalue())
    except Exception:
        pass  # the lazy /thumbs endpoint will retry generation on first view


def ensure_tag(conn, tenant_id: int, name: str) -> int:
    """Get or create a tag by name (case-insensitive) within a tenant.
    Returns tag id.

    Tags are per-tenant per spec § "Tag uniqueness" — same tag name in
    two tenants resolves to two distinct rows.  Existing rows from the
    pre-multi-tenant migration carry the same tenant_id but no UNIQUE
    constraint enforces (tenant_id, name) yet (DAO migration in step 3
    adds it); for now the lookup is by (tenant_id, name) and an
    INSERT OR IGNORE matches on the legacy global UNIQUE on `name`."""
    name = name.strip()
    conn.execute(
        "INSERT OR IGNORE INTO tags (name, tenant_id) VALUES (?, ?)",
        (name, tenant_id),
    )
    row = conn.execute(
        "SELECT id FROM tags WHERE name = ? AND "
        "(tenant_id = ? OR tenant_id IS NULL) "
        "ORDER BY tenant_id IS NULL, id LIMIT 1",
        (name, tenant_id),
    ).fetchone()
    return row["id"]


def parse_tag_input(raw: str) -> list[tuple[str, str | None]]:
    """Parse comma-separated tag input. 'serial:ABC' → ('serial', 'ABC'), 'red' → ('red', None)."""
    results = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            key, val = part.split(":", 1)
            results.append((key.strip(), val.strip() or None))
        else:
            results.append((part, None))
    return results


def format_tag(name: str, value: str | None) -> str:
    return f"{name}:{value}" if value else name


def _open_image_oriented(path: Path):
    """Open an image and apply EXIF orientation so pixels match what the user sees.
    Cropper.js auto-rotates per EXIF, so we must do the same before cropping."""
    from PIL import Image, ImageOps
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    return img


def crop_photo(tenant_id: int, photo_name: str, bbox: tuple[int, int, int, int]) -> str:
    """Crop a photo using bbox (y_min, x_min, y_max, x_max in 0-1000 coords).
    Returns the filename of the cropped image saved to UPLOAD_DIR/{tenant_id}/.

    Source is read + decrypted from the tenant's directory; the new crop is
    encrypted with the tenant's DEK before write.  Normalizes to JPEG and
    pre-generates the companion thumbnail so a re-crop is reflected
    immediately on the next page render — without the thumbnail side, lazy
    /thumbs generation can fall back to serving the source under an
    immutable cache header, leaving stale crops visible."""
    plaintext = _read_encrypted(tenant_id, photo_name)
    import io as _io
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(_io.BytesIO(plaintext)))
    w, h = img.size
    y_min, x_min, y_max, x_max = bbox
    # Convert 0-1000 normalized coords to pixels
    left = int(x_min / 1000 * w)
    top = int(y_min / 1000 * h)
    right = int(x_max / 1000 * w)
    bottom = int(y_max / 1000 * h)
    # Clamp and ensure minimum size
    left = max(0, left)
    top = max(0, top)
    right = min(w, right)
    bottom = min(h, bottom)
    if right - left < 10 or bottom - top < 10:
        return photo_name  # bbox too small, use original
    cropped = img.crop((left, top, right, bottom))
    if cropped.mode not in ("RGB", "L"):
        cropped = cropped.convert("RGB")
    crop_name = f"{secrets.token_hex(8)}.jpg"
    out = _io.BytesIO()
    cropped.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    _write_encrypted(tenant_id, crop_name, out.getvalue())
    _save_thumb_from_image(tenant_id, cropped, crop_name)
    return crop_name


def _photo_still_referenced(conn, photo_name: str) -> bool:
    """True if any row still points at this upload file."""
    if not photo_name:
        return True
    return conn.execute(
        "SELECT 1 FROM items WHERE photo = ? OR source_photo = ? "
        "UNION SELECT 1 FROM pending_items WHERE photo = ? "
        "UNION SELECT 1 FROM ingest_jobs WHERE photo = ? "
        "UNION SELECT 1 FROM boxes WHERE background_art = ? "
        "UNION SELECT 1 FROM floors WHERE floorplan = ? "
        "UNION SELECT 1 FROM locations WHERE floorplan = ? LIMIT 1",
        (photo_name,) * 7,
    ).fetchone() is not None


def _delete_upload_if_orphan(conn, tenant_id: int, photo_name: str) -> None:
    """Delete a tenant's upload (and its thumb companion) if no row
    still references it.  Caller passes the tenant_id of the row that
    just stopped pointing at the file — cross-tenant references aren't
    a thing in the new schema, so the file lives in exactly one
    tenant's directory."""
    if not photo_name or _photo_still_referenced(conn, photo_name):
        return
    try:
        _tenant_file(tenant_id, photo_name).unlink()
    except FileNotFoundError:
        pass
    # Companion thumbnail goes with the source. The thumb is a derived
    # artifact, never tracked in the DB — so its lifetime is purely tied to
    # whether anything still references the original.
    _delete_thumb_if_exists(tenant_id, photo_name)


import re as _re
_UPLOAD_NAME_RE = _re.compile(r"^[A-Za-z0-9._-]+$")


# ── Per-tenant upload paths + encryption helpers ─────────────────────
# Files live under UPLOAD_DIR/{tenant_id}/{name} and are encrypted with
# the tenant's DEK.  See spec § "Filesystem layout" + "Encryption at
# rest".  Routes derive tenant_id from request.state.actor.tenant_id;
# the URL itself stays plain (`/uploads/{name}`) so templates don't
# need to thread tenant ids through every src attribute.

def _tenant_dir(tenant_id: int) -> Path:
    return UPLOAD_DIR / str(tenant_id)


def _ensure_tenant_dir(tenant_id: int) -> Path:
    p = _tenant_dir(tenant_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _tenant_file(tenant_id: int, name: str) -> Path:
    """Encrypted blob path for a given upload."""
    return _tenant_dir(tenant_id) / name


def _tenant_thumb(tenant_id: int, name: str) -> Path:
    """Encrypted thumbnail path for a given upload (always .jpg)."""
    return _tenant_dir(tenant_id) / f"{Path(name).stem}_thumb.jpg"


def _encrypt_for(tenant_id: int, plaintext: bytes) -> bytes:
    """Encrypt bytes with the tenant's DEK.  Opens a fresh connection;
    the DEK cache hides the cost after the first call per tenant."""
    with db() as conn:
        return vault.encrypt_for_tenant(conn, tenant_id, _KEK, plaintext)


def _decrypt_for(tenant_id: int, ciphertext: bytes) -> bytes:
    with db() as conn:
        return vault.decrypt_for_tenant(conn, tenant_id, _KEK, ciphertext)


def _write_encrypted(tenant_id: int, name: str, plaintext: bytes) -> None:
    """Encrypt + atomically replace the on-disk blob."""
    _ensure_tenant_dir(tenant_id)
    target = _tenant_file(tenant_id, name)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(_encrypt_for(tenant_id, plaintext))
    os.replace(tmp, target)


def _read_encrypted(tenant_id: int, name: str) -> bytes:
    """Read + decrypt an upload.  Caller is responsible for tenant scoping
    (i.e. for routes, pull tenant_id from request.state.actor)."""
    target = _tenant_file(tenant_id, name)
    return _decrypt_for(tenant_id, target.read_bytes())


def _resolve_tenant_for_filename(conn, name: str) -> int | None:
    """Reverse-lookup which tenant owns a file by scanning every column
    that holds an upload reference.  Used by serve_upload / serve_thumb
    to keep URLs tenant-id-free; also used by the filesystem migration
    to relocate cleartext blobs into the right per-tenant directory."""
    for sql in (
        "SELECT tenant_id FROM items WHERE photo = ? OR source_photo = ? LIMIT 1",
        "SELECT tenant_id FROM pending_items WHERE photo = ? LIMIT 1",
        "SELECT tenant_id FROM ingest_jobs WHERE photo = ? LIMIT 1",
        "SELECT tenant_id FROM boxes WHERE background_art = ? LIMIT 1",
        "SELECT tenant_id FROM floors WHERE floorplan = ? LIMIT 1",
        "SELECT tenant_id FROM locations WHERE floorplan = ? LIMIT 1",
    ):
        params = (name, name) if "OR" in sql else (name,)
        row = conn.execute(sql, params).fetchone()
        if row and row["tenant_id"] is not None:
            return row["tenant_id"]
    # Thumb companions follow their source: strip _thumb suffix and
    # try the source's owner.  Same approach the migration uses.
    stem = Path(name).stem
    if stem.endswith("_thumb"):
        source_stem = stem[: -len("_thumb")]
        # Source extension is unknown without filesystem inspection, but
        # the DB column always carries the full filename — try the four
        # most common image extensions.
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            for tid_sql in (
                "SELECT tenant_id FROM items WHERE photo = ? OR source_photo = ? LIMIT 1",
                "SELECT tenant_id FROM pending_items WHERE photo = ? LIMIT 1",
                "SELECT tenant_id FROM ingest_jobs WHERE photo = ? LIMIT 1",
                "SELECT tenant_id FROM boxes WHERE background_art = ? LIMIT 1",
                "SELECT tenant_id FROM floors WHERE floorplan = ? LIMIT 1",
                "SELECT tenant_id FROM locations WHERE floorplan = ? LIMIT 1",
            ):
                src = f"{source_stem}{ext}"
                params = (src, src) if "OR" in tid_sql else (src,)
                row = conn.execute(tid_sql, params).fetchone()
                if row and row["tenant_id"] is not None:
                    return row["tenant_id"]
    return None

# Longest side of a generated thumbnail. 320 px renders crisply at 100 px CSS
# squares on retina (3x = 300) without paying the cost of the 2048 px source.
THUMB_MAX_DIM = 320

# Cap concurrent thumbnail decodes so a page with a dozen brand-new photos
# can't fan out into a dozen full-resolution PIL decodes at once and run the
# container out of memory. Two at a time is plenty — each generation finishes
# in <100 ms once draft() is in play.
import threading as _threading
_THUMB_GEN_SEMAPHORE = _threading.Semaphore(2)


def _thumb_path(tenant_id: int, name: str) -> Path:
    """Companion thumbnail file for a given upload. Always .jpg since
    save_photo_bytes re-encodes everything to JPEG anyway."""
    return _tenant_thumb(tenant_id, name)


def _is_thumb_name(name: str) -> bool:
    return Path(name).stem.endswith("_thumb")


def _ensure_thumb(tenant_id: int, name: str) -> bytes | None:
    """Lazy-generate the thumb for an existing upload.  Returns decrypted
    thumb bytes or None if the source is missing / un-decodable.  Writes
    the encrypted thumb via tmp-file + rename so concurrent requests
    can't see a half-written file.

    Memory-aware: uses PIL's draft() to tell the JPEG decoder to scale down
    BEFORE allocating pixel buffers. A pre-cap 7000×7000 JPEG decodes at full
    res to ~150 MB RGB; with draft asking for ~640 px, the same image lands
    at <3 MB. Combined with the module-level semaphore, the container can no
    longer be OOM-killed by a fan-out of concurrent thumb requests."""
    if _is_thumb_name(name):
        return None
    src = _tenant_file(tenant_id, name)
    if not src.exists():
        return None
    thumb = _tenant_thumb(tenant_id, name)
    if thumb.exists():
        try:
            return _decrypt_for(tenant_id, thumb.read_bytes())
        except Exception:
            return None

    with _THUMB_GEN_SEMAPHORE:
        # Re-check now that we hold the lock — another request may have
        # generated this very thumb while we were queued.
        if thumb.exists():
            try:
                return _decrypt_for(tenant_id, thumb.read_bytes())
            except Exception:
                return None
        try:
            from PIL import Image, ImageOps
            import io as _io
            plaintext = _decrypt_for(tenant_id, src.read_bytes())
            with Image.open(_io.BytesIO(plaintext)) as opened:
                # draft() is JPEG-only and a no-op on other formats. Asking
                # for 2x the target size gives the decoder enough room to
                # downscale further with a clean LANCZOS pass below.
                if opened.format == "JPEG":
                    opened.draft("RGB", (THUMB_MAX_DIM * 2, THUMB_MAX_DIM * 2))
                img = ImageOps.exif_transpose(opened)
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                if max(img.size) > THUMB_MAX_DIM:
                    img.thumbnail((THUMB_MAX_DIM, THUMB_MAX_DIM), Image.LANCZOS)
                out = _io.BytesIO()
                img.save(out, format="JPEG", quality=80, optimize=True)
                thumb_plaintext = out.getvalue()
            _write_encrypted(tenant_id, thumb.name, thumb_plaintext)
            return thumb_plaintext
        except Exception:
            # Any decode failure (test fixtures, corrupt jpegs, OOM in PIL,
            # etc) — let the caller fall back to serving the full-res source
            # so the page doesn't break.
            return None


def _delete_thumb_if_exists(tenant_id: int, name: str) -> None:
    if _is_thumb_name(name):
        return
    try:
        _thumb_path(tenant_id, name).unlink()
    except FileNotFoundError:
        pass


def _resolve_serve_tenant(request: Request, name: str) -> int | None:
    """Pick the tenant whose directory holds `name` for this actor.

    Decision order (each layer has a different access basis):

    1. **Active membership** — file lives in the actor's own
       tenant directory.
    2. **Multi-tenant membership** — file lives in another tenant
       the actor is a member of (the switcher in roadmap step 15
       chooses one as active; the rest still resolve here).
    3. **Object share** — file is one of the photos / thumbs / box
       art reachable via an active share pointed at the actor's
       email.  Bound by :func:`dao.shares.file_allowlist_for_actor`,
       which recomputes the set per request so a box-share that
       gains items after creation covers the new photos.

    A share-only recipient *cannot* fetch arbitrary files from a
    share-tenant's directory by guessing names — only the
    allow-listed set resolves.  Operators with no membership get
    None by design (spec § "Operator surface" — no auto data
    access).
    """
    actor: Actor = request.state.actor
    if actor.tenant_id is not None:
        # Fast path: file lives in the active tenant's directory.
        if _tenant_file(actor.tenant_id, name).exists():
            return actor.tenant_id
        # Membership in another tenant?  (Multi-membership pre-dates
        # the switcher; resolve any tenant the actor has access to.)
        for tid, _role in actor.memberships:
            if tid != actor.tenant_id and _tenant_file(tid, name).exists():
                return tid
    # Share recipients: only files in the share allow-list are
    # reachable, regardless of which tenant directory holds them.
    # Compute once per request — the lookup hits the DB but is
    # bounded by the actor's share count (typically <10).
    if actor.shares:
        # Cache on request.state so a page that fetches a dozen
        # thumbs only pays for the lookup once.
        cached = getattr(request.state, "_share_allowlist", None)
        if cached is None:
            cached = dao_shares.file_allowlist_for_actor(actor)
            request.state._share_allowlist = cached
        if name in cached:
            for s in actor.shares:
                if _tenant_file(s["tenant_id"], name).exists():
                    return s["tenant_id"]
    if actor.is_operator:
        # Operators have no automatic data access through /admin (see
        # spec § "Operator surface"), but if a tenant maintainer has
        # invited them, the membership above already resolved.  Refuse
        # any operator path that isn't backed by a real membership.
        return None
    return None


@app.get("/uploads/{name}")
def serve_upload(request: Request, name: str):
    # Reject obviously hostile names before any filesystem touch. We only
    # generate names like `<hex>.jpg` — anything outside that alphabet is not
    # ours and not worth resolving.
    if not _UPLOAD_NAME_RE.match(name) or ".." in name:
        raise HTTPException(404)
    tenant_id = _resolve_serve_tenant(request, name)
    if tenant_id is None:
        raise HTTPException(404)
    upload_root = UPLOAD_DIR.resolve()
    p = _tenant_file(tenant_id, name).resolve()
    # Defense-in-depth: even after the regex check, refuse to serve anything
    # that lands outside UPLOAD_DIR (e.g. through symlink shenanigans).
    if not p.is_relative_to(upload_root) or not p.is_file():
        raise HTTPException(404)
    try:
        plaintext = _decrypt_for(tenant_id, p.read_bytes())
    except Exception:
        raise HTTPException(500, "decryption failed")
    # Egress bandwidth metering — daily-grain UPSERT so 50 thumb
    # fetches on a grid view add to one row instead of inserting 50.
    dao_usage.record_rollup(
        tenant_id, "download", "download_bytes", units=len(plaintext),
    )
    # Photo MIME — every stored upload is JPEG (save_photo_bytes
    # normalises) so we can hard-code without sniffing bytes.
    return Response(content=plaintext, media_type="image/jpeg")


@app.get("/thumbs/{name}")
def serve_thumb(request: Request, name: str):
    """Serves a downscaled version of /uploads/{name} for grid + list views.
    Filenames are content-hashed so the result is immutable — long-cached."""
    if not _UPLOAD_NAME_RE.match(name) or ".." in name:
        raise HTTPException(404)
    tenant_id = _resolve_serve_tenant(request, name)
    if tenant_id is None:
        raise HTTPException(404)
    plaintext = _ensure_thumb(tenant_id, name)
    if plaintext is None:
        # Fall through to the full-res source so the page doesn't break.
        try:
            plaintext = _read_encrypted(tenant_id, name)
        except Exception:
            raise HTTPException(404)
    # Daily-grain UPSERT keeps thumb fetches from bloating the
    # events table even on heavy grid-view sessions.
    dao_usage.record_rollup(
        tenant_id, "download", "download_bytes", units=len(plaintext),
    )
    return Response(
        content=plaintext,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/", response_class=HTMLResponse)
def public_landing(request: Request):
    """Public marketing landing — what an unauthenticated visitor
    sees at the bare site.  No tenant data, no app chrome.  The
    "Sign in" button points at ``/home`` which is the authenticated
    dashboard; oauth2-proxy redirects through Google when the user
    clicks through.

    This route is in the auth bypass list so unauth visitors can
    reach it.  Authenticated users hitting ``/`` also see the
    landing (oauth2-proxy strips session headers on bypass routes);
    they click the prominent "Open your stash →" button to land at
    /home where their tenant data lives."""
    return templates.TemplateResponse(
        request, "landing.html",
        {
            "business_name": _public_business_name(),
            "product_name": _public_product_name(),
            "contact_email": _public_contact_email(),
            "pro_price_display": _pro_price_display(),
            "public_url": PUBLIC_URL,
            "hide_feedback_widget": True,
        },
    )


@app.get("/home", response_class=HTMLResponse)
def index(request: Request):
    """The authenticated user's home — list of every box in the
    tenant, grouped by location/room.  Was at ``/`` historically;
    moved to ``/home`` so the bare site can show a public landing
    page for new visitors."""
    actor: Actor = request.state.actor
    boxes = dao_boxes.list_with_counts(actor)
    thumbs = dao_items.list_recent_photos_per_box(actor, limit_per_box=5)
    rooms = dao_rooms.list_for_picker(actor)
    box_groups = _group_boxes_for_index(boxes)
    return templates.TemplateResponse(
        request, "index.html",
        {"boxes": boxes, "thumbs": thumbs, "rooms": rooms,
         "box_groups": box_groups},
    )


def _group_boxes_for_index(boxes) -> list[dict]:
    """Bucket boxes by their (location, room) so the index renders as
    visual sections instead of one undifferentiated grid.  The SQL is
    already sorted into the right order — we just walk it and start a
    new bucket whenever the key changes."""
    groups: list[dict] = []
    prev_key: object = object()
    for b in boxes:
        if b["room_name"]:
            key = ("room", b["location_id"], b["room_id"])
        elif b["location"] and b["location"].strip():
            key = ("loc", b["location"].strip().casefold())
        else:
            key = ("none",)
        if key != prev_key:
            groups.append({
                "kind": key[0],
                "location_name": b["location_name"],
                "room_name": b["room_name"],
                "room_color": b["room_color"],
                "legacy_location": b["location"] if not b["room_name"] else None,
                "boxes": [],
            })
            prev_key = key
        groups[-1]["boxes"].append(b)
    return groups


def _coerce_room_id(value: str) -> int | None:
    """Form posts the room id as a string — empty/none means clear the link."""
    if not value or value in ("none", "0"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_if_match(value: str) -> int | None:
    """Pull the optional ``if_match`` form field into the int the DAO
    wants, or None if the field wasn't sent.  Bad values fall through
    to None — the caller's update is then a last-write-wins, but the
    fallback matters less than not 500'ing on a flaky form payload."""
    if value is None or not str(value).strip():
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _conflict_http(entity: str, entity_id: int) -> HTTPException:
    """Friendly 409 for a stale-edit collision.  Phase-4 entry point —
    one place to swap in a custom rendered page later if the bare
    text isn't enough.  Spec § "Optimistic concurrency"."""
    return HTTPException(
        409,
        f"This {entity} was edited under you. Refresh the page and "
        f"reapply your changes — the previous edit happened in another "
        f"tab or session.",
    )


@app.post("/boxes")
def create_box(
    request: Request,
    name: str = Form(...),
    location: str = Form(""),
    notes: str = Form(""),
    room_id: str = Form(""),
):
    actor: Actor = request.state.actor
    rid = _coerce_room_id(room_id)
    # If a room is picked, denormalize its name into boxes.location so the
    # plain text shows up everywhere that doesn't JOIN to rooms.  Allow
    # legacy tenant_id-NULL rooms to match too — pre-multi-tenancy
    # databases may still have those rows around.
    if rid is not None:
        with db() as conn:
            row = conn.execute(
                "SELECT name FROM rooms WHERE id = ? "
                "  AND (tenant_id = ? OR tenant_id IS NULL)",
                (rid, actor.tenant_id),
            ).fetchone()
        if row:
            location = row["name"]
        else:
            rid = None
    try:
        dao_boxes.create(actor, name, location, notes, room_id=rid)
    except ForbiddenError:
        raise HTTPException(403)
    return RedirectResponse("/home", status_code=303)


def _known_locations(actor: Actor) -> list[str]:
    """Distinct denormalised box.location strings the actor's tenant has
    used — feeds the location combobox on the box-edit form."""
    if actor.tenant_id is None:
        return []
    with db() as conn:
        return [
            r["location"] for r in conn.execute(
                "SELECT DISTINCT location FROM boxes "
                "WHERE location IS NOT NULL AND location != '' "
                "  AND tenant_id = ? "
                "ORDER BY location",
                (actor.tenant_id,),
            ).fetchall()
        ]


@app.get("/boxes/{box_id}", response_class=HTMLResponse)
def box_detail(request: Request, box_id: int):
    actor: Actor = request.state.actor
    try:
        box = dao_boxes.get_by_id(actor, box_id)
    except NotFoundError:
        raise HTTPException(404)
    items_raw = dao_items.list_for_box(actor, box_id)
    # Reverse to newest-first to match the previous ORDER BY DESC.
    items_raw = list(reversed(items_raw))
    items_with_tags = [
        {"item": it, "tags": dao_items.list_tags_for_item(actor, it["id"])}
        for it in items_raw
    ]
    other_boxes = [
        {"id": b["id"], "name": b["name"], "location": b["location"]}
        for b in dao_boxes.list_for_picker(actor)
        if b["id"] != box_id
    ]
    other_boxes.sort(key=lambda b: b["name"])
    locations = _known_locations(actor)
    rooms = dao_rooms.list_for_picker(actor)
    all_tags = dao_tags.list_names(actor)
    return templates.TemplateResponse(
        request, "box.html",
        {
            "box": box,
            "items_with_tags": items_with_tags,
            "other_boxes": other_boxes,
            "locations": locations,
            "rooms": rooms,
            "all_tags": all_tags,
            "color_palette": _ROOM_COLORS,
            "art_enabled": _ai_art_enabled(),
        },
    )


@app.post("/boxes/{box_id}/move-to-room")
def move_box_to_room(
    request: Request,
    box_id: int,
    room_id: str = Form(""),
):
    """Reassign a box to a different room. Used by floorplan drag-and-drop
    and as a generic API. Empty room_id clears the assignment."""
    actor: Actor = request.state.actor
    rid = _coerce_room_id(room_id)
    location_text = ""
    if rid is not None:
        # Validate the room belongs to this tenant before passing through.
        try:
            location_text = dao_rooms.get_with_location(actor, rid)["name"]
        except NotFoundError:
            raise HTTPException(400, "Unknown room")
    try:
        # set_room handles the box-tenancy check; we update location text
        # alongside it via the broader update() call so the legacy
        # free-text location stays in sync with the room reassignment.
        box = dao_boxes.get_by_id(actor, box_id)
        dao_boxes.update(
            actor, box_id,
            name=box["name"], location=location_text,
            notes=box.get("notes") or "", room_id=rid,
            color=box.get("color"),
        )
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    if "application/json" in request.headers.get("accept", ""):
        return {"ok": True, "room_id": rid}
    return RedirectResponse(f"/boxes/{box_id}", status_code=303)


@app.post("/boxes/{box_id}/edit")
def edit_box(
    request: Request,
    box_id: int,
    name: str = Form(...),
    location: str = Form(""),
    notes: str = Form(""),
    room_id: str = Form(""),
    color: str = Form(""),
    if_match: str = Form(""),
):
    if not name.strip():
        raise HTTPException(400, "Name required")
    actor: Actor = request.state.actor
    rid = _coerce_room_id(room_id)
    # "" / "inherit" wipes the override and falls back to the room color.
    color_val: str | None
    if color.strip() == "" or color.strip() == "inherit":
        color_val = None
    elif color.strip() in _ROOM_COLORS:
        color_val = color.strip()
    else:
        color_val = None  # silently reject off-palette
    if rid is not None:
        try:
            location = dao_rooms.get_with_location(actor, rid)["name"]
        except NotFoundError:
            rid = None
    expected_version = _coerce_if_match(if_match)
    try:
        dao_boxes.update(
            actor, box_id,
            name=name, location=location, notes=notes,
            room_id=rid, color=color_val,
            if_match=expected_version,
        )
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    except ConflictError:
        raise _conflict_http("box", box_id)
    return RedirectResponse(f"/boxes/{box_id}", status_code=303)


@app.post("/items/{item_id}/move")
def move_item(request: Request, item_id: int, box_id: int = Form(...)):
    actor: Actor = request.state.actor
    # Distinguish "item gone" (404) from "target box bad" (400) — the
    # legacy route did this via two separate SELECTs; we reproduce it
    # by validating the target box first, then letting move_to_box
    # raise NotFoundError only for missing items.
    try:
        dao_items.get_by_id(actor, item_id)
    except NotFoundError:
        raise HTTPException(404)
    try:
        dao_boxes.get_by_id(actor, box_id)
    except NotFoundError:
        raise HTTPException(400, "Unknown box")
    try:
        dao_items.move_to_box(actor, item_id, box_id)
    except ForbiddenError:
        raise HTTPException(403)
    if "application/json" in request.headers.get("accept", ""):
        return {"ok": True, "item_id": item_id, "box_id": box_id}
    return RedirectResponse(f"/boxes/{box_id}#item-{item_id}", status_code=303)


@app.post("/boxes/{box_id}/move-items")
async def bulk_move_items(request: Request, box_id: int):
    """Bulk-move all submitted item ids out of `box_id` and into the
    target.  Tenant-scoped: the UPDATE clause filters by tenant_id so a
    crafted item_ids list pointing at another tenant's items can't move
    them."""
    actor: Actor = request.state.actor
    form_data = await request.form()
    target_box_id = int(form_data["target_box_id"])
    item_ids = [int(v) for v in form_data.getlist("item_ids")]
    if not item_ids:
        return RedirectResponse(f"/boxes/{box_id}", status_code=303)
    # Validate the target box belongs to the actor's tenant.
    try:
        dao_boxes.get_by_id(actor, target_box_id)
    except NotFoundError:
        raise HTTPException(400, "Unknown target box")
    # Move each item via the DAO (tenant + box checks happen there).
    for item_id in item_ids:
        try:
            dao_items.move_to_box(actor, item_id, target_box_id)
        except (NotFoundError, ForbiddenError):
            # Skip items that don't belong to this actor / tenant
            # rather than failing the whole batch — the legacy SQL had
            # the same lenient semantics via the `AND box_id = ?` clause.
            continue
    return RedirectResponse(f"/boxes/{target_box_id}", status_code=303)


@app.get("/boxes/{box_id}/audit", response_class=HTMLResponse)
def audit_box(request: Request, box_id: int):
    """Tinder-style swipe audit.  Each item gets a full-screen card;
    the user swipes right for "found" or left for "missing", with
    keyboard + button alternatives.  Partial progress persists via
    ``boxes.last_audit_started_at`` so the user can pause + resume.

    The page renders a no-JS fallback that posts the legacy bulk
    form for accessibility + scraper-safety."""
    actor: Actor = request.state.actor
    try:
        box = dao_boxes.get_by_id(actor, box_id)
        remaining = dao_boxes.audit_session_remaining(actor, box_id)
    except NotFoundError:
        raise HTTPException(404)
    all_items = sorted(
        dao_items.list_for_box(actor, box_id),
        key=lambda it: (it["name"] or ""),
    )
    started_at = box.get("last_audit_started_at")
    # Auto-start the session on first visit so the user doesn't
    # have to click a second "Start audit" button right after
    # they clicked Audit on the box page (feedback #24, "Don't
    # make me hit start audit, just start it, that's why I
    # clicked the button.  The whole point is progress is saved
    # regardless of how far you get").  Idempotent if a session
    # is already in flight — audit_session_start is no-op-on-
    # already-started.  Only kicks in if there's actually
    # something to audit; an empty box still falls through to
    # the "No items to audit" branch.
    if started_at is None and all_items:
        try:
            dao_boxes.audit_session_start(actor, box_id)
            # Re-fetch to pick up the freshly-stamped timestamp.
            box = dao_boxes.get_by_id(actor, box_id)
            started_at = box.get("last_audit_started_at")
        except (NotFoundError, ForbiddenError):
            # Readonly member or vanished box — fall through and
            # let the template render the no-progress state.
            pass
    return templates.TemplateResponse(
        request, "audit.html",
        {
            "box": box,
            "items": all_items,
            "remaining": remaining,
            "audited_count": len(all_items) - len(remaining),
            "total_count": len(all_items),
            "session_started_at": started_at,
        },
    )


@app.post("/boxes/{box_id}/audit/start")
def audit_session_start(request: Request, box_id: int):
    """Begin (or restart) an audit session.  Idempotent on the
    'already running' case — Start while running resets the session
    timestamp, matching user expectation when they reopen the page."""
    actor: Actor = request.state.actor
    try:
        dao_boxes.audit_session_start(actor, box_id)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    if _wants_json(request):
        return {"ok": True, "box_id": box_id}
    return RedirectResponse(
        f"/boxes/{box_id}/audit", status_code=303,
    )


@app.post("/boxes/{box_id}/audit/items/{item_id}/present")
def audit_mark_present(request: Request, box_id: int, item_id: int):
    """Mark one item as found-in-the-box.  Returns JSON for the
    swipe UI to advance to the next card without a reload."""
    actor: Actor = request.state.actor
    try:
        remaining = dao_boxes.audit_mark_present(actor, box_id, item_id)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    if _wants_json(request):
        return {"ok": True, "remaining": remaining}
    return RedirectResponse(
        f"/boxes/{box_id}/audit", status_code=303,
    )


@app.post("/boxes/{box_id}/audit/items/{item_id}/missing")
def audit_mark_missing(request: Request, box_id: int, item_id: int):
    """Mark one item as missing from the box.  Moves it to the sort
    queue with provenance + tags preserved."""
    actor: Actor = request.state.actor
    try:
        remaining, pending_id = dao_boxes.audit_mark_missing(
            actor, box_id, item_id,
        )
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    if _wants_json(request):
        return {"ok": True, "remaining": remaining,
                "pending_id": pending_id}
    return RedirectResponse("/queue", status_code=303)


@app.post("/boxes/{box_id}/audit/finish")
def audit_session_finish(request: Request, box_id: int):
    """Wrap up an audit session.  Stamps ``last_audited_at`` +
    clears the session start so a future Start resets cleanly."""
    actor: Actor = request.state.actor
    try:
        dao_boxes.audit_session_finish(actor, box_id)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    if _wants_json(request):
        return {"ok": True, "box_id": box_id}
    return RedirectResponse(f"/boxes/{box_id}", status_code=303)


@app.post("/boxes/{box_id}/audit")
async def submit_audit(request: Request, box_id: int):
    form_data = await request.form()
    found_ids = {int(v) for v in form_data.getlist("found")}
    actor: Actor = request.state.actor
    with db() as conn:
        box = conn.execute(
            "SELECT name, tenant_id FROM boxes WHERE id = ? AND tenant_id = ?",
            (box_id, actor.tenant_id),
        ).fetchone()
        if not box:
            raise HTTPException(404)
        tenant_id = box["tenant_id"] or actor.tenant_id
        all_items = conn.execute(
            "SELECT id, name, notes, photo FROM items "
            "WHERE box_id = ? AND tenant_id = ?",
            (box_id, actor.tenant_id),
        ).fetchall()
        moved_to_queue = 0
        for it in all_items:
            if it["id"] in found_ids:
                conn.execute(
                    "UPDATE items SET last_seen_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (it["id"],),
                )
            else:
                # Item not in box anymore — extract to sort queue with provenance
                cur = conn.execute(
                    "INSERT INTO pending_items "
                    "(name, description, photo, previous_box_name, tenant_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (it["name"], it["notes"], it["photo"], box["name"], tenant_id),
                )
                pending_id = cur.lastrowid
                # Preserve tags
                conn.execute(
                    "INSERT INTO pending_item_tags "
                    "(pending_item_id, tag_id, value, tenant_id) "
                    "SELECT ?, tag_id, value, ? FROM item_tags WHERE item_id = ?",
                    (pending_id, tenant_id, it["id"]),
                )
                conn.execute("DELETE FROM items WHERE id = ?", (it["id"],))
                moved_to_queue += 1
        conn.execute(
            "UPDATE boxes SET last_audited_at = CURRENT_TIMESTAMP WHERE id = ?", (box_id,)
        )
        conn.commit()
    target = "/queue" if moved_to_queue else f"/boxes/{box_id}"
    return RedirectResponse(target, status_code=303)


@app.post("/boxes/{box_id}/items")
async def add_item(
    request: Request,
    box_id: int,
    name: str = Form(...),
    notes: str = Form(""),
    tags: str = Form(""),
    photo: UploadFile = File(None),
):
    actor: Actor = request.state.actor
    try:
        dao_boxes.get_by_id(actor, box_id)
    except NotFoundError:
        raise HTTPException(404)
    photo_name = save_photo(actor.tenant_id, photo)
    try:
        new_id = dao_items.create(
            actor, box_id,
            name=name, notes=notes,
            photo=photo_name, source_photo=photo_name,
        )
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    if tags.strip():
        dao_tags.attach_to_item(actor, new_id, parse_tag_input(tags))
    return RedirectResponse(f"/boxes/{box_id}", status_code=303)


@app.post("/items/{item_id}/tags")
def add_item_tag(request: Request, item_id: int, tag: str = Form(...)):
    entries = parse_tag_input(tag)
    if not entries:
        raise HTTPException(400, "Tag required")
    actor: Actor = request.state.actor
    try:
        item = dao_items.get_by_id(actor, item_id)
        dao_tags.attach_to_item(actor, item_id, entries)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    return RedirectResponse(f"/boxes/{item['box_id']}#item-{item_id}", status_code=303)


@app.post("/items/{item_id}/tags/{tag_id}/delete")
def remove_item_tag(request: Request, item_id: int, tag_id: int):
    actor: Actor = request.state.actor
    try:
        box_id = dao_items.remove_tag(actor, item_id, tag_id)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    return RedirectResponse(f"/boxes/{box_id}#item-{item_id}", status_code=303)


def _tag_suggest_quota_check(actor: Actor) -> None:
    """Pre-flight quota gate for the AI tag-suggest endpoints.
    Gemini Flash for tags is the same price tier as detect; we still
    bounce the call against ``dao_quotas`` so a runaway agent doesn't
    burn through the tenant's daily-cost cap unnoticed."""
    try:
        dao_quotas.check_or_raise(
            actor.tenant_id, "ai",
            cost_about_to_record=dao_usage._cost_for(
                "ai", "gemini_tags", 1,
            ),
        )
    except dao_quotas.QuotaExceeded as exc:
        raise HTTPException(
            429,
            f"AI quota exceeded ({exc.key}={exc.used} > {exc.cap}).  "
            "Wait for the window reset or raise the cap on /admin.",
        )


def _item_photo_bytes(tenant_id: int, photo_name: str | None) -> bytes | None:
    """Read + decrypt an item's photo for vision prompting.  Returns
    None for items without a photo or when the file is missing /
    undecryptable — the suggest path keeps working off name + notes
    alone in that case."""
    if not photo_name:
        return None
    p = _tenant_file(tenant_id, photo_name)
    if not p.exists():
        return None
    try:
        return _decrypt_for(tenant_id, p.read_bytes())
    except Exception:
        return None


@app.post("/items/{item_id}/suggest-tags")
def suggest_item_tags(request: Request, item_id: int):
    """Gemini-suggested tags for a single item.  Returns JSON
    ``{"ok": true, "tags": [...]}``; the item-detail dialog renders
    each as a one-tap apply pill.  Synchronous (sub-second on a
    short prompt) so the user doesn't see a spinner forever."""
    actor: Actor = request.state.actor
    try:
        item = dao_items.get_by_id(actor, item_id)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    _tag_suggest_quota_check(actor)
    existing = dao_tags.list_names(actor)
    photo_bytes = _item_photo_bytes(item["tenant_id"], item.get("photo"))
    try:
        tags = vision.suggest_tags_for_item(
            item["name"], item.get("notes") or "",
            photo_bytes=photo_bytes,
            existing_tags=existing,
        )
        dao_usage.record(item["tenant_id"], "ai", "gemini_tags")
    except Exception as exc:
        raise HTTPException(502, f"Tag suggestion failed: {exc}")
    return {"ok": True, "item_id": item_id, "tags": tags}


@app.post("/boxes/{box_id}/suggest-tags")
def suggest_box_tags(request: Request, box_id: int):
    """Tags that apply across every item in the box.  One Gemini call
    over all the item names + notes (no photos — text context is
    sufficient and keeps cost flat regardless of how many items the
    box has).  JSON ``{"ok": true, "tags": [...]}`` so the box page
    can render apply-to-all pills."""
    actor: Actor = request.state.actor
    try:
        box = dao_boxes.get_by_id(actor, box_id)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    _tag_suggest_quota_check(actor)
    existing = dao_tags.list_names(actor)
    items = dao_items.list_for_box(actor, box_id)
    if not items:
        return {"ok": True, "box_id": box_id, "tags": []}
    try:
        tags = vision.suggest_tags_for_box(
            box["name"], box.get("notes") or "",
            items=[{"name": it["name"], "notes": it.get("notes") or ""}
                   for it in items],
            existing_tags=existing,
        )
        dao_usage.record(box["tenant_id"], "ai", "gemini_tags")
    except Exception as exc:
        raise HTTPException(502, f"Tag suggestion failed: {exc}")
    return {"ok": True, "box_id": box_id, "tags": tags}


@app.post("/boxes/{box_id}/tag-all")
def bulk_tag_box(
    request: Request,
    box_id: int,
    tag: str = Form(...),
):
    """Tag every item currently in the box with ``tag`` (one or more,
    comma-separated, same parsing as the single-item form).  Returns
    JSON for AJAX callers; redirects to the box page otherwise so the
    no-JS path still works."""
    entries = parse_tag_input(tag)
    if not entries:
        raise HTTPException(400, "Tag required")
    actor: Actor = request.state.actor
    try:
        # Confirm the box exists + belongs to this tenant before we
        # touch items — otherwise a forged box_id silently no-ops
        # (0 items) and the user gets a confusing "tagged 0 items"
        # success.
        dao_boxes.get_by_id(actor, box_id)
        count = dao_tags.attach_to_box(actor, box_id, entries)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    if _wants_json(request):
        return {"ok": True, "box_id": box_id, "tagged": count,
                "tags": [format_tag(n, v) for n, v in entries]}
    return RedirectResponse(f"/boxes/{box_id}", status_code=303)


@app.get("/boxes/{box_id}/preview", response_class=HTMLResponse)
def box_preview(request: Request, box_id: int):
    """Compact box summary for the floorplan tile-click modal — name,
    location, item count, a few thumbs, and a link to open the box."""
    actor: Actor = request.state.actor
    try:
        box = dao_boxes.get_by_id(actor, box_id)
    except NotFoundError:
        raise HTTPException(404)
    all_items = dao_items.list_for_box(actor, box_id)
    # newest-first to match the previous ORDER BY DESC LIMIT 60.
    all_items_desc = list(reversed(all_items))
    items = all_items_desc[:60]
    item_count = len(all_items)
    return templates.TemplateResponse(
        request, "_floorplan_box_preview.html",
        {"box": box, "items": items, "item_count": item_count},
    )


@app.get("/items/{item_id}/preview", response_class=HTMLResponse)
def item_preview(request: Request, item_id: int):
    """HTML fragment rendering an item's detail card. Used by the search
    page to open a result in a modal instead of navigating away — the same
    actions (re-tag, move, replace photo, delete) work in place."""
    actor: Actor = request.state.actor
    try:
        item = dao_items.get_by_id(actor, item_id)
        box = dao_boxes.get_by_id(actor, item["box_id"])
    except NotFoundError:
        raise HTTPException(404)
    item = {**item, "box_name": box["name"]}
    tags = dao_items.list_tags_for_item(actor, item_id)
    other_boxes = sorted(
        ({"id": b["id"], "name": b["name"], "location": b["location"]}
         for b in dao_boxes.list_for_picker(actor)
         if b["id"] != item["box_id"]),
        key=lambda b: b["name"] or "",
    )
    return templates.TemplateResponse(
        request, "_search_item_modal.html",
        {"it": item, "tags": tags, "other_boxes": other_boxes},
    )


@app.get("/items/{item_id}/recrop", response_class=HTMLResponse)
def recrop_item(request: Request, item_id: int):
    actor: Actor = request.state.actor
    try:
        item = dao_items.get_by_id(actor, item_id)
    except NotFoundError:
        raise HTTPException(404)
    return templates.TemplateResponse(
        request, "recrop.html", {"item": item}
    )


@app.post("/items/{item_id}/recrop")
def apply_recrop(
    request: Request,
    item_id: int,
    crop_y_min: str = Form(""),
    crop_x_min: str = Form(""),
    crop_y_max: str = Form(""),
    crop_x_max: str = Form(""),
    skip_crop: str = Form(""),
):
    actor: Actor = request.state.actor
    try:
        item = dao_items.get_for_recrop(actor, item_id)
    except NotFoundError:
        raise HTTPException(404)
    tenant_id = item["tenant_id"]
    source = item["source_photo"] or item["photo"]
    old_photo = item["photo"]

    if skip_crop.strip() == "1":
        # Undo crop — revert to full source image
        new_photo = source
    elif crop_y_min.strip() and crop_x_min.strip() and crop_y_max.strip() and crop_x_max.strip():
        bbox = (int(crop_y_min), int(crop_x_min), int(crop_y_max), int(crop_x_max))
        new_photo = crop_photo(tenant_id, source, bbox)
    else:
        # No change
        return RedirectResponse(f"/boxes/{item['box_id']}#item-{item_id}", status_code=303)

    try:
        dao_items.apply_recrop(actor, item_id, new_photo, source)
    except (NotFoundError, ForbiddenError):
        raise HTTPException(404)
    # Old crop file may now be orphaned
    if old_photo and old_photo != new_photo and old_photo != source:
        with db() as conn:
            _delete_upload_if_orphan(conn, tenant_id, old_photo)
    return RedirectResponse(f"/boxes/{item['box_id']}#item-{item_id}", status_code=303)


@app.post("/items/{item_id}/replace-photo")
async def replace_item_photo(request: Request, item_id: int, photo: UploadFile = File(...)):
    if not photo or not photo.filename:
        raise HTTPException(400, "Photo required")
    actor: Actor = request.state.actor
    try:
        item = dao_items.get_for_recrop(actor, item_id)
    except NotFoundError:
        raise HTTPException(404)
    tenant_id = item["tenant_id"]
    new_photo = save_photo(tenant_id, photo)
    try:
        result = dao_items.replace_photo(actor, item_id, new_photo)
    except (NotFoundError, ForbiddenError):
        raise HTTPException(404)
    with db() as conn:
        for old in {result["old_photo"], result["old_source"]}:
            if old:
                _delete_upload_if_orphan(conn, tenant_id, old)
    return RedirectResponse(f"/boxes/{result['box_id']}#item-{item_id}", status_code=303)


@app.post("/items/{item_id}/delete")
def delete_item(request: Request, item_id: int):
    actor: Actor = request.state.actor
    try:
        result = dao_items.delete(actor, item_id)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    with db() as conn:
        for photo_name in {result["photo"], result["source_photo"]}:
            if photo_name:
                _delete_upload_if_orphan(conn, actor.tenant_id, photo_name)
    return RedirectResponse(f"/boxes/{result['box_id']}", status_code=303)


_EXT_TO_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def _bytes_for_vision(tenant_id: int, photo_name: str) -> bytes:
    """Re-encode the saved photo into a JPEG with no EXIF segment before
    sending it to the vision model.

    save_photo_bytes already runs ImageOps.exif_transpose, but Gemini's
    behaviour with EXIF is undocumented — when a phone photo carries an
    orientation tag, the browser auto-rotates while Gemini decoded the raw
    landscape pixels and returned bboxes in that un-rotated coordinate
    space, so the overlay rendered 90° off from what the user sees.
    Stripping the EXIF segment entirely (info.pop + a fresh save without
    the `exif=` kwarg) means there's nothing left for the decoder to
    interpret either way: pixel orientation is the only source of truth.

    Falls back to raw plaintext bytes if PIL can't decode the file (test
    fixtures sometimes use synthetic JPEG headers PIL refuses)."""
    plaintext = _read_encrypted(tenant_id, photo_name)
    try:
        from PIL import Image, ImageOps
        import io as _io
        with Image.open(_io.BytesIO(plaintext)) as opened:
            rotated = ImageOps.exif_transpose(opened)
        if rotated.mode not in ("RGB", "L"):
            rotated = rotated.convert("RGB")
        rotated.info.pop("exif", None)
        buf = _io.BytesIO()
        rotated.save(buf, format="JPEG", quality=JPEG_QUALITY)
        return buf.getvalue()
    except Exception:
        return plaintext


import threading as _threading
# Process-global ingest concurrency cap.  Each upload spawns a
# BackgroundTask per photo; on a small VM N parallel Gemini calls
# can saturate CPU, blow through API rate limits, or — in the user-
# observed case — produce duplicate pending_items as multiple
# workers race to detect items in the same photo (see fix(ingest):
# restrict retry).  Default 1 = strict serialization, which keeps a
# tiny droplet healthy.  Operators with headroom can raise it via
# ``STASH_INGEST_CONCURRENCY``.
_INGEST_SEMAPHORE = _threading.BoundedSemaphore(
    max(1, int(os.environ.get("STASH_INGEST_CONCURRENCY", "1"))),
)


def process_ingest_job(job_id: int, photo_name: str, tenant_id: int) -> None:
    """Background worker: vision pass → insert pending items → mark job done.
    Runs after the request scope closes, so it routes through the no-actor
    DAO entry points instead of going through Actor-gated mutations.

    If the originating /ingest request stamped a packing-session
    ``target_box_id`` onto the job, every pending_item we create gets
    ``suggested_box_id = target_box_id`` so the sort UI pre-fills
    the box selection (very similar to the AI suggest path).  We
    re-read it from the row (rather than threading another param)
    so the retry handler doesn't have to re-discover state.

    Acquires ``_INGEST_SEMAPHORE`` before doing real work — while
    waiting the row stays in 'pending' status (mark_processing
    fires after acquire) so the UI shows "Queued" rather than
    "Looking…", which is honest about the state."""
    _log = obs.get_logger("stash.ingest")
    _log.info("ingest.worker.queued job_id=%s tenant_id=%s photo=%s",
              job_id, tenant_id, photo_name)
    with _INGEST_SEMAPHORE:
        _process_ingest_job_locked(job_id, photo_name, tenant_id, _log)


def _process_ingest_job_locked(
    job_id: int, photo_name: str, tenant_id: int, _log,
) -> None:
    _log.info("ingest.worker.start job_id=%s tenant_id=%s photo=%s",
              job_id, tenant_id, photo_name)
    dao_ingest_jobs.mark_processing(job_id)
    try:
        # Re-reading the hint must live inside the try so a missing-
        # column or other DB hiccup gets recorded as a failed job
        # rather than wedging the row at "processing" forever (the
        # symptom: photo uploaded, sits spinning, no logs after
        # ``ingest.worker.start``).
        target_box_id = dao_ingest_jobs.get_target_box_id(job_id)
        scope = dao_ingest_jobs.get_scope(job_id)
        image_bytes = _bytes_for_vision(tenant_id, photo_name)
        _log.info("ingest.worker.vision job_id=%s bytes=%s scope=%s",
                  job_id, len(image_bytes), scope)
        detected = vision.detect_items(
            image_bytes, media_type="image/jpeg", scope=scope,
        )
        _log.info("ingest.worker.vision_done job_id=%s items=%s",
                  job_id, len(detected))
        dao_usage.record(tenant_id, "ai", "gemini_detect")
        for item in detected:
            bbox = item.bbox or [None, None, None, None]
            dao_ingest_jobs.insert_pending_item(
                tenant_id,
                name=item.name,
                description=item.description,
                photo=photo_name,
                bbox=tuple(bbox),
                suggested_box_id=target_box_id,
            )
        dao_ingest_jobs.mark_done(job_id, len(detected))
        _log.info("ingest.worker.done job_id=%s items=%s",
                  job_id, len(detected))
    except Exception as e:
        _log.exception("ingest.worker.failed job_id=%s", job_id)
        try:
            dao_ingest_jobs.mark_failed(job_id, str(e))
        except Exception:
            _log.exception("ingest.worker.mark_failed_also_failed job_id=%s",
                           job_id)


@app.get("/ingest", response_class=HTMLResponse)
def ingest_form(request: Request):
    """Ingest page.  The packing-session "I'm packing Box X" picker
    is a plain ``<select>`` on the form — leaving the page or
    reloading drops the selection (state is inherent to the UI),
    which keeps the model from drifting into stale-session bugs."""
    actor: Actor = request.state.actor
    jobs = dao_ingest_jobs.list_active(actor)
    fp = dao_ingest_jobs.fingerprint(actor)
    boxes = dao_boxes.list_for_picker(actor) if actor.tenant_id else []
    return templates.TemplateResponse(
        request, "ingest.html",
        {
            "jobs": jobs,
            "fingerprint": fp["fingerprint"],
            "boxes": boxes,
        },
    )


@app.get("/ingest/state")
def ingest_state(request: Request):
    """Lightweight poll target so the ingest page can update its job list
    without a full meta-refresh — that one was nuking in-progress file
    picker selections and cancelling uploads mid-stream."""
    actor: Actor = request.state.actor
    return dao_ingest_jobs.fingerprint(actor)


@app.get("/ingest/jobs", response_class=HTMLResponse)
def ingest_jobs_fragment(request: Request):
    """HTML fragment of just the jobs list — the ingest page swaps this in
    when its fingerprint changes."""
    actor: Actor = request.state.actor
    return templates.TemplateResponse(
        request, "_ingest_jobs.html",
        {"jobs": dao_ingest_jobs.list_active(actor)},
    )


@app.post("/ingest")
async def ingest(
    request: Request,
    background_tasks: BackgroundTasks,
    photos: list[UploadFile] = File(...),
    target_box_id: str = Form(""),
    scope: str = Form("auto"),
):
    """Photo upload + worker dispatch.  ``target_box_id`` is the
    packing-session hint from the box picker — empty string means
    "no session", otherwise the integer is validated against the
    actor's tenant inside ``dao_ingest_jobs.create`` and a forged
    cross-tenant id silently degrades to no hint (crash toward
    happy path).  ``scope`` ('auto' / 'single' / 'many') tunes the
    Gemini prompt: 'single' fixes the "took a photo of one thing,
    AI returned a dozen items" failure mode."""
    valid = [p for p in photos if p and p.filename]
    if not valid:
        raise HTTPException(400, "Photo required")
    actor: Actor = request.state.actor

    target_id: int | None
    try:
        target_id = int(target_box_id) if target_box_id.strip() else None
    except ValueError:
        target_id = None
    scope_clean = (scope or "auto").strip().lower()
    if scope_clean not in ("auto", "single", "many"):
        scope_clean = "auto"

    # Pre-flight AI quota check — block obvious overages here
    # rather than burning the encode + per-photo background-job
    # spawn before rejecting.  Approximate cost: one gemini_detect
    # per photo.
    detect_cost = dao_usage._cost_for("ai", "gemini_detect", 1)
    try:
        dao_quotas.check_or_raise(
            actor.tenant_id, "ai",
            units_about_to_record=len(valid),
            cost_about_to_record=detect_cost * len(valid),
        )
    except dao_quotas.QuotaExceeded as exc:
        raise HTTPException(
            429,
            f"AI quota exceeded ({exc.key}={exc.used} > {exc.cap}).  "
            "Resets at the start of the next window.",
        )

    for photo in valid:
        image_bytes = await photo.read()
        photo_name = save_photo_bytes(actor.tenant_id, image_bytes, photo.filename)
        try:
            job_id = dao_ingest_jobs.create(
                actor, photo_name,
                target_box_id=target_id,
                scope=scope_clean,
            )
        except ForbiddenError:
            raise HTTPException(403)
        background_tasks.add_task(
            process_ingest_job, job_id, photo_name, actor.tenant_id,
        )

    return RedirectResponse("/ingest", status_code=303)


@app.post("/ingest/{job_id}/retry")
def ingest_retry(request: Request, background_tasks: BackgroundTasks, job_id: int):
    actor: Actor = request.state.actor
    try:
        row = dao_ingest_jobs.get_for_retry(actor, job_id)
    except NotFoundError:
        raise HTTPException(404, "Job not found or not failed")
    except ForbiddenError:
        raise HTTPException(403)
    dao_ingest_jobs.reset_to_pending(actor, job_id)
    background_tasks.add_task(
        process_ingest_job, job_id, row["photo"], row["tenant_id"] or actor.tenant_id,
    )
    return RedirectResponse("/ingest", status_code=303)


@app.post("/ingest/{job_id}/dismiss")
def ingest_dismiss(request: Request, job_id: int):
    actor: Actor = request.state.actor
    try:
        dao_ingest_jobs.dismiss(actor, job_id)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    return RedirectResponse("/ingest", status_code=303)


def _queue_view_context(actor: Actor) -> dict:
    """Load the data needed to render the queue page (or just its cards
    fragment).  Both /queue and /queue/items share the same shape so a
    real-time refresh produces markup identical to the SSR path."""
    pending = dao_pending.list_for_queue(actor)
    boxes = dao_boxes.list_for_picker(actor)
    all_tags = dao_tags.list_names(actor)

    # Group boxes for the <optgroup>'d picker: "Location · Room", or just
    # "Room" if no location, or "Unassigned" for boxes with neither.
    boxes_grouped: list[tuple[str, list]] = []
    current_label = object()
    current_group: list = []
    for b in boxes:
        if b["location_name"] and b["room_name"]:
            label = f"{b['location_name']} · {b['room_name']}"
        elif b["room_name"]:
            label = b["room_name"]
        elif b["location"]:  # legacy free-text fallback
            label = b["location"]
        else:
            label = "Unassigned"
        if label != current_label:
            current_group = []
            boxes_grouped.append((label, current_group))
            current_label = label
        current_group.append(b)

    return {
        "pending": pending,
        "boxes": boxes,
        "boxes_grouped": boxes_grouped,
        "fingerprint": dao_pending.fingerprint(actor),
        "all_tags": all_tags,
    }


@app.get("/queue", response_class=HTMLResponse)
def queue(request: Request):
    actor: Actor = request.state.actor
    return templates.TemplateResponse(request, "queue.html", _queue_view_context(actor))


@app.get("/queue/items", response_class=HTMLResponse)
def queue_items_fragment(request: Request):
    """HTML fragment of the pending-item cards only.

    The queue page polls this on /queue/state fingerprint changes and
    splices new cards in (and prunes vanished ones) without a full
    reload — earlier the page hard-reloaded on every fingerprint flip,
    which kept eating in-flight edits whenever a background ingest job
    finished or another tab touched the queue."""
    actor: Actor = request.state.actor
    return templates.TemplateResponse(
        request, "_queue_cards.html", _queue_view_context(actor),
    )


@app.post("/queue/{pending_id}/match")
def queue_match(request: Request, pending_id: int):
    actor: Actor = request.state.actor
    try:
        row = dao_pending.get_by_id(actor, pending_id)
    except NotFoundError:
        raise HTTPException(404)
    boxes = dao_boxes.list_for_picker(actor)

    try:
        dao_quotas.check_or_raise(
            actor.tenant_id, "ai",
            cost_about_to_record=dao_usage._cost_for(
                "ai", "anthropic_match", 1,
            ),
        )
    except dao_quotas.QuotaExceeded as exc:
        raise HTTPException(
            429,
            f"AI quota exceeded ({exc.key}={exc.used} > {exc.cap}).",
        )
    suggestion = vision.suggest_box(row["name"], row["description"] or "", boxes)
    dao_usage.record(actor.tenant_id, "ai", "anthropic_match")

    try:
        dao_pending.update_suggestion(
            actor, pending_id,
            suggested_box_id=suggestion.box_id if suggestion.match == "existing" else None,
            suggested_new_box_name=suggestion.new_box_name if suggestion.match == "new" else None,
            suggested_new_box_location=suggestion.new_box_location if suggestion.match == "new" else None,
            suggestion_reason=suggestion.reason,
        )
    except (NotFoundError, ForbiddenError):
        raise HTTPException(404)
    return RedirectResponse("/queue", status_code=303)


@app.post("/queue/{pending_id}/assign")
def queue_assign(
    request: Request,
    pending_id: int,
    box_id: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    tags: str = Form(""),
    crop_y_min: str = Form(""),
    crop_x_min: str = Form(""),
    crop_y_max: str = Form(""),
    crop_x_max: str = Form(""),
    skip_crop: str = Form(""),
):
    if not box_id.strip():
        raise HTTPException(400, "Pick a box")
    if not name.strip():
        raise HTTPException(400, "Name required")
    actor: Actor = request.state.actor

    try:
        row = dao_pending.get_for_assign(actor, pending_id)
    except NotFoundError:
        raise HTTPException(404)
    tenant_id = row["tenant_id"]
    target_box_id = int(box_id)
    try:
        dao_boxes.get_by_id(actor, target_box_id)
    except NotFoundError:
        raise HTTPException(400, "Unknown box")

    # Use manual crop coords if submitted, fall back to DB bbox, skip if cleared
    source_photo = row["photo"]
    photo = source_photo
    if skip_crop.strip() != "1":
        if crop_y_min.strip() and crop_x_min.strip() and crop_y_max.strip() and crop_x_max.strip():
            bbox = (int(crop_y_min), int(crop_x_min), int(crop_y_max), int(crop_x_max))
            photo = crop_photo(tenant_id, photo, bbox)
        elif photo and row["bbox_y_min"] is not None:
            bbox = (row["bbox_y_min"], row["bbox_x_min"], row["bbox_y_max"], row["bbox_x_max"])
            photo = crop_photo(tenant_id, photo, bbox)

    new_item_id = dao_items.create(
        actor, target_box_id,
        name=name, notes=description, photo=photo, source_photo=source_photo,
    )
    # Transfer tags from pending to the real item.  This is the one
    # cross-table copy that doesn't fit any single DAO method cleanly,
    # so we keep the SELECT/INSERT inline — both filters scope to the
    # actor's tenant.
    with db() as conn:
        conn.execute(
            "INSERT INTO item_tags (item_id, tag_id, value, tenant_id) "
            "SELECT ?, tag_id, value, ? FROM pending_item_tags "
            "WHERE pending_item_id = ? AND tenant_id = ?",
            (new_item_id, tenant_id, pending_id, tenant_id),
        )
        conn.commit()
    if tags.strip():
        dao_tags.attach_to_item(actor, new_item_id, parse_tag_input(tags))
    dao_pending.delete(actor, pending_id)
    return RedirectResponse("/queue", status_code=303)


@app.get("/queue/state")
def queue_state(request: Request):
    """Fingerprint for real-time polling — changes whenever queue content changes."""
    actor: Actor = request.state.actor
    return {"fingerprint": dao_pending.fingerprint(actor)}


@app.post("/queue/{pending_id}/delete")
def queue_delete(request: Request, pending_id: int):
    actor: Actor = request.state.actor
    try:
        result = dao_pending.delete(actor, pending_id)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    if result["photo"]:
        with db() as conn:
            _delete_upload_if_orphan(conn, actor.tenant_id, result["photo"])
    return RedirectResponse("/queue", status_code=303)


SEARCH_PAGE_SIZE = 100


def _coerce_search_int(value: str) -> int | None:
    if not value or value in ("none", "0", "all"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _build_search_query(
    actor: Actor,
    q: str, tag: str, location_id: int | None, room_id: int | None,
    box_id: int | None, missing: bool, has_photo: bool,
) -> tuple[str, list]:
    """Compose the WHERE clause + params for the search query. Pulled out so
    both the listing query and the count/facet queries can share it.
    Always pins on i.tenant_id so a tenant can't see another's items
    even when they brute-force filter ids in the URL."""
    clauses = ["i.tenant_id = ?"]
    params: list = [actor.tenant_id]
    if q.strip():
        like = f"%{q.strip()}%"
        clauses.append("(i.name LIKE ? OR i.notes LIKE ?)")
        params.extend([like, like])
    if tag.strip():
        clauses.append(
            "i.id IN (SELECT it.item_id FROM item_tags it "
            "JOIN tags t ON t.id = it.tag_id "
            "WHERE t.name = ? AND it.tenant_id = ?)"
        )
        params.extend([tag.strip(), actor.tenant_id])
    if box_id is not None:
        clauses.append("b.id = ?")
        params.append(box_id)
    if room_id is not None:
        clauses.append("r.id = ?")
        params.append(room_id)
    if location_id is not None:
        clauses.append("l.id = ?")
        params.append(location_id)
    if missing:
        clauses.append("i.is_missing = 1")
    if has_photo:
        clauses.append("i.photo IS NOT NULL AND i.photo != ''")
    return " AND ".join(clauses), params


@app.get("/search", response_class=HTMLResponse)
def search(
    request: Request,
    q: str = "",
    tag: str = "",
    location_id: str = "",
    room_id: str = "",
    box_id: str = "",
    missing: str = "",
    has_photo: str = "",
    offset: int = 0,
):
    """Faceted search across all items.

    Supports filtering by free text, tag, location/room/box, missing-flag,
    and has-photo. Results are grouped by box and paginated server-side
    (LIMIT/OFFSET) so a stash with thousands of items still renders.
    Honors Accept: application/json so the client can use it as the API
    behind a "load more" button without re-rendering the chrome.
    """
    loc_id = _coerce_search_int(location_id)
    rm_id = _coerce_search_int(room_id)
    bx_id = _coerce_search_int(box_id)
    is_missing = bool(missing)
    has_photo_flag = bool(has_photo)
    offset = max(0, offset)
    actor: Actor = request.state.actor

    all_tags = dao_tags.list_names(actor)
    with db() as conn:
        all_locations = [
            dict(r) for r in conn.execute(
                "SELECT id, name FROM locations WHERE tenant_id = ? ORDER BY name",
                (actor.tenant_id,),
            ).fetchall()
        ]
        all_rooms = dao_rooms.list_for_picker(actor)
        all_boxes = [
            dict(r) for r in conn.execute(
                "SELECT b.id, b.name, "
                "       l.id AS location_id, l.name AS location_name, "
                "       r.id AS room_id, r.name AS room_name "
                "FROM boxes b "
                "LEFT JOIN rooms r ON r.id = b.room_id "
                "LEFT JOIN locations l ON l.id = r.location_id "
                "WHERE b.tenant_id = ? "
                "ORDER BY l.name IS NULL, l.name, r.name, b.name",
                (actor.tenant_id,),
            ).fetchall()
        ]

        where, params = _build_search_query(
            actor,
            q, tag, loc_id, rm_id, bx_id, is_missing, has_photo_flag,
        )
        common_join = (
            "FROM items i "
            "JOIN boxes b ON b.id = i.box_id "
            "LEFT JOIN rooms r ON r.id = b.room_id "
            "LEFT JOIN locations l ON l.id = r.location_id "
        )

        # Headline counts so the user knows the result size before pagination.
        totals = conn.execute(
            f"SELECT COUNT(DISTINCT i.id) AS items, COUNT(DISTINCT b.id) AS boxes "
            f"{common_join}WHERE {where}",
            params,
        ).fetchone()
        total_items = totals["items"]
        total_boxes = totals["boxes"]

        rows = conn.execute(
            f"SELECT i.id, i.name, i.notes, i.photo, i.is_missing, "
            f"       b.id AS box_id, b.name AS box_name, b.color AS box_color, "
            f"       r.id AS room_id, r.name AS room_name, r.color AS room_color, "
            f"       l.id AS location_id, l.name AS location_name "
            f"{common_join}WHERE {where} "
            f"ORDER BY l.name IS NULL, l.name, r.name, b.name, i.name "
            f"LIMIT ? OFFSET ?",
            [*params, SEARCH_PAGE_SIZE, offset],
        ).fetchall()

        # Pull tags per item in a single pass so the row template doesn't
        # round-trip per item.
        item_ids = [r["id"] for r in rows]
        tags_by_item: dict[int, list[dict]] = {}
        if item_ids:
            placeholders = ",".join("?" * len(item_ids))
            for r in conn.execute(
                f"SELECT it.item_id, t.id AS tag_id, t.name, it.value "
                f"FROM item_tags it JOIN tags t ON t.id = it.tag_id "
                f"WHERE it.item_id IN ({placeholders}) AND it.tenant_id = ? "
                f"ORDER BY t.name",
                [*item_ids, actor.tenant_id],
            ).fetchall():
                tags_by_item.setdefault(r["item_id"], []).append(dict(r))

    # Group server-side so the template renders one section per box without
    # a stateful loop in Jinja. Boxes are already ordered by location/room
    # in the SQL, so we just bucket on transitions.
    groups: list[dict] = []
    current_box_id = None
    for r in rows:
        if r["box_id"] != current_box_id:
            groups.append({
                "box_id": r["box_id"],
                "box_name": r["box_name"],
                "box_color": r["box_color"],
                "room_name": r["room_name"],
                "room_color": r["room_color"],
                "location_name": r["location_name"],
                "items": [],
            })
            current_box_id = r["box_id"]
        groups[-1]["items"].append({
            **dict(r),
            "tags": tags_by_item.get(r["id"], []),
        })

    page_loaded = offset + len(rows)
    has_more = page_loaded < total_items

    if "application/json" in request.headers.get("accept", ""):
        # JSON response for the "Load more" path — just the new groups +
        # whether there's more behind them. Wrap in JSONResponse so the
        # endpoint's response_class=HTMLResponse default doesn't try to
        # render the dict as HTML.
        from fastapi.responses import JSONResponse
        return JSONResponse({
            "groups": groups,
            "total_items": total_items,
            "total_boxes": total_boxes,
            "page_loaded": page_loaded,
            "has_more": has_more,
            "offset": offset,
        })

    # Reflect the current filter state so the template can render filter
    # chips and pre-select dropdowns.
    return templates.TemplateResponse(
        request, "search.html",
        {
            "q": q,
            "tag": tag,
            "location_id": loc_id,
            "room_id": rm_id,
            "box_id": bx_id,
            "missing": is_missing,
            "has_photo": has_photo_flag,
            "groups": groups,
            "total_items": total_items,
            "total_boxes": total_boxes,
            "page_loaded": page_loaded,
            "has_more": has_more,
            "page_size": SEARCH_PAGE_SIZE,
            "all_tags": all_tags,
            "all_locations": all_locations,
            "all_rooms": all_rooms,
            "all_boxes": all_boxes,
        },
    )


@app.get("/tags", response_class=HTMLResponse)
def tags_page(request: Request):
    actor: Actor = request.state.actor
    # Rich-distribution view per tag (item count + which rooms +
    # which locations the tagged items live in) — feedback #20
    # asked the /tags landing to actually carry weight instead of
    # being a flat name list.  list_with_distribution does the
    # JOIN + aggregation in one pass.
    tags = dao_tags.list_with_distribution(actor)
    return templates.TemplateResponse(
        request, "tags.html", {"tags": tags},
    )


@app.get("/tags/autocomplete")
def tags_autocomplete(request: Request, q: str = ""):
    actor: Actor = request.state.actor
    names = dao_tags.list_names(actor)
    if q:
        ql = q.lower()
        # Same prefix-match LIKE 'q%' semantic, capped at 20.
        return [n for n in names if n.lower().startswith(ql)][:20]
    return names[:50]


def _box_art_bytes(box_row) -> bytes | None:
    """Decrypt a box's background art (if any) for embedding in a label.
    Returns None when there's no art configured or the file is missing /
    won't decrypt (rotation gone wrong, etc.) — labels still render
    cleanly without art."""
    art = box_row["background_art"] if "background_art" in box_row.keys() else None
    tenant_id = box_row["tenant_id"] if "tenant_id" in box_row.keys() else None
    if not art or tenant_id is None:
        return None
    p = _tenant_file(tenant_id, art)
    if not p.exists():
        return None
    try:
        return _decrypt_for(tenant_id, p.read_bytes())
    except Exception:
        return None


def _attach_art_bytes(box_dict: dict) -> dict:
    """Mutate a box dict in place to add `art_bytes` for the label
    renderers, then return it for chaining."""
    box_dict["art_bytes"] = _box_art_bytes(box_dict)
    return box_dict


def _resolve_room_tint(request: Request) -> bool:
    """Read the ``?colors=room`` toggle off the query string.
    Anything else (default, missing, ``none``) means "no tint" so
    the /labels page can always link out with a clean URL when the
    checkbox is off."""
    return request.query_params.get("colors", "").lower() == "room"


@app.get("/boxes/{box_id}/label.svg")
def box_label_svg(request: Request, box_id: int):
    """Single-cell label SVG.  Respects the box's persisted
    ``label_orientation`` and the format query param so the
    /labels grid thumbnails match the print preview."""
    actor: Actor = request.state.actor
    try:
        box = dao_boxes.get_by_id(actor, box_id)
    except NotFoundError:
        raise HTTPException(404)
    fmt = _resolve_label_format(request)
    orientation = box.get("label_orientation") or "landscape"
    color_tint = None
    if _resolve_room_tint(request):
        color_tint = box.get("color") or box.get("room_color")
    svg = labels.render_label_svg(
        box["id"], box["name"], box["notes"] or "", PUBLIC_URL,
        background_art=_box_art_bytes(box),
        fmt=fmt, orientation=orientation, color_tint=color_tint,
    )
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Content-Disposition": f'attachment; filename="box-{box_id}-label.svg"'},
    )


def _selected_boxes(conn, actor: Actor, box_ids_raw: list[str]) -> list:
    """Return rows for the selected boxes, or all boxes in the actor's
    tenant if no selection given.  Ordering matches the labels page
    (alpha) so printed sheets are predictable.

    Pulls ``label_orientation`` so the print + PDF paths render
    each box at the orientation the user chose on /labels —
    without it, ``_label_group`` would fall back to landscape
    for every box.  Joins ``rooms`` to surface the room's colour
    so the room-tint toggle on /labels can paint each label its
    room's hue (with the per-box ``color`` overriding when set).
    """
    cols = (
        "b.id, b.name, b.notes, b.background_art, b.label_orientation, "
        "b.tenant_id, b.color, r.color AS room_color"
    )
    if box_ids_raw:
        placeholders = ",".join("?" * len(box_ids_raw))
        return conn.execute(
            f"SELECT {cols} FROM boxes b "
            f"LEFT JOIN rooms r ON r.id = b.room_id "
            f"WHERE b.id IN ({placeholders}) AND b.tenant_id = ? "
            f"ORDER BY b.name",
            [*[int(b) for b in box_ids_raw], actor.tenant_id],
        ).fetchall()
    return conn.execute(
        f"SELECT {cols} FROM boxes b "
        f"LEFT JOIN rooms r ON r.id = b.room_id "
        f"WHERE b.tenant_id = ? ORDER BY b.name",
        (actor.tenant_id,),
    ).fetchall()


def _attach_color_tint(box_dict: dict, *, enabled: bool) -> dict:
    """When room-tinting is enabled, resolve the effective hex on
    each box and set ``color_tint`` for the label renderer.
    Resolution order: per-box ``color`` override, then the room's
    ``color``, else nothing.  Off → strip any tint so a stale
    field can't sneak into the SVG."""
    if not enabled:
        box_dict["color_tint"] = None
        return box_dict
    box_dict["color_tint"] = (
        box_dict.get("color") or box_dict.get("room_color") or None
    )
    return box_dict


def _resolve_label_format(request: Request) -> labels.AveryFormat:
    """Pull ``?format=…`` off the query string and resolve via
    the registry; falls back to the default if missing or
    unknown.  Used by every label route so the format choice is
    honoured uniformly."""
    return labels.get_format(request.query_params.get("format"))


_LABEL_COPIES_MAX = 4


def _resolve_label_copies(request: Request) -> int:
    """How many duplicates of each selected box to lay out on the
    sheet.  User wraps tape around the box, so a single label gets
    lost on three of four sides — 2-4 copies covers a box visibly
    from any angle.  Clamped [1, 4] so a forged ``?copies=9999``
    can't blow up the page count."""
    raw = request.query_params.get("copies")
    if not raw:
        return 1
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, min(_LABEL_COPIES_MAX, n))


def _expand_copies(boxes: list, copies: int) -> list:
    """Duplicate each box ``copies`` times in place.  Used by the
    sheet + PDF + print paths so one logical "selection" of N
    boxes lays out as N × copies cells on the printed sheet."""
    if copies <= 1:
        return boxes
    out: list = []
    for b in boxes:
        out.extend([b] * copies)
    return out


@app.get("/labels", response_class=HTMLResponse)
def labels_page(request: Request):
    """Per-tenant label-print surface.  Avery shipping-label
    pivot — drop the PDF in the printer, hit print, done.  The
    Cricut SVG round-trip is gone; we target Avery 5523 / 5160 /
    5164 directly.

    Boxes are bucketed by (location, room) so a stash with 80
    boxes reads as a navigable list of sections instead of one
    undifferentiated grid — same grouping rules as the home page,
    but no item counts (the labels page only cares about printable
    boxes)."""
    actor: Actor = request.state.actor
    fmt = _resolve_label_format(request)
    use_room_tint = _resolve_room_tint(request)
    copies = _resolve_label_copies(request)
    with db() as conn:
        # Mirrors dao_boxes.list_with_counts' join + ordering so the
        # `_group_boxes_for_index` bucketing loop walks the rows in
        # the right order without us repeating the comparator here.
        boxes = conn.execute(
            "SELECT b.*, "
            "       r.name AS room_name, r.color AS room_color, "
            "       l.id AS location_id, l.name AS location_name "
            "FROM boxes b "
            "LEFT JOIN rooms r ON r.id = b.room_id "
            "LEFT JOIN locations l ON l.id = r.location_id "
            "WHERE b.tenant_id = ? "
            "ORDER BY "
            "  CASE "
            "    WHEN r.id IS NOT NULL THEN 0 "
            "    WHEN b.location IS NOT NULL AND TRIM(b.location) != '' THEN 1 "
            "    ELSE 2 "
            "  END, "
            "  COALESCE(l.name, '') COLLATE NOCASE, "
            "  COALESCE(r.name, '') COLLATE NOCASE, "
            "  COALESCE(b.location, '') COLLATE NOCASE, "
            "  b.name COLLATE NOCASE",
            (actor.tenant_id,),
        ).fetchall()
    boxes_d = [dict(b) for b in boxes]
    box_groups = _group_boxes_for_index(boxes_d)
    return templates.TemplateResponse(
        request, "labels.html",
        {
            "boxes": boxes_d,
            "box_groups": box_groups,
            "fmt": fmt,
            "all_formats": list(labels.AVERY_FORMATS.values()),
            "labels_per_page": fmt.labels_per_page,
            "art_enabled": _ai_art_enabled(),
            "use_room_tint": use_room_tint,
            "copies": copies,
            "copies_max": _LABEL_COPIES_MAX,
        },
    )


@app.post("/boxes/{box_id}/label-orientation")
def set_box_label_orientation(
    request: Request,
    box_id: int,
    orientation: str = Form(...),
):
    """Persist a box's label orientation.  Called via fetch from
    the /labels page when the user toggles the L/P pill.  Returns
    JSON for the AJAX path; falls back to a redirect for the
    no-JS path."""
    actor: Actor = request.state.actor
    try:
        dao_boxes.set_label_orientation(actor, box_id, orientation)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if "application/json" in request.headers.get("accept", ""):
        return {"ok": True, "box_id": box_id, "orientation": orientation}
    return RedirectResponse(
        request.headers.get("referer") or "/labels", status_code=303,
    )


@app.get("/labels/sheet.pdf")
def labels_sheet_pdf(request: Request):
    """Multi-page vector PDF — one Avery sheet per page.
    *Primary* output path: drop the Avery sheet in the printer,
    open the PDF, hit print.  Sharp because QR + text are vector,
    not rasterised."""
    actor: Actor = request.state.actor
    fmt = _resolve_label_format(request)
    use_room_tint = _resolve_room_tint(request)
    copies = _resolve_label_copies(request)
    box_ids_raw = request.query_params.getlist("box_ids")
    with db() as conn:
        boxes = _selected_boxes(conn, actor, box_ids_raw)
    payload = _expand_copies([
        _attach_color_tint(_attach_art_bytes(dict(b)), enabled=use_room_tint)
        for b in boxes
    ], copies)
    try:
        pdf_bytes = labels.render_sheet_pdf(payload, PUBLIC_URL, fmt=fmt)
    except ImportError:
        raise HTTPException(
            501, "PDF export requires cairosvg + pypdf — install them or "
                 "use the Print button instead.",
        )
    filename = f"stash-labels-{fmt.sku}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@app.get("/labels/print", response_class=HTMLResponse)
def labels_print(request: Request):
    """Browser-printable preview, paginated via CSS so Cmd/Ctrl+P
    produces real multi-page output.  No PDF dep needed.  Each
    sheet sits in its own page-break div so a single Print
    dialog covers the whole batch."""
    actor: Actor = request.state.actor
    fmt = _resolve_label_format(request)
    use_room_tint = _resolve_room_tint(request)
    copies = _resolve_label_copies(request)
    box_ids_raw = request.query_params.getlist("box_ids")
    with db() as conn:
        boxes = _expand_copies([
            _attach_color_tint(
                _attach_art_bytes(dict(b)), enabled=use_room_tint,
            )
            for b in _selected_boxes(conn, actor, box_ids_raw)
        ], copies)
    pages = []
    for chunk_start in range(0, max(len(boxes), 1), fmt.labels_per_page):
        chunk = boxes[chunk_start:chunk_start + fmt.labels_per_page]
        pages.append(labels.render_single_sheet_svg(chunk, PUBLIC_URL, fmt=fmt))
    # Carry the same query string back to /labels so the Back link
    # restores the selection the user just printed.  sessionStorage
    # alone doesn't survive ``formtarget="_blank"`` (the new tab is
    # its own top-level browsing context and gets a fresh storage
    # area), but URL params do — the /labels JS picks ``box_ids``
    # off the query string and re-checks the right boxes.
    qs = request.url.query
    back_url = f"/labels?{qs}" if qs else "/labels"
    return templates.TemplateResponse(
        request, "labels_print.html",
        {
            "sheet_svgs": pages,
            "label_count": len(boxes),
            "fmt": fmt,
            "back_url": back_url,
        },
    )


def _wants_json(request: Request) -> bool:
    """The labels page calls these endpoints from fetch() with Accept: application/json
    so it can re-render in place; the form-post fallback gets a 303 redirect."""
    return "application/json" in request.headers.get("accept", "")


def _safe_internal_redirect(target: str, default: str = "/labels") -> str:
    """Reject open-redirect targets — only honor relative paths beneath this app."""
    if not target.startswith("/") or target.startswith("//"):
        return default
    return target


def _ai_art_enabled() -> bool:
    """AI art generation is available when GEMINI_API_KEY is set.
    Per-tenant gating happens through the ``monthly_ai_art_calls``
    quota (Free: 0, Pro: 5) so a fresh free tenant gets a clean
    "upgrade to use this" message rather than the feature being
    hidden entirely — the marketing surface stays honest about
    what Pro unlocks."""
    return bool(os.environ.get("GEMINI_API_KEY"))


@app.post("/boxes/{box_id}/generate-art")
def generate_box_art(
    request: Request,
    box_id: int,
    next_url: str = Form("/labels"),
):
    """Synchronously generate label background art via Nano Banana 2.

    Synchronous because the user is waiting on it from the labels page; the
    model takes ~10-20s. Each call independently grabs items + a few photos
    so the prompt is grounded in actual contents instead of just the box
    name. The old image, if any, is cleaned up only after the new one writes
    successfully so a failed generation doesn't strand the box without art."""
    if not _ai_art_enabled():
        # No Gemini key configured at all — the surface can't run.
        # 503 (not 404) so a misconfigured operator can spot the
        # missing env var in the logs rather than think the route
        # is gone.
        raise HTTPException(
            503,
            "AI art generation requires GEMINI_API_KEY on the "
            "deployment.",
        )
    actor: Actor = request.state.actor
    try:
        box = dao_boxes.get_by_id(actor, box_id)
    except NotFoundError:
        raise HTTPException(404)
    tenant_id = box["tenant_id"]
    try:
        dao_quotas.check_or_raise(
            actor.tenant_id, "ai_art",
            units_about_to_record=1,
            cost_about_to_record=dao_usage._cost_for(
                "ai", "gemini_art", 1,
            ),
        )
    except dao_quotas.QuotaExceeded as exc:
        # Tailored 429 copy based on which cap fired — over the
        # monthly art budget vs over the daily cost ceiling get
        # different "what to do" guidance.
        if exc.key == "monthly_ai_art_calls":
            msg = (
                f"You've used all {exc.cap} of your monthly AI-art "
                f"generations.  Resets on the 1st UTC.  Pro tier "
                f"raises this cap; Free tier doesn't include "
                f"AI-art generation."
            )
        else:
            msg = (
                f"AI quota exceeded ({exc.key}={exc.used} > {exc.cap}).  "
                f"Art generation is the most expensive AI surface; "
                f"wait for the daily cost reset or upgrade your plan."
            )
        raise HTTPException(429, msg)
    # Newest 12 items, by created_at DESC, for the prompt.  list_for_box
    # returns oldest-first so we reverse + slice here.
    items = list(reversed(dao_items.list_for_box(actor, box_id)))[:12]

    # Up to 3 small photo references for the multimodal prompt.  Read +
    # decrypt; mime is always image/jpeg post-phase-2 because every
    # upload is re-encoded as JPEG.
    photo_refs: list[tuple[bytes, str]] = []
    for it in items:
        if not it.get("photo"):
            continue
        p = _tenant_file(tenant_id, it["photo"])
        if not p.exists():
            continue
        try:
            photo_refs.append((_decrypt_for(tenant_id, p.read_bytes()), "image/jpeg"))
        except Exception:
            continue
        if len(photo_refs) >= 3:
            break

    try:
        image_bytes = vision.generate_label_art(
            box["name"], box["notes"] or "",
            items=items, item_photos=photo_refs,
        )
        dao_usage.record(tenant_id, "ai", "gemini_art")
    except Exception as e:
        if _wants_json(request):
            raise HTTPException(502, f"Art generation failed: {e}")
        raise HTTPException(502, f"Art generation failed: {e}")

    new_name = f"art-{secrets.token_hex(8)}.jpg"
    _write_encrypted(tenant_id, new_name, image_bytes)

    try:
        old = dao_boxes.set_background_art(actor, box_id, new_name)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    if old and old != new_name:
        with db() as conn:
            _delete_upload_if_orphan(conn, tenant_id, old)

    if _wants_json(request):
        return {"ok": True, "box_id": box_id, "background_art": new_name}
    return RedirectResponse(
        _safe_internal_redirect(next_url), status_code=303,
    )


@app.post("/boxes/{box_id}/clear-art")
def clear_box_art(
    request: Request,
    box_id: int,
    next_url: str = Form("/labels"),
):
    """Drop the generated background art for a box."""
    actor: Actor = request.state.actor
    try:
        old = dao_boxes.clear_background_art(actor, box_id)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    if old:
        with db() as conn:
            _delete_upload_if_orphan(conn, actor.tenant_id, old)

    if _wants_json(request):
        return {"ok": True, "box_id": box_id, "background_art": None}
    return RedirectResponse(
        _safe_internal_redirect(next_url), status_code=303,
    )


@app.post("/boxes/{box_id}/delete")
def delete_box(request: Request, box_id: int, confirm: str = Form(...)):
    actor: Actor = request.state.actor
    try:
        box = dao_boxes.get_by_id(actor, box_id)
    except NotFoundError:
        raise HTTPException(404)
    if confirm.strip() != box["name"]:
        raise HTTPException(400, "Type the box name to confirm deletion")
    try:
        result = dao_boxes.delete(actor, box_id)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    tenant_id = result["tenant_id"]
    for p in result["photos"]:
        try:
            _tenant_file(tenant_id, p).unlink()
        except FileNotFoundError:
            pass
        try:
            _tenant_thumb(tenant_id, p).unlink()
        except FileNotFoundError:
            pass
    return RedirectResponse("/home", status_code=303)


# ── Locations + rooms (where stuff actually lives) ─────────────────────────

# A small fixed palette so distinct rooms get visually separable colors on the
# floorplan. Cycled by index when assigning a fresh room.
_ROOM_COLORS = [
    "#4ade80", "#60a5fa", "#fbbf24", "#f87171",
    "#a78bfa", "#34d399", "#fb923c", "#22d3ee",
    "#f472b6", "#facc15", "#94a3b8", "#fde047",
]


def _next_room_color(conn, location_id: int) -> str:
    """Pick the next unused color in the palette for a location, cycling if all
    are taken. Keeps rooms inside a single floorplan visually distinct."""
    used = {
        r["color"] for r in conn.execute(
            "SELECT color FROM rooms WHERE location_id = ? AND color IS NOT NULL",
            (location_id,),
        ).fetchall()
    }
    for c in _ROOM_COLORS:
        if c not in used:
            return c
    n = conn.execute(
        "SELECT COUNT(*) FROM rooms WHERE location_id = ?", (location_id,),
    ).fetchone()[0]
    return _ROOM_COLORS[n % len(_ROOM_COLORS)]


@app.get("/locations", response_class=HTMLResponse)
def locations_index(request: Request):
    actor: Actor = request.state.actor
    locs = dao_locations.list_with_room_counts(actor)
    return templates.TemplateResponse(
        request, "locations.html", {"locations": locs},
    )


@app.post("/locations")
def create_location(request: Request, name: str = Form(...)):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    actor: Actor = request.state.actor
    try:
        location_id = dao_locations.create(actor, name)
    except ForbiddenError:
        raise HTTPException(403)
    return RedirectResponse(f"/locations/{location_id}", status_code=303)


@app.get("/locations/{location_id}", response_class=HTMLResponse)
def location_detail(
    request: Request,
    location_id: int,
    edit: str = "",
    floor: int | None = None,
):
    actor: Actor = request.state.actor
    try:
        loc = dao_locations.get_by_id(actor, location_id)
    except NotFoundError:
        raise HTTPException(404)
    floors = dao_floors.list_for_location(actor, location_id)

    # Default the current floor: explicit ?floor= wins, else the first one.
    current_floor = None
    if floors:
        if floor is not None:
            current_floor = next((f for f in floors if f["id"] == floor), floors[0])
        else:
            current_floor = floors[0]

    rooms = []
    with db() as conn:
        if current_floor:
            rooms = [dict(r) for r in conn.execute(
                "SELECT r.*, "
                "       (SELECT COUNT(*) FROM boxes "
                "         WHERE room_id = r.id AND tenant_id = ?) AS box_count "
                "FROM rooms r "
                "WHERE r.floor_id = ? AND r.tenant_id = ? "
                "ORDER BY r.name",
                (actor.tenant_id, current_floor["id"], actor.tenant_id),
            ).fetchall()]
            # Pull every box on this floor in one shot, then bucket by room
            # so each room rect can render its boxes as tiles inside it.
            box_rows = conn.execute(
                "SELECT b.id, b.name, b.room_id, b.color, "
                "       b.created_at, b.last_audited_at, "
                "       (SELECT COUNT(*) FROM items "
                "         WHERE box_id = b.id AND tenant_id = ?) AS item_count "
                "FROM boxes b "
                "JOIN rooms r ON r.id = b.room_id "
                "WHERE r.floor_id = ? AND b.tenant_id = ? "
                "ORDER BY b.name",
                (actor.tenant_id, current_floor["id"], actor.tenant_id),
            ).fetchall()
            # Items (with photo) for the high-LOD tile mosaic. Pull id +
            # name so the rendered img can carry both — needed for the
            # hover tooltip and the item drag-and-drop between boxes.
            item_rows = conn.execute(
                "SELECT i.id AS item_id, i.box_id, i.name AS item_name, i.photo "
                "FROM items i "
                "JOIN boxes b ON b.id = i.box_id "
                "JOIN rooms r ON r.id = b.room_id "
                "WHERE r.floor_id = ? AND i.photo IS NOT NULL "
                "  AND i.tenant_id = ? "
                "ORDER BY i.box_id, i.created_at DESC",
                (current_floor["id"], actor.tenant_id),
            ).fetchall()
            # Cap at 64 per box (8x8 grid worth) — enough headroom that even
            # heavily-photographed boxes show every item, but bounded so a
            # pathological 1000-item box doesn't bloat the page payload.
            mosaic_by_box: dict[int, list] = {}
            for it in item_rows:
                lst = mosaic_by_box.setdefault(it["box_id"], [])
                if len(lst) < 64:
                    lst.append({
                        "item_id": it["item_id"],
                        "name": it["item_name"],
                        "photo": it["photo"],
                    })
            import math as _math
            boxes_by_room: dict[int, list] = {}
            for b in box_rows:
                d = dict(b)
                mosaic = mosaic_by_box.get(b["id"], [])
                d["mosaic"] = mosaic
                # Square-ish grid: ⌈√N⌉ columns. With auto-flow rows the
                # mosaic packs into a near-square so each cell stays as
                # large as possible at the deepest zoom tier.
                d["mosaic_cols"] = max(1, _math.ceil(_math.sqrt(len(mosaic)))) if mosaic else 1
                boxes_by_room.setdefault(b["room_id"], []).append(d)
            for r in rooms:
                rb = boxes_by_room.get(r["id"], [])
                r["boxes"] = rb
                # Same square-pack idea as the mosaic: pick a column count
                # that lets N boxes fill the room cleanly. 1 box → 1 col
                # → fills the room; 4 boxes → 2x2; 12 boxes → 4x3-ish.
                r["box_cols"] = max(1, _math.ceil(_math.sqrt(len(rb)))) if rb else 1

        # Rooms with no floor (e.g. from the legacy text-location migration)
        # surface separately so the user can clean them up.
        unassigned = [dict(r) for r in conn.execute(
            "SELECT r.*, "
            "       (SELECT COUNT(*) FROM boxes "
            "         WHERE room_id = r.id AND tenant_id = ?) AS box_count "
            "FROM rooms r "
            "WHERE r.location_id = ? AND r.floor_id IS NULL AND r.tenant_id = ? "
            "ORDER BY r.name",
            (actor.tenant_id, location_id, actor.tenant_id),
        ).fetchall()]
    return templates.TemplateResponse(
        request, "location.html",
        {
            "location": loc,
            "floors": floors,
            "current_floor": current_floor,
            "rooms": rooms,
            "unassigned_rooms": unassigned,
            "edit_mode": edit == "1",
            "room_palette": _ROOM_COLORS,
        },
    )


@app.post("/locations/{location_id}")
def edit_location(request: Request, location_id: int, name: str = Form(...)):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    actor: Actor = request.state.actor
    try:
        dao_locations.rename(actor, location_id, name)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    return RedirectResponse(f"/locations/{location_id}", status_code=303)


@app.post("/locations/{location_id}/delete")
def delete_location(request: Request, location_id: int, confirm: str = Form(...)):
    actor: Actor = request.state.actor
    try:
        result = dao_locations.delete(actor, location_id, confirm.strip())
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError as exc:
        # The DAO raises ForbiddenError for both role denial AND for
        # the type-the-name confirm mismatch — distinguish by message
        # so the type-the-name path returns 400 (a user error) rather
        # than 403 (an authz error).
        if "type the location name" in str(exc).lower():
            raise HTTPException(400, "Type the location name to confirm deletion")
        raise HTTPException(403)
    if result.get("floorplan"):
        with db() as conn:
            _delete_upload_if_orphan(conn, actor.tenant_id, result["floorplan"])
    return RedirectResponse("/locations", status_code=303)


@app.post("/locations/{location_id}/floors")
def create_floor(request: Request, location_id: int, name: str = Form(...)):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    actor: Actor = request.state.actor
    try:
        floor_id = dao_floors.create(actor, location_id, name)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    return RedirectResponse(
        f"/locations/{location_id}?floor={floor_id}&edit=1", status_code=303,
    )


@app.post("/floors/{floor_id}")
def edit_floor(request: Request, floor_id: int, name: str = Form(...)):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    actor: Actor = request.state.actor
    try:
        location_id = dao_floors.rename(actor, floor_id, name)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    return RedirectResponse(
        f"/locations/{location_id}?floor={floor_id}", status_code=303,
    )


@app.get("/floors/{floor_id}/edit-image", response_class=HTMLResponse)
def floor_edit_image(request: Request, floor_id: int):
    """In-browser floorplan editor.  Loads the floor's existing
    floorplan as a locked background image in a Fabric.js canvas
    when one exists, or starts with a blank white canvas when the
    floor has no floorplan yet.  Saves back through the existing
    ``/floors/{id}/floorplan`` POST endpoint — annotations bake
    into the bitmap, no schema change."""
    actor: Actor = request.state.actor
    try:
        floor = dao_floors.get_by_id(actor, floor_id)
    except NotFoundError:
        raise HTTPException(404)
    return templates.TemplateResponse(
        request, "floor_edit_image.html",
        {"floor": floor},
    )


@app.post("/floors/{floor_id}/floorplan")
async def upload_floor_floorplan(request: Request, floor_id: int, image: UploadFile = File(...)):
    if not image or not image.filename:
        raise HTTPException(400, "Image required")
    actor: Actor = request.state.actor
    try:
        floor = dao_floors.get_by_id(actor, floor_id)
    except NotFoundError:
        raise HTTPException(404)
    new_name = save_photo_bytes(actor.tenant_id, await image.read(), image.filename)
    try:
        result = dao_floors.update_floorplan(actor, floor_id, new_name)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    old = result["old_floorplan"]
    if old and old != new_name:
        with db() as conn:
            _delete_upload_if_orphan(conn, actor.tenant_id, old)
    return RedirectResponse(
        f"/locations/{floor['location_id']}?floor={floor_id}&edit=1", status_code=303,
    )


@app.post("/floors/{floor_id}/delete")
def delete_floor(request: Request, floor_id: int):
    actor: Actor = request.state.actor
    try:
        result = dao_floors.delete(actor, floor_id)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    if result.get("floorplan"):
        with db() as conn:
            _delete_upload_if_orphan(conn, actor.tenant_id, result["floorplan"])
    return RedirectResponse(f"/locations/{result['location_id']}", status_code=303)


@app.post("/floors/{floor_id}/rooms")
def create_room(
    request: Request,
    floor_id: int,
    name: str = Form(...),
    x: float = Form(0), y: float = Form(0),
    w: float = Form(0), h: float = Form(0),
):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    actor: Actor = request.state.actor
    try:
        floor = dao_floors.get_by_id(actor, floor_id)
    except NotFoundError:
        raise HTTPException(404)
    with db() as conn:
        color = _next_room_color(conn, floor["location_id"])
    try:
        room_id = dao_rooms.create(
            actor, floor_id, name,
            x=_clamp01(x), y=_clamp01(y), w=_clamp01(w), h=_clamp01(h),
            color=color,
        )
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    if "application/json" in request.headers.get("accept", ""):
        return {"ok": True, "id": room_id, "color": color, "name": name}
    return RedirectResponse(
        f"/locations/{floor['location_id']}?floor={floor_id}&edit=1", status_code=303,
    )


@app.post("/rooms/{room_id}")
def edit_room(
    request: Request,
    room_id: int,
    name: str = Form(...),
    x: float = Form(0), y: float = Form(0),
    w: float = Form(0), h: float = Form(0),
    color: str = Form(""),
):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    color_val = color.strip() if color and color.strip() in _ROOM_COLORS else None
    actor: Actor = request.state.actor
    try:
        row = dao_rooms.get_with_location(actor, room_id)
    except NotFoundError:
        raise HTTPException(404)
    new_color = color_val if color_val is not None else row["color"]
    try:
        dao_rooms.update(
            actor, room_id,
            name=name,
            x=_clamp01(x), y=_clamp01(y), w=_clamp01(w), h=_clamp01(h),
            color=new_color,
        )
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    if "application/json" in request.headers.get("accept", ""):
        return {"ok": True, "color": new_color}
    target = f"/locations/{row['location_id']}?edit=1"
    if row["floor_id"]:
        target = f"/locations/{row['location_id']}?floor={row['floor_id']}&edit=1"
    return RedirectResponse(target, status_code=303)


@app.post("/rooms/{room_id}/delete")
def delete_room(request: Request, room_id: int):
    actor: Actor = request.state.actor
    try:
        result = dao_rooms.delete(actor, room_id)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    if "application/json" in request.headers.get("accept", ""):
        return {"ok": True}
    target = f"/locations/{result['location_id']}?edit=1"
    if result["floor_id"]:
        target = f"/locations/{result['location_id']}?floor={result['floor_id']}&edit=1"
    return RedirectResponse(target, status_code=303)


def _clamp01(v: float) -> float:
    """Floorplan coordinates are stored as fractions of the image; clamp so a
    drag that overshoots the canvas doesn't end up with negative or >1 values."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, f))


@app.get("/rooms/{room_id}/boxes", response_class=HTMLResponse)
def room_boxes(request: Request, room_id: int):
    """Boxes assigned to a single room — used as the click-through target from
    the floorplan view. Renders with the same card+thumb-strip presentation
    as the main /boxes index so the user gets parity."""
    actor: Actor = request.state.actor
    try:
        room = dao_rooms.get_with_location(actor, room_id)
    except NotFoundError:
        raise HTTPException(404)
    boxes = dao_boxes.list_for_room(actor, room_id)
    thumbs = dao_items.list_recent_photos_for_room(actor, room_id, limit_per_box=5)
    return templates.TemplateResponse(
        request, "room_boxes.html",
        {"room": room, "boxes": boxes, "thumbs": thumbs},
    )


def _referenced_uploads() -> set[tuple[int, str]]:
    """All upload `(tenant_id, filename)` pairs referenced by any row in
    the DB, plus their derived thumbnail companions so the orphan sweep
    keeps both halves.

    This is the single source of truth for both /maintenance/cleanup
    (orphan deletion) and /maintenance/export (backup zip).  Any new
    file-bearing column MUST be added here — otherwise a fresh feature
    will silently leak files on cleanup AND drop them from backups.

    DB tables themselves are captured by zipping the whole stash.db, so
    DB-only additions (new tables, new non-file columns) need nothing.

    The set is keyed on (tenant_id, filename) since after phase 2 every
    upload lives under UPLOAD_DIR/{tenant_id}/{name} — same filename
    can legitimately exist in two tenants without collision."""
    refs: set[tuple[int, str]] = set()
    with db() as conn:
        for sql in (
            "SELECT tenant_id, photo FROM items WHERE photo IS NOT NULL",
            "SELECT tenant_id, source_photo FROM items WHERE source_photo IS NOT NULL",
            "SELECT tenant_id, photo FROM pending_items WHERE photo IS NOT NULL",
            "SELECT tenant_id, photo FROM ingest_jobs WHERE photo IS NOT NULL",
            "SELECT tenant_id, background_art FROM boxes WHERE background_art IS NOT NULL",
            "SELECT tenant_id, floorplan FROM floors WHERE floorplan IS NOT NULL",
            "SELECT tenant_id, floorplan FROM locations WHERE floorplan IS NOT NULL",
        ):
            for tid, name in conn.execute(sql).fetchall():
                if tid is None or not name:
                    continue
                refs.add((tid, name))
    # Thumb companions follow their source — same tenant, derived name.
    for tid, name in list(refs):
        refs.add((tid, _thumb_path(tid, name).name))
    return refs


# ── /usage — per-tenant members + invites + telemetry ─────────────
#
# Spec § "User-facing usage page".  Phase-5 + 9 surface: members
# table, mint/revoke invite tokens, AI/upload counters.  The full
# spec'd page also has a billing breakdown, GDPR data export,
# backup links — those land in later phases (7, 8) where the
# underlying machinery exists.


def _render_usage_page(
    request: Request,
    *,
    invite_url: str = "",
    api_token_plaintext: str = "",
):
    """Shared usage-page renderer used by both GET and POST
    handlers.  Pulled out so the token-mint POST can render
    directly with the plaintext in the response body — *not*
    via a URL round-trip, which the leak scanner would (very
    correctly) catch and revoke the freshly-minted token."""
    actor: Actor = request.state.actor
    if actor.tenant_id is None:
        raise HTTPException(403, "No active tenant")
    tenant = dao_tenants.get_tenant(actor, actor.tenant_id)
    members = dao_tenants.list_members(actor, actor.tenant_id)
    invites = []
    outbound_shares = []
    api_tokens = []
    manual_tokens: list[dict] = []
    oauth_client_groups: list[dict] = []
    if actor.role == "maintainer":
        invites = dao_invites.list_for_tenant(actor)
        outbound_shares = dao_shares.list_outbound(actor)
        api_tokens = dao_api_tokens.list_for_tenant(actor)
        # Split OAuth-minted tokens out of the flat list and roll
        # them up by client.  Claude.ai's MCP connector mints a
        # fresh access token on every reach-out, so without
        # grouping the panel grows by one row per call and the
        # maintainer can't see the forest for the trees.  Manual
        # tokens (oauth_client_id IS NULL) stay flat.
        oauth_client_groups = _group_oauth_tokens(api_tokens)
        manual_tokens = [
            t for t in api_tokens if not t.get("oauth_client_id")
        ]
    summary = dao_usage.summary(actor)
    months = dao_usage.monthly_summary(actor.tenant_id, months_back=12)
    tour_email = _tour_actor_email(actor)
    tour_catalogue = dao_tours.catalogue()
    tour_seen = dao_tours.state_for_actor(tour_email)
    billing_enabled = dao_billing.is_configured()
    subscription = (
        dao_billing.subscription_for_tenant(actor.tenant_id)
        if billing_enabled else None
    )
    pro_price_display = _pro_price_display()
    free_caps = dao_quotas._PLAN_DEFAULTS["free"]
    pro_caps = dao_quotas._PLAN_DEFAULTS["pro"]
    return templates.TemplateResponse(
        request, "usage.html",
        {
            "tenant": tenant,
            "members": members,
            "invites": invites,
            "outbound_shares": outbound_shares,
            "api_tokens": api_tokens,
            "manual_tokens": manual_tokens,
            "oauth_client_groups": oauth_client_groups,
            "api_token_plaintext": api_token_plaintext,
            "current_email": actor.email,
            "current_role": actor.role,
            "is_maintainer": actor.role == "maintainer",
            "usage": summary,
            "months": months,
            "invite_url": invite_url,
            "public_url": PUBLIC_URL,
            "billing_enabled": billing_enabled,
            "subscription": subscription,
            "billing_status": request.query_params.get("billing", ""),
            "pro_price_display": pro_price_display,
            "free_caps": free_caps,
            "pro_caps": pro_caps,
            "tour_catalogue": tour_catalogue,
            "tour_seen": tour_seen,
            # Feedback stars + recent submissions — feeds the
            # "Your contributions" card.  Tracks the actor email
            # so the count survives a tenant switch.
            "your_stars": dao_feedback.stars_for_actor(actor.email or ""),
            "your_feedback": dao_feedback.list_for_actor(
                actor.email or "", limit=20,
            ),
            # Public-leaderboard handle (opt-in).  ``None`` means
            # the actor hasn't set one + reads as "Anonymous" on
            # the public board.  ``revoked`` means an operator
            # nuked a previous pick; user is prompted to choose
            # something new.
            "your_handle": (
                dao_handles.active_handle(actor.email or "")
            ),
            "your_handle_revoked": (
                (h := dao_handles.get_handle(actor.email or "")) is not None
                and h.get("revoked_at") is not None
            ),
            "handle_set": request.query_params.get("handle_set", ""),
            "handle_error": request.query_params.get("handle_error", ""),
        },
    )


@app.get("/usage", response_class=HTMLResponse)
def usage_page(
    request: Request,
    invite_url: str = "",
):
    """Per-tenant usage + members surface.  Maintainers see the full
    page; readonly members see the meters only (membership listing
    too, since they're already in it).  ``invite_url`` round-trips a
    freshly-created invite link into the page — invite tokens don't
    match the API-token leak signature so they're safe to put in a
    URL.  API token plaintext is *never* in a URL — the mint POST
    renders the page inline instead of redirecting."""
    return _render_usage_page(request, invite_url=invite_url)


_MAX_FEEDBACK_BODY = 4000
_MAX_FEEDBACK_SCREENSHOT_BYTES = 1_500_000


def _decode_feedback_screenshot(data_url: str) -> bytes | None:
    """Decode the ``data:image/jpeg;base64,…`` payload the widget
    POSTs into raw bytes.  Returns None for missing / malformed
    inputs so the feedback row can still land without a screenshot.
    Capped at 1.5 MB so a runaway capture can't OOM the worker."""
    if not data_url:
        return None
    head, _, payload = data_url.partition(",")
    if "base64" not in head or not payload:
        return None
    try:
        raw = base64.b64decode(payload, validate=False)
    except Exception:
        return None
    if len(raw) > _MAX_FEEDBACK_SCREENSHOT_BYTES:
        return None
    return raw


@app.post("/feedback")
def submit_feedback(
    request: Request,
    body: str = Form(...),
    screenshot_data: str = Form(""),
    source_url: str = Form(""),
    user_agent: str = Form(""),
    viewport_w: str = Form(""),
    viewport_h: str = Form(""),
):
    """In-app feedback widget submission.  Authenticated tenant
    members only — the widget is hidden for anonymous visitors and
    the route enforces the same.  Optional screenshot is encrypted
    with the tenant's DEK (same pipeline as item photos) so a
    cross-tenant leak on disk is impossible."""
    actor: Actor = request.state.actor
    if not actor.tenant_id:
        raise HTTPException(403, "Feedback requires an active tenant")
    body = (body or "").strip()
    if not body:
        raise HTTPException(400, "Feedback body is required")
    body = body[:_MAX_FEEDBACK_BODY]
    screenshot_name: str | None = None
    raw = _decode_feedback_screenshot(screenshot_data)
    if raw:
        screenshot_name = f"feedback-{secrets.token_hex(8)}.jpg"
        try:
            _write_encrypted(actor.tenant_id, screenshot_name, raw)
        except Exception:
            # Screenshot write failure is non-fatal — the feedback
            # is more valuable than the attached image.
            screenshot_name = None
    try:
        viewport_w_int = int(viewport_w) if viewport_w else None
    except ValueError:
        viewport_w_int = None
    try:
        viewport_h_int = int(viewport_h) if viewport_h else None
    except ValueError:
        viewport_h_int = None
    feedback_id = dao_feedback.create(
        tenant_id=actor.tenant_id,
        actor_email=actor.email,
        body=body,
        screenshot=screenshot_name,
        source_url=(source_url or "")[:512] or None,
        user_agent=(user_agent or "")[:256] or None,
        viewport_w=viewport_w_int,
        viewport_h=viewport_h_int,
    )
    if _wants_json(request):
        return {"ok": True, "feedback_id": feedback_id}
    # ``source_url`` is set by JS in the feedback widget to
    # ``window.location.href`` — the page the user was on when they
    # opened the widget.  Treat it as untrusted: a malicious caller
    # could POST any URL here.  ``_safe_internal_redirect`` rejects
    # off-site targets and falls back to /home.
    return RedirectResponse(
        _safe_internal_redirect(source_url), status_code=303,
    )


@app.get("/leaderboard", response_class=HTMLResponse)
def leaderboard_page(request: Request):
    """Top-N feedback contributors across the whole stash —
    cross-tenant, cross-actor.  Stars are 1:1 with shipped
    (``status='done'``) feedback rows.  The page is intentionally
    celebratory: every contributor who landed even one shipped
    item shows up on the user's own card with confetti vibes;
    the global podium below ranks the top 3.

    PII handling (feedback #30): the page renders ``handle`` —
    an opt-in display name — instead of the actor's email.  An
    email with no handle reads as "Anonymous"; the user can opt
    in via /usage by picking a handle.  Operators can revoke
    handles (the row stays, the display reverts to Anonymous).

    Visible to any signed-in tenant member.  The operator's email
    (and anything else in ``STASH_LEADERBOARD_IGNORE_EMAILS``) is
    filtered out of the ranking so the operator doesn't trophy
    themselves on their own platform."""
    actor: Actor = request.state.actor
    raw = dao_feedback.leaderboard(
        exclude_emails=tuple(_LEADERBOARD_IGNORE_EMAILS),
        limit=3,
    )
    # Decorate each row with the actor's handle (if any).  Drop
    # the raw email — the template must never get a chance to
    # leak it.
    top = []
    for r in raw:
        handle = dao_handles.active_handle(r["actor_email"])
        top.append({"handle": handle, "stars": r["stars"]})
    your_stars = dao_feedback.stars_for_actor(actor.email or "")
    your_handle_row = dao_handles.get_handle(actor.email or "")
    your_handle = (
        your_handle_row.get("handle")
        if your_handle_row and your_handle_row.get("revoked_at") is None
        else None
    )
    you_excluded = (actor.email or "").lower() in _LEADERBOARD_IGNORE_EMAILS
    return templates.TemplateResponse(
        request, "leaderboard.html",
        {
            "top": top,
            "your_stars": your_stars,
            "your_email": actor.email,
            "your_handle": your_handle,
            "your_handle_revoked": (
                your_handle_row is not None
                and your_handle_row.get("revoked_at") is not None
            ),
            "you_excluded": you_excluded,
        },
    )


@app.post("/usage/handle")
def set_handle_route(
    request: Request,
    handle: str = Form(...),
):
    """Set or update the actor's public-leaderboard handle.
    Validates length + character set + uniqueness; on failure
    redirects back to /usage with an error flash."""
    actor: Actor = request.state.actor
    if not actor.email:
        raise HTTPException(403, "Sign in with an email account first.")
    try:
        dao_handles.set_handle(actor, handle)
    except dao_handles.HandleError as exc:
        # Round-trip the error via a query param so the user sees
        # the validation message without losing their place.
        from urllib.parse import quote
        return RedirectResponse(
            f"/usage?handle_error={quote(exc.reason)}#contributions",
            status_code=303,
        )
    return RedirectResponse(
        "/usage?handle_set=1#contributions", status_code=303,
    )


@app.post("/admin/handles/revoke")
def admin_revoke_handle(
    request: Request,
    actor_email: str = Form(...),
    reason: str = Form(""),
):
    """Operator-only handle revocation.  Use case: handle is
    offensive / abusive / impersonates someone; operator nukes
    it, the actor keeps their stars but reverts to Anonymous on
    the public board until they pick a new acceptable handle."""
    actor: Actor = request.state.actor
    _require_operator_route(actor)
    try:
        dao_handles.revoke_handle(actor, actor_email, reason=reason)
    except NotFoundError:
        raise HTTPException(404)
    return RedirectResponse("/admin#handles", status_code=303)


_FEEDBACK_EXPORT_COLUMNS = (
    "id", "status", "tenant_id", "tenant_name", "actor_email",
    "body", "source_url", "user_agent", "viewport_w", "viewport_h",
    "screenshot", "operator_notes", "created_at", "resolved_at",
    "resolved_by",
)


@app.get("/admin/feedback/export")
def admin_feedback_export(request: Request):
    """Operator-only export of the feedback queue for offline
    triage (paste into a chat with an AI assistant, drop into a
    spreadsheet, whatever).  ``format=json`` returns a structured
    list; ``format=csv`` returns a spreadsheet-friendly dump.
    ``status=open|accepted|rejected|done`` filters; default ``all``.

    Screenshot bytes don't ride along — the column carries the
    filename, fetch the actual image via
    ``/admin/feedback/{id}/screenshot`` when needed.  Keeps the
    export size predictable so a big queue doesn't blow the
    download."""
    actor: Actor = request.state.actor
    _require_operator_route(actor)
    fmt = (request.query_params.get("format") or "json").lower()
    status = request.query_params.get("status") or None
    if status == "all":
        status = None
    rows = dao_feedback.list_for_operator(status=status, limit=500)
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    if fmt == "csv":
        import csv
        import io as _io
        buf = _io.StringIO()
        writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(_FEEDBACK_EXPORT_COLUMNS)
        for r in rows:
            writer.writerow([r.get(c, "") for c in _FEEDBACK_EXPORT_COLUMNS])
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition":
                    f'attachment; filename="stash-feedback-{stamp}.csv"',
            },
        )
    # JSON default.
    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "exported_by": actor.email,
        "filter": {"status": status or "all"},
        "count": len(rows),
        "feedback": [
            {c: r.get(c) for c in _FEEDBACK_EXPORT_COLUMNS}
            for r in rows
        ],
    }
    return Response(
        content=json.dumps(payload, indent=2, default=str),
        media_type="application/json",
        headers={
            "Content-Disposition":
                f'attachment; filename="stash-feedback-{stamp}.json"',
        },
    )


@app.get("/admin/feedback/{feedback_id}/screenshot")
def admin_feedback_screenshot(request: Request, feedback_id: int):
    """Operator-only screenshot fetch.  Reads + decrypts the tenant
    blob.  Same role gate as the rest of /admin."""
    actor: Actor = request.state.actor
    _require_operator_route(actor)
    try:
        row = dao_feedback.get(feedback_id)
    except NotFoundError:
        raise HTTPException(404)
    if not row.get("screenshot") or not row.get("tenant_id"):
        raise HTTPException(404, "No screenshot attached")
    tenant_id = row["tenant_id"]
    p = _tenant_file(tenant_id, row["screenshot"])
    if not p.exists():
        raise HTTPException(404, "Screenshot file missing")
    try:
        plaintext = _decrypt_for(tenant_id, p.read_bytes())
    except Exception:
        raise HTTPException(500, "Screenshot decrypt failed")
    return Response(content=plaintext, media_type="image/jpeg")


@app.post("/admin/feedback/{feedback_id}/status")
def admin_feedback_status(
    request: Request,
    feedback_id: int,
    status: str = Form(...),
    notes: str = Form(""),
):
    """Operator transitions feedback through the queue (accepted /
    rejected / done).  Stamps resolved_at + resolved_by."""
    actor: Actor = request.state.actor
    _require_operator_route(actor)
    try:
        dao_feedback.set_status(
            feedback_id, status,
            operator_email=actor.email or "operator",
            notes=(notes or "").strip() or None,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except NotFoundError:
        raise HTTPException(404)
    return RedirectResponse("/admin#feedback", status_code=303)


# ── Public /about pages (Stripe KYC + general transparency) ────────


# Stash's public-facing contact email.  Stripe + similar financial
# partners require a reachable customer-service channel on the
# business website; this env var keeps the address out of the
# template source so a deploy can swap it without a code change.
# Fallback is a sensible "ops@<domain>" if the operator hasn't set
# one yet — better than a hard-coded fake.
def _pro_price_display() -> str:
    """Pro tier price as shown on /about/pricing + the /usage
    upgrade card.  Configurable so a deploy can match whatever
    is actually configured in Stripe without a code change —
    Stripe is the source of truth, this string just mirrors it
    on the marketing page.  Defaults to '$4' if unset."""
    return (os.environ.get("STASH_PRO_PRICE_DISPLAY") or "$4").strip()


def _public_contact_email() -> str:
    v = (os.environ.get("STASH_PUBLIC_CONTACT_EMAIL") or "").strip()
    if v:
        return v
    base = PUBLIC_URL or ""
    host = (base.split("://", 1)[-1].split("/", 1)[0] or "stash.example.com")
    return f"support@{host}"


def _public_business_name() -> str:
    """Legal entity name that ships on Stripe pages + the /about
    footer.  Set via ``STASH_PUBLIC_BUSINESS_NAME`` to your
    actual business name (operators with a personal LLC put their
    full name here for Stripe KYC).  Distinct from the product
    name below — Stripe wants the entity, the marketing surface
    wants the product."""
    return (os.environ.get("STASH_PUBLIC_BUSINESS_NAME") or "Stash").strip() or "Stash"


def _public_product_name() -> str:
    """Marketing / link-preview / page-title name.  Always
    ``"Stash"`` unless an operator forks the brand and explicitly
    overrides with ``STASH_PUBLIC_PRODUCT_NAME``.  Keeping this
    separate from ``_public_business_name`` so a personal-LLC
    operator doesn't end up with their own name as the title bar
    on every public page + every social-card preview (the bug
    that triggered this split: a shared link to ``/`` rendered as
    'Brennan VanderLaan — Brennan VanderLaan household inventory'
    in Discord's link preview)."""
    return (os.environ.get("STASH_PUBLIC_PRODUCT_NAME") or "Stash").strip() or "Stash"


def _render_about(request: Request, page: str, title: str) -> HTMLResponse:
    """One-stop renderer for the public /about/* pages.  Pages bypass
    the auth wall (see ``_AUTH_BYPASS_PREFIXES``) so they're reachable
    without a Google sign-in — Stripe + similar KYC-grade financial
    partners require it.  ``hide_feedback_widget`` keeps the in-app
    feedback bubble off these pages since the viewer may be a
    prospect or auditor, not a tenant member."""
    pro_caps = dao_quotas._PLAN_DEFAULTS["pro"]
    free_caps = dao_quotas._PLAN_DEFAULTS["free"]
    return templates.TemplateResponse(
        request, f"about/{page}.html",
        {
            "page_title": title,
            "business_name": _public_business_name(),
            "product_name": _public_product_name(),
            "contact_email": _public_contact_email(),
            "public_url": PUBLIC_URL,
            "pro_price_display": _pro_price_display(),
            "pro_caps": pro_caps,
            "free_caps": free_caps,
            "ai_art_enabled": _ai_art_enabled(),
            "hide_feedback_widget": True,
        },
    )


@app.get("/about", response_class=HTMLResponse)
def about_index(request: Request):
    return _render_about(request, "index", "About")


@app.get("/about/pricing", response_class=HTMLResponse)
def about_pricing(request: Request):
    return _render_about(request, "pricing", "Pricing")


@app.get("/about/terms", response_class=HTMLResponse)
def about_terms(request: Request):
    return _render_about(request, "terms", "Terms of Service")


@app.get("/about/privacy", response_class=HTMLResponse)
def about_privacy(request: Request):
    return _render_about(request, "privacy", "Privacy Policy")


@app.get("/about/refunds", response_class=HTMLResponse)
def about_refunds(request: Request):
    return _render_about(request, "refunds", "Refunds & Cancellation")


@app.get("/about/contact", response_class=HTMLResponse)
def about_contact(request: Request):
    return _render_about(request, "contact", "Contact")


@app.get("/about/sub-processors", response_class=HTMLResponse)
def about_sub_processors(request: Request):
    return _render_about(request, "sub_processors", "Sub-processors")


@app.get("/about/transparency", response_class=HTMLResponse)
def about_transparency(request: Request):
    return _render_about(request, "transparency", "Where your $4 goes")


# ── Onboarding tours ───────────────────────────────────────────────


def _tour_actor_email(actor: Actor) -> str | None:
    """Resolve the user identity for tour state.  Bearer-auth
    actors carry a synthetic ``api_token:N`` email — tours are a
    UX preference for humans, not robots, so we skip the marker
    and return None there (no tour fires for an MCP client)."""
    email = (actor.email or "").strip()
    if not email or email.startswith("api_token:"):
        return None
    return email


@app.get("/api/v1/tour/state")
def tour_state(request: Request):
    """Return the user's tour state — a feature → seen-bool map +
    the catalogue of every tour with steps so the JS layer can
    render an overlay without a second round trip."""
    actor: Actor = request.state.actor
    email = _tour_actor_email(actor)
    path = request.query_params.get("path") or "/"
    pending = dao_tours.tours_for_page(email, path)
    return {
        "ok": True,
        "actor_email": email,
        "seen": dao_tours.state_for_actor(email),
        "auto_play": [
            {
                "feature": t["feature"],
                "title": t["title"],
                "version": t["version"],
                "steps": t["steps"],
            }
            for t in pending
        ],
    }


@app.get("/api/v1/tour/{feature}")
def tour_get(request: Request, feature: str):
    """Fetch a single tour by feature id — used when the user
    triggers a replay from /usage."""
    tour = next((t for t in dao_tours.TOURS if t["feature"] == feature), None)
    if tour is None:
        raise HTTPException(404)
    return {
        "ok": True,
        "feature": tour["feature"],
        "title": tour["title"],
        "version": tour["version"],
        "steps": tour["steps"],
    }


@app.post("/tour/{feature}/seen")
def tour_mark_seen(request: Request, feature: str):
    """Mark a tour as seen at its current version.  Called by the
    overlay JS on the user's last 'Next' or 'Skip' tap."""
    actor: Actor = request.state.actor
    email = _tour_actor_email(actor)
    if not email:
        raise HTTPException(403, "Anonymous actor")
    dao_tours.mark_seen(email, feature)
    if _wants_json(request):
        return {"ok": True, "feature": feature}
    # ``Referer`` is browser-supplied but ultimately attacker-
    # controllable (a form on evil.com can drive this request with
    # any Referer the browser will send).  Guard against using it
    # as an off-site redirect target.
    return RedirectResponse(
        _safe_internal_redirect(request.headers.get("referer")),
        status_code=303,
    )


@app.post("/tour/{feature}/reset")
def tour_reset_one(request: Request, feature: str):
    """Clear a single seen-record so the user can replay the tour.
    Trigger lives on /usage."""
    actor: Actor = request.state.actor
    email = _tour_actor_email(actor)
    if not email:
        raise HTTPException(403, "Anonymous actor")
    dao_tours.reset(email, feature)
    return RedirectResponse("/usage#tours", status_code=303)


@app.post("/tour/reset-all")
def tour_reset_all(request: Request):
    """Replay every tour for the current user."""
    actor: Actor = request.state.actor
    email = _tour_actor_email(actor)
    if not email:
        raise HTTPException(403, "Anonymous actor")
    dao_tours.reset_all(email)
    return RedirectResponse("/usage#tours", status_code=303)


# ── Stripe billing ─────────────────────────────────────────────────


@app.post("/usage/upgrade")
def usage_upgrade(request: Request):
    """Create a Stripe Checkout session and redirect the user's
    browser to it.  Maintainer-only.  503 when STRIPE_SECRET_KEY +
    STRIPE_WEBHOOK_SECRET + STRIPE_PRICE_ID_PRO aren't all set."""
    actor: Actor = request.state.actor
    if actor.tenant_id is None:
        raise HTTPException(403, "No active tenant")
    base = PUBLIC_URL or str(request.base_url).rstrip("/")
    try:
        url = dao_billing.create_checkout_session(
            actor,
            success_url=f"{base}/usage?billing=success",
            cancel_url=f"{base}/usage?billing=canceled",
        )
    except dao_billing.BillingNotConfiguredError as exc:
        raise HTTPException(503, str(exc))
    except ForbiddenError:
        raise HTTPException(403)
    return RedirectResponse(url, status_code=303)


@app.get("/usage/billing-portal")
def usage_billing_portal(request: Request):
    """Redirect to the Stripe Customer Portal so an existing
    subscriber can manage / cancel without us building a portal
    UI."""
    actor: Actor = request.state.actor
    if actor.tenant_id is None:
        raise HTTPException(403, "No active tenant")
    base = PUBLIC_URL or str(request.base_url).rstrip("/")
    try:
        url = dao_billing.create_portal_session(
            actor, return_url=f"{base}/usage",
        )
    except dao_billing.BillingNotConfiguredError as exc:
        raise HTTPException(503, str(exc))
    except NotFoundError:
        raise HTTPException(404, "No Stripe customer yet — upgrade first.")
    except ForbiddenError:
        raise HTTPException(403)
    return RedirectResponse(url, status_code=303)


@app.post("/webhooks/stripe")
async def webhooks_stripe(request: Request):
    """Stripe webhook receiver.  Signature-verified inside the DAO
    via the SDK; we don't trust the body until that check passes.
    Returns 200 + small JSON for any handled or no-op event;
    returns 400 on signature failures so Stripe surfaces the
    delivery error in the dashboard."""
    sig = request.headers.get("Stripe-Signature", "")
    body = await request.body()
    try:
        outcome = dao_billing.process_webhook_event(body, sig)
    except dao_billing.BillingNotConfiguredError as exc:
        raise HTTPException(503, str(exc))
    except Exception as exc:
        # Stripe's SignatureVerificationError + any other parse
        # failure end up here; surface as 400 so the dashboard
        # marks the delivery failed and Stripe retries.
        _LOG_ROUTE.warning("billing.webhook.error err=%r", exc)
        raise HTTPException(400, "webhook verification failed")
    return outcome


@app.get("/usage/gdpr-export")
def usage_gdpr_export(request: Request):
    """GDPR Article 20 portability bundle — same per-tenant data as
    the operator-format backup, but photos are *decrypted* into the
    zip + a plain-language README explains the layout.  Maintainer-
    only.  Audit-logged as ``gdpr.export``."""
    actor: Actor = request.state.actor
    if actor.tenant_id is None:
        raise HTTPException(403, "No active tenant")
    try:
        zip_bytes, manifest = dao_backups.build_gdpr_zip(actor)
    except ForbiddenError:
        raise HTTPException(403)
    except NotFoundError:
        raise HTTPException(404)
    safe_name = "".join(
        c if c.isalnum() or c in "-_" else "-" for c in manifest["tenant_name"]
    )[:40] or f"tenant-{actor.tenant_id}"
    stamp = manifest["exported_at"][:10].replace("-", "")
    filename = f"stash-data-{safe_name}-{stamp}.zip"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Backup-Sha256": manifest["zip_sha256"],
            "X-GDPR-Article": "20",
        },
    )


@app.get("/usage/backup")
def usage_backup(request: Request):
    """Per-tenant backup zip download.  Maintainer-only — readonly
    members can't trigger backups (spec § "Roles · Operations
    matrix").  See :mod:`dao.backups` for the zip shape + the
    "without STASH_KEK this is useless" caveat."""
    actor: Actor = request.state.actor
    if actor.tenant_id is None:
        raise HTTPException(403, "No active tenant")
    try:
        zip_bytes, manifest = dao_backups.build_tenant_zip(actor)
    except ForbiddenError:
        raise HTTPException(403)
    except NotFoundError:
        raise HTTPException(404)
    safe_name = "".join(
        c if c.isalnum() or c in "-_" else "-" for c in manifest["tenant_name"]
    )[:40] or f"tenant-{actor.tenant_id}"
    stamp = manifest["exported_at"][:10].replace("-", "")
    filename = f"stash-{safe_name}-{stamp}.zip"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Backup-Format-Version": str(manifest["format_version"]),
            "X-Backup-Sha256": manifest["zip_sha256"],
        },
    )


@app.post("/usage/api-tokens", response_class=HTMLResponse)
def create_api_token(
    request: Request,
    name: str = Form(...),
    role: str = Form("maintainer"),
):
    """Mint a new API token and render the usage page inline with
    the one-time plaintext block.  Deliberately *not* a redirect:
    putting the plaintext in ``?api_token_plaintext=…`` would land
    in the next request's URL, where the leak scanner correctly
    spots it and revokes the freshly-minted token.  The plaintext
    rides only in the response body — never in a URL or any
    request that follows.

    URL bar after this POST shows ``/usage/api-tokens`` rather
    than ``/usage``; refreshing prompts the standard browser
    "resubmit form?" dialog, which is fine — re-submitting just
    mints another token, the user can revoke duplicates."""
    actor: Actor = request.state.actor
    try:
        result = dao_api_tokens.create(actor, name=name, role=role)
    except ForbiddenError:
        raise HTTPException(403)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _render_usage_page(
        request, api_token_plaintext=result["plaintext"],
    )


@app.post("/usage/api-tokens/{token_id}/revoke")
def revoke_api_token(request: Request, token_id: int):
    actor: Actor = request.state.actor
    try:
        dao_api_tokens.revoke(actor, token_id)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    return RedirectResponse("/usage", status_code=303)


@app.post("/usage/invites")
def create_invite(
    request: Request,
    email: str = Form(...),
    role: str = Form("maintainer"),
):
    """Mint a new invite and round-trip the URL into the /usage page
    so the maintainer can copy it.  No email send — the link goes
    out-of-band (text, signal, paper, whatever)."""
    actor: Actor = request.state.actor
    try:
        invite = dao_invites.create(actor, email=email, role=role)
    except ForbiddenError:
        raise HTTPException(403)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    base = PUBLIC_URL or str(request.base_url).rstrip("/")
    url = f"{base}/invite/{invite['token']}"
    # Stash the URL on the redirect so the page can render the
    # copy-this-link block right after the round-trip.
    from urllib.parse import urlencode
    return RedirectResponse(
        f"/usage?{urlencode({'invite_url': url})}",
        status_code=303,
    )


@app.post("/usage/invites/{token}/revoke")
def revoke_invite(request: Request, token: str):
    actor: Actor = request.state.actor
    try:
        dao_invites.revoke(actor, token)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    return RedirectResponse("/usage", status_code=303)


@app.get("/invite/{token}", response_class=HTMLResponse)
def invite_accept_page(request: Request, token: str):
    """Landing page after the recipient clicks the share link.  Shows
    tenant name, role, who invited, and a one-button accept.  Reuses
    the 4-state model from the DAO: redeemable / consumed / expired /
    unknown — copy differs per state so the recipient knows whether
    to ask the inviter for a fresh link."""
    actor: Actor = request.state.actor
    invite = dao_invites.get_by_token(token)
    return templates.TemplateResponse(
        request, "invite.html",
        {
            "invite": invite,
            "current_email": actor.email,
            "token": token,
        },
    )


# ── Tenant switcher ────────────────────────────────────────────


_TENANT_SWITCH_COOKIE = "stash_active_tenant"
_TENANT_SWITCH_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def _safe_internal_redirect(target: str | None, default: str = "/home") -> str:
    """Open-redirect guard.  Returns ``target`` only if it's a
    relative path beneath this app (``/foo``, never ``//host`` or
    ``http://evil/``).  Falls back to ``default`` otherwise.

    Used wherever a route echoes a user-supplied URL back as a 303
    Location — feedback ``source_url``, tour-seen ``referer``,
    tenant-switch ``next``.  Without this, any of those become a
    classic open-redirect: ``GET /...?next=https://evil.example``
    returns a 303 that the browser follows off-site, which is a
    phishing primer that originates on our domain."""
    if not target:
        return default
    # Strip leading whitespace/control chars + any ``\`` that some
    # browsers normalise to ``/`` (cf. CVE-2017-1000080 style).
    t = target.strip().replace("\\", "/")
    if not t.startswith("/") or t.startswith("//"):
        return default
    return t


def _safe_switch_redirect(target: str) -> str:
    """Open-redirect guard for ``next`` on /tenants/switch.  Thin
    wrapper around :func:`_safe_internal_redirect` so the call-site
    reads as "switch redirect" rather than the generic helper."""
    return _safe_internal_redirect(target, default="/home")


@app.post("/tenants/switch")
def tenants_switch(
    request: Request,
    tenant_id: str = Form(...),
    next: str = Form("/"),
):
    """Set the active-tenant cookie + bounce back where the user
    came from.  Cookie value is validated against the actor's
    memberships so a tampered request can't grant access to a
    tenant the user isn't in — the middleware re-checks every
    request anyway, but we reject early here so the cookie never
    holds a junk value."""
    actor: Actor = request.state.actor
    try:
        wanted = int(tenant_id)
    except ValueError:
        raise HTTPException(400, "tenant_id must be integer")
    if not any(tid == wanted for tid, _role in actor.memberships):
        # 404 (not 403) matches the operator-surface opacity rule —
        # we don't disclose whether a tenant exists.
        raise HTTPException(404)
    target = _safe_switch_redirect(next)
    response = RedirectResponse(target, status_code=303)
    # Secure is True only when the request arrived over HTTPS so
    # local dev (http://testserver / http://localhost) still works.
    is_https = request.url.scheme == "https" or (
        request.headers.get("x-forwarded-proto", "").lower() == "https"
    )
    response.set_cookie(
        _TENANT_SWITCH_COOKIE,
        str(wanted),
        max_age=_TENANT_SWITCH_MAX_AGE,
        path="/",
        httponly=True,
        secure=is_https,
        samesite="lax",
    )
    return response


@app.post("/invite/{token}/accept")
def invite_accept(request: Request, token: str):
    actor: Actor = request.state.actor
    try:
        result = dao_invites.redeem(token, actual_email=actor.email)
    except NotFoundError:
        raise HTTPException(404, "Invite is no longer valid")
    except ForbiddenError as exc:
        raise HTTPException(403, str(exc))
    # Newly-joined member needs a fresh request so the middleware
    # picks up the membership; redirect to the authenticated home.
    return RedirectResponse("/home", status_code=303)


# ── /mcp — Model Context Protocol endpoint (phase 18) ────────────


@app.post("/mcp")
async def mcp_post(request: Request):
    """Streamable HTTP POST handler.  Body is a single JSON-RPC
    message; response is application/json with the result (or 202
    for notifications).  SSE response is reserved for future tools
    that want to stream — atomic tools land on the JSON path.

    Spec rev 2025-11-25 — see mcp_server.py + spec.md."""
    if not _MCP_ENABLED:
        raise HTTPException(404)
    actor: Actor = request.state.actor
    _mcp_server.validate_request_headers(request)
    body = await request.body()
    response_payload = _mcp_server.dispatch(body, actor)
    if response_payload is None:
        # Notification — spec says 202 Accepted with no body.
        return Response(status_code=202)
    return Response(
        content=json.dumps(response_payload, default=str),
        media_type="application/json",
        # Pin the negotiated protocol version on responses so
        # clients can confirm what they're talking to.
        headers={
            "MCP-Protocol-Version": _mcp_server.SUPPORTED_PROTOCOL_VERSION,
        },
    )


@app.get("/mcp")
def mcp_get(request: Request):
    """Spec compliance: the endpoint MUST accept GET, but stash
    has no server-push use cases in v1.  Return 405."""
    if not _MCP_ENABLED:
        raise HTTPException(404)
    _mcp_server.validate_request_headers(request)
    return Response(
        status_code=405,
        content="Stash does not offer a server-initiated SSE stream.",
        media_type="text/plain",
        headers={"Allow": "POST"},
    )


@app.delete("/mcp")
def mcp_delete(request: Request):
    """Spec: ``DELETE /mcp`` is for client-initiated session
    termination.  Stash opts out of MCP-Session-Id, so there's
    no per-connection state to drop — return 405."""
    if not _MCP_ENABLED:
        raise HTTPException(404)
    _mcp_server.validate_request_headers(request)
    return Response(
        status_code=405,
        content="Stash does not implement MCP-Session-Id.",
        media_type="text/plain",
        headers={"Allow": "POST"},
    )


# ── OAuth 2.1 discovery (phase 19) ─────────────────────────────────


def _public_url(request: Request) -> str:
    """Resolve the canonical public URL for discovery responses.
    ``STASH_PUBLIC_URL`` wins (set in deploy); otherwise fall back
    to the request's reported base URL — fine for tests, may be
    wrong behind a proxy that doesn't set X-Forwarded-Proto."""
    return PUBLIC_URL or str(request.base_url).rstrip("/")


@app.get("/.well-known/oauth-protected-resource")
def well_known_protected_resource(request: Request):
    """RFC 9728 Protected Resource Metadata at the root path.
    Public — anyone can fetch it (no tenant data leaks)."""
    return dao_oauth.protected_resource_metadata(_public_url(request))


@app.get("/.well-known/oauth-protected-resource/{rest:path}")
def well_known_protected_resource_suffix(request: Request, rest: str):
    """RFC 9728 Section 3.1 also allows the path-suffixed form
    ``/.well-known/oauth-protected-resource/<resource-path>`` (e.g.
    ``/.well-known/oauth-protected-resource/mcp``).  Some clients —
    notably claude.ai's web custom-connector — probe this form
    first and only fall back to the root URI if it 404s.  Serving
    both saves a discovery round-trip + keeps a strict client
    happy."""
    return dao_oauth.protected_resource_metadata(_public_url(request))


@app.get("/.well-known/oauth-authorization-server")
def well_known_authorization_server(request: Request):
    """RFC 8414 Authorization Server Metadata.  Tells clients
    where to send authorize / token / register requests + which
    grants and PKCE methods we support."""
    return dao_oauth.authorization_server_metadata(_public_url(request))


# ── OAuth 2.1 flow endpoints (phase 19) ─────────────────────────────


def _validate_oauth_param(name: str, value: str, *, max_len: int) -> None:
    """Generic length guard for OAuth query/form params.  The spec
    doesn't pin exact upper bounds; these caps are sized for "more
    than any legitimate client needs, much less than a memory-bloat
    payload"."""
    if value and len(value) > max_len:
        raise HTTPException(
            400,
            f"OAuth {name} parameter exceeds {max_len} characters",
        )


def _append_query(redirect_uri: str, params: dict) -> str:
    """Compose a redirect URL by adding ``params`` to whatever
    query string the registered ``redirect_uri`` may already
    carry.  RFC 6749 §3.1.2 explicitly allows query components
    on the registered URI; the previous naive
    ``f"{uri}?{urlencode(params)}"`` produced ``...?a=b?code=…``
    when the registered URI was ``...?session=xyz``.

    Splits via ``urlparse``, merges + re-encodes the query, puts
    the URI back together with ``urlunparse`` so any registered
    fragment (rare) survives unchanged."""
    from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse
    parsed = urlparse(redirect_uri)
    existing = parse_qsl(parsed.query, keep_blank_values=True)
    # Add ours after the registered query; on key collision the
    # OAuth-protocol field wins (we set it second, and dict-form
    # would overwrite anyway).  Pass through ``parse_qsl``'s
    # list-of-pairs to preserve ordering for any client that
    # asserts on it.
    merged = existing + [(k, v) for k, v in params.items() if v is not None]
    return urlunparse(parsed._replace(query=urlencode(merged)))


def _oauth_redirect_with_error(redirect_uri: str, *, error: str,
                               error_description: str = "",
                               state: str = "") -> RedirectResponse:
    """OAuth's standard error-response shape: bounce back to the
    client's redirect_uri with ``error``/``error_description``/
    ``state`` query params.  Spec § 4.1.2.1 — never display the
    error to the user, the client surfaces it."""
    params = {"error": error}
    if error_description:
        params["error_description"] = error_description
    if state:
        params["state"] = state
    return RedirectResponse(
        _append_query(redirect_uri, params), status_code=303,
    )


@app.get("/oauth/authorize", response_class=HTMLResponse)
def oauth_authorize_get(
    request: Request,
    response_type: str = "",
    client_id: str = "",
    redirect_uri: str = "",
    scope: str = "",
    state: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "",
    resource: str = "",
):
    """OAuth authorization-code flow start.  The user's browser
    arrives here from the client (claude.ai's "connect" flow).
    Validate the params, render the consent page.  oauth2-proxy
    has already authenticated the user by the time we get here —
    ``request.state.actor.email`` is who's approving."""
    actor: Actor = request.state.actor
    if actor.tenant_id is None and not actor.memberships:
        # Edge case: signed-in via oauth2-proxy but no membership
        # anywhere.  Drop them on the auth-deny wall — operator
        # needs to invite them to a tenant first.
        raise HTTPException(403, "No tenant membership; ask an operator to invite you.")

    # Defensive length caps.  Spec doesn't mandate exact upper
    # bounds; these are sized for "more than any legitimate
    # client needs, less than a memory-bloat payload".
    _validate_oauth_param("state", state, max_len=512)
    _validate_oauth_param("scope", scope, max_len=256)
    _validate_oauth_param("code_challenge", code_challenge, max_len=128)
    _validate_oauth_param("resource", resource, max_len=2048)

    # Validate response_type early — this is the only one we
    # support and a misconfigured client should hit a clean error.
    if response_type != "code":
        raise HTTPException(400, "response_type must be 'code'")
    if code_challenge_method and code_challenge_method != "S256":
        raise HTTPException(400, "code_challenge_method must be 'S256'")
    if not code_challenge:
        raise HTTPException(400, "PKCE code_challenge required")

    client = dao_oauth.get_client(client_id)
    if client is None:
        raise HTTPException(400, f"unknown client_id: {client_id!r}")

    # Exact-match redirect_uri against the client's registered
    # list — open-redirect mitigation per OAuth 2.1 §7.12.
    if redirect_uri not in client["redirect_uris"]:
        raise HTTPException(
            400,
            f"redirect_uri {redirect_uri!r} not registered for "
            f"client {client_id!r}",
        )

    canonical_resource = f"{_public_url(request)}/mcp"
    # Default the resource to /mcp on this deployment if the
    # client didn't specify (most MCP clients will, but be lenient).
    if not resource:
        resource = canonical_resource
    elif resource.rstrip("/") != canonical_resource.rstrip("/"):
        # Spec §"Resource Parameter Implementation": the
        # ``resource`` MUST identify the MCP server the client
        # intends to use the token with.  Issuing tokens for
        # arbitrary resources isn't useful (the audience-bound
        # token won't authenticate anywhere on this stash) and
        # bloats DB rows.  Refuse explicitly.
        raise HTTPException(
            400,
            f"resource {resource!r} is not served by this deployment "
            f"(expected {canonical_resource!r})",
        )

    # Memberships dropdown: every tenant the user can grant
    # access to.  We surface the tenant name (not just id) so
    # the consent UX is human-readable.
    memberships: list[dict] = []
    for tid, role in actor.memberships:
        try:
            t = dao_tenants.get_tenant(actor, tid)
            memberships.append({
                "tenant_id": tid, "role": role, "tenant_name": t["name"],
            })
        except NotFoundError:
            continue
    if not memberships:
        raise HTTPException(
            403, "No tenant memberships available to grant.",
        )

    return templates.TemplateResponse(
        request, "oauth_consent.html",
        {
            "client": client,
            "current_email": actor.email,
            "memberships": memberships,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method or "S256",
            "scope": scope or "mcp",
            "resource": resource,
        },
    )


@app.post("/oauth/authorize")
def oauth_authorize_post(
    request: Request,
    decision: str = Form(...),
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    state: str = Form(""),
    code_challenge: str = Form(...),
    code_challenge_method: str = Form("S256"),
    scope: str = Form(""),
    resource: str = Form(...),
    tenant_id: int = Form(...),
):
    """User approved or denied the consent.  On approve, mint a
    code + redirect to the client's callback with it.  On deny,
    redirect with ``error=access_denied``."""
    actor: Actor = request.state.actor

    client = dao_oauth.get_client(client_id)
    if client is None or redirect_uri not in client["redirect_uris"]:
        raise HTTPException(400, "client / redirect_uri invalid")

    if decision != "approve":
        return _oauth_redirect_with_error(
            redirect_uri,
            error="access_denied",
            error_description="User denied authorization",
            state=state,
        )

    # Verify the user actually has the membership they claim.
    role = actor.has_membership(tenant_id)
    if role is None:
        raise HTTPException(
            403,
            "You don't have a membership on the selected tenant.",
        )

    code = dao_oauth.issue_authorization_code(
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        scope=scope or "mcp",
        resource=resource,
        tenant_id=tenant_id,
        user_email=actor.email,
        role=role,
    )

    params = {"code": code}
    if state:
        params["state"] = state
    return RedirectResponse(
        _append_query(redirect_uri, params), status_code=303,
    )


@app.post("/oauth/token")
async def oauth_token(request: Request):
    """OAuth /token endpoint.  Server-to-server (or browser-to-AS
    for public clients).  Reads form-encoded body per spec; we
    don't accept JSON bodies here even though many SDKs offer
    them — staying narrow keeps the parse path tight.

    Grants supported:
    * ``authorization_code`` — code → access + refresh.
    * ``refresh_token`` — rotate the refresh, mint a fresh
      access token.
    """
    form = await request.form()
    grant_type = form.get("grant_type")
    client_id = form.get("client_id") or ""
    client_secret = form.get("client_secret") or ""

    client = dao_oauth.get_client(client_id)
    if client is None:
        return Response(
            content=json.dumps({"error": "invalid_client"}),
            status_code=401,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )
    if not dao_oauth.verify_client_secret(client, client_secret):
        return Response(
            content=json.dumps({"error": "invalid_client"}),
            status_code=401,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    try:
        if grant_type == "authorization_code":
            code = form.get("code") or ""
            redirect_uri = form.get("redirect_uri") or ""
            code_verifier = form.get("code_verifier") or ""
            ctx = dao_oauth.consume_authorization_code(
                code=code, client_id=client_id,
                redirect_uri=redirect_uri,
                code_verifier=code_verifier,
            )
        elif grant_type == "refresh_token":
            refresh = form.get("refresh_token") or ""
            ctx = dao_oauth.consume_refresh_token(
                refresh_token=refresh, client_id=client_id,
            )
        else:
            return Response(
                content=json.dumps({
                    "error": "unsupported_grant_type",
                    "error_description":
                        f"grant_type {grant_type!r} not supported",
                }),
                status_code=400,
                media_type="application/json",
                headers={"Cache-Control": "no-store"},
            )
    except ValueError as exc:
        # ValueError carries the OAuth-shaped error message from
        # the DAO (``invalid_grant: ...``); split into the spec's
        # two-field shape for the response.
        msg = str(exc)
        err, _, desc = msg.partition(": ")
        return Response(
            content=json.dumps({
                "error": err.strip() or "invalid_grant",
                "error_description": desc.strip() or msg,
            }),
            status_code=400,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    pair = dao_oauth.issue_token_pair(
        client_id=client_id,
        tenant_id=ctx["tenant_id"],
        user_email=ctx["user_email"],
        role=ctx["role"],
        scope=ctx.get("scope") or "mcp",
        resource=ctx["resource"],
    )
    return Response(
        content=json.dumps(pair),
        media_type="application/json",
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
        },
    )


@app.post("/oauth/register")
async def oauth_register(request: Request):
    """Dynamic Client Registration (RFC 7591).  Public — anyone
    can self-register, which is the spec's expectation for
    interop.  Operators concerned about DCR abuse can revoke
    individual clients from /admin (or set
    ``STASH_OAUTH_DCR_ENABLED=false`` to disable entirely)."""
    if os.environ.get(
        "STASH_OAUTH_DCR_ENABLED", "true",
    ).strip().lower() in ("false", "0", "no"):
        return Response(
            content=json.dumps({
                "error": "registration_not_supported",
                "error_description":
                    "Dynamic Client Registration is disabled on this "
                    "deployment.  Ask the operator to pre-register "
                    "the client at /admin.",
            }),
            status_code=403,
            media_type="application/json",
        )
    # Per-IP throttle: defends against a malicious or runaway
    # client looping registrations.  Caddy stamps the real client
    # IP on ``X-Forwarded-For``; ``request.client.host`` is the
    # fallback for direct deploys.  Same shape as the
    # tenant-creation throttle in dao.quotas.
    client_ip = (
        (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        or (request.client.host if request.client else "")
    )
    try:
        dao_oauth.check_dcr_rate(client_ip)
    except dao_quotas.QuotaExceeded as exc:
        return Response(
            content=json.dumps({
                "error": "too_many_requests",
                "error_description":
                    f"DCR rate limit hit ({exc.used} ≥ {exc.cap} per "
                    f"hour from this IP).  Wait for the window reset.",
            }),
            status_code=429,
            media_type="application/json",
        )

    body = await request.json()
    name = (body.get("client_name") or "Unregistered MCP client").strip()
    redirect_uris = body.get("redirect_uris") or []
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return Response(
            content=json.dumps({
                "error": "invalid_redirect_uri",
                "error_description":
                    "redirect_uris must be a non-empty array",
            }),
            status_code=400,
            media_type="application/json",
        )
    # ``token_endpoint_auth_method == "none"`` means public
    # client (PKCE only).  Most MCP clients are public; allow
    # confidential clients too since the spec doesn't forbid them.
    is_public = body.get("token_endpoint_auth_method", "none") == "none"
    try:
        result = dao_oauth.register_client(
            name=name,
            redirect_uris=redirect_uris,
            is_public=is_public,
            registered_by_email="<dcr>",
            client_ip=client_ip,
        )
    except ValueError as exc:
        return Response(
            content=json.dumps({
                "error": "invalid_client_metadata",
                "error_description": str(exc),
            }),
            status_code=400,
            media_type="application/json",
        )
    out = {
        "client_id": result["client_id"],
        "client_id_issued_at": int(_unix_now()),
        "client_name": result["name"],
        "redirect_uris": result["redirect_uris"],
        "token_endpoint_auth_method": (
            "none" if result["is_public"] else "client_secret_post"
        ),
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
    if "client_secret" in result:
        out["client_secret"] = result["client_secret"]
    return Response(
        content=json.dumps(out),
        status_code=201,
        media_type="application/json",
    )


def _unix_now() -> float:
    import time
    return time.time()


@app.get("/healthz")
def healthz():
    """Unauthenticated liveness probe for the container HEALTHCHECK
    + external uptime monitors.  Bypasses ``current_actor`` (see
    ``_AUTH_BYPASS_PATHS``) so it works without an
    ``X-Forwarded-Email`` header from inside the container, and
    returns no tenant data — just a fixed shape so curl --fail
    succeeds when the process is up."""
    return {"ok": True, "version": VERSION, "git_sha": GIT_SHA[:7] if GIT_SHA else ""}


# ── /shared + share-mint routes (phase 6) ──────────────────────────
#
# Spec § "Sharing model".  Two sides:
# * Granters: maintainers of a tenant share a single box / item to
#   an outside email.  Mint via POST /boxes/{id}/share or
#   /items/{id}/share; revoke from /usage's outbound table.
# * Recipients: see /shared (index of inbound shares) and the
#   read-only /shared/box/{id} + /shared/item/{id} views.  Their
#   actor middleware bypass is wired in current_actor — this surface
#   doesn't widen the existing /boxes/{id} route.


@app.post("/boxes/{box_id}/share")
def share_box(
    request: Request,
    box_id: int,
    recipient_email: str = Form(...),
    role: str = Form("readonly"),
):
    actor: Actor = request.state.actor
    try:
        result = dao_shares.create(
            actor, target_kind="box", target_id=box_id,
            recipient_email=recipient_email, role=role,
        )
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if "application/json" in request.headers.get("accept", ""):
        return {"ok": True, "share_id": result["id"], "role": result["role"]}
    return RedirectResponse(f"/boxes/{box_id}", status_code=303)


@app.post("/items/{item_id}/share")
def share_item(
    request: Request,
    item_id: int,
    recipient_email: str = Form(...),
    role: str = Form("readonly"),
):
    actor: Actor = request.state.actor
    try:
        result = dao_shares.create(
            actor, target_kind="item", target_id=item_id,
            recipient_email=recipient_email, role=role,
        )
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if "application/json" in request.headers.get("accept", ""):
        return {"ok": True, "share_id": result["id"], "role": result["role"]}
    # Item-detail target rendered by the modal on the parent box —
    # send the granter back there with the item anchor preserved so
    # the modal reopens after the redirect.
    try:
        item = dao_items.get_by_id(actor, item_id)
        target = f"/boxes/{item['box_id']}#item-{item_id}"
    except NotFoundError:
        target = "/usage"
    return RedirectResponse(target, status_code=303)


@app.post("/shares/{share_id}/revoke")
def revoke_share(request: Request, share_id: int):
    actor: Actor = request.state.actor
    try:
        dao_shares.revoke(actor, share_id)
    except NotFoundError:
        raise HTTPException(404)
    except ForbiddenError:
        raise HTTPException(403)
    return RedirectResponse("/usage", status_code=303)


@app.get("/shared", response_class=HTMLResponse)
def shared_index(request: Request):
    """Recipient view: every inbound active share, grouped per
    granting tenant.  Soft-deleted granting tenants are filtered
    out by the DAO so the recipient sees them as gone."""
    actor: Actor = request.state.actor
    shares = dao_shares.list_for_recipient(actor.email)
    return templates.TemplateResponse(
        request, "shared.html",
        {"shares": shares, "current_email": actor.email},
    )


@app.get("/shared/box/{box_id}", response_class=HTMLResponse)
def shared_box(request: Request, box_id: int):
    """Read-only recipient view of a shared box.  Tenant members
    should still use /boxes/{id} — that route carries the full
    edit surface; this one renders the items grid only."""
    actor: Actor = request.state.actor
    box = dao_shares.fetch_box_for_recipient(actor, box_id)
    if box is None:
        raise HTTPException(404)
    items = dao_shares.fetch_box_items_for_recipient(actor, box_id)
    return templates.TemplateResponse(
        request, "shared_box.html",
        {"box": box, "items": items},
    )


@app.get("/shared/item/{item_id}", response_class=HTMLResponse)
def shared_item(request: Request, item_id: int):
    actor: Actor = request.state.actor
    item = dao_shares.fetch_item_for_recipient(actor, item_id)
    if item is None:
        raise HTTPException(404)
    return templates.TemplateResponse(
        request, "shared_item.html",
        {"item": item},
    )


# ── /admin — operator dashboard (phase 12) ─────────────────────────
#
# Spec § "Operator surface".  Hard rule honoured here: operators see
# counts + metadata + lifecycle controls, *never* tenant content.
# `/admin` gates on `actor.is_operator`; routes raise 404 (not 403)
# for non-operators so the surface's existence stays opaque to users.


def _group_oauth_tokens(tokens: list[dict]) -> list[dict]:
    """Roll up OAuth-issued tokens by (tenant_id, oauth_client_id) so
    the /admin panel renders one card per real "source" instead of
    one row per access-token mint.  claude.ai's MCP connector
    spawns a fresh access token on every reach-out; without
    grouping the table grows by one row per call.

    Manual tokens (oauth_client_id IS NULL) are left out — they
    don't have an external "source", so they're rendered in the
    per-row table as before.

    Returned shape:
        [{
          "tenant_id": ..., "tenant_name": ...,
          "oauth_client_id": ..., "oauth_client_name": ...,
          "active": N, "revoked": M, "suspended": K, "total": T,
          "latest_used_at": iso8601 | None,
          "latest_created_at": iso8601 | None,
        }, ...]
    """
    buckets: dict[tuple, dict] = {}
    for t in tokens:
        cid = t.get("oauth_client_id")
        if not cid:
            continue
        key = (t.get("tenant_id"), cid)
        bucket = buckets.setdefault(key, {
            "tenant_id": t.get("tenant_id"),
            "tenant_name": t.get("tenant_name"),
            "oauth_client_id": cid,
            "oauth_client_name": t.get("oauth_client_name") or cid,
            "active": 0, "revoked": 0, "suspended": 0, "total": 0,
            "latest_used_at": None,
            "latest_created_at": None,
        })
        bucket["total"] += 1
        if t.get("revoked_at"):
            bucket["revoked"] += 1
        elif t.get("suspended_at"):
            bucket["suspended"] += 1
        else:
            bucket["active"] += 1
        for stamp_key in ("latest_used_at", "latest_created_at"):
            src = "last_used_at" if stamp_key == "latest_used_at" else "created_at"
            v = t.get(src)
            if v and (bucket[stamp_key] is None or v > bucket[stamp_key]):
                bucket[stamp_key] = v
    # Sort: most-active groups first (active desc), then most-recent.
    return sorted(
        buckets.values(),
        key=lambda b: (-b["active"], -(b["total"]),
                       b["latest_used_at"] or ""),
    )


def _require_operator_route(actor: Actor) -> None:
    """404 — not 403 — when a non-operator probes ``/admin``.  We
    don't want a curious tenant maintainer to learn the operator
    URL space exists; for them, ``/admin`` should look exactly like
    any other unrouted path."""
    if not actor.is_operator:
        raise HTTPException(404)


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(
    request: Request,
    invite_url: str = "",
    backup_status: str = "",
):
    """Per-deployment tenant roster + cross-tenant token panel.
    ``invite_url`` round-trips a freshly-minted invite link from
    the create-tenant POST.  ``backup_status`` surfaces the result
    of a manually-triggered B2 upload."""
    actor: Actor = request.state.actor
    _require_operator_route(actor)
    tenants = dao_tenants.list_all(actor)
    # Bucket open invites by tenant_id so each tenant card can
    # surface the actual invite URLs.  The original
    # "you minted an invite — here's the URL" panel only renders
    # once, on the POST-create redirect; an operator who navigates
    # away (or misses the copy on first paint) had no way to fish
    # the link back out short of opening the DB by hand.
    open_invites_by_tenant: dict[int, list[dict]] = {}
    for inv in dao_invites.list_open_for_operator(actor):
        open_invites_by_tenant.setdefault(inv["tenant_id"], []).append(inv)
    base = (PUBLIC_URL or "").rstrip("/") or str(request.base_url).rstrip("/")
    # Decorate each tenant row with its current effective caps + the
    # member roster (so the operator gets per-email last_active_at
    # without a follow-up click into a per-tenant page).
    for t in tenants:
        t["caps"] = dao_quotas.get_caps(t["id"])
        t["usage"] = dao_quotas.usage_for_tenant(t["id"])
        t["members"] = dao_tenants.list_members(actor, t["id"])
        t["open_invite_list"] = [
            {**inv, "url": f"{base}/invite/{inv['token']}"}
            for inv in open_invites_by_tenant.get(t["id"], [])
        ]
    handles = dao_handles.list_all_for_operator(actor)
    api_tokens = dao_api_tokens.list_all_for_operator(actor)
    oauth_client_groups = _group_oauth_tokens(api_tokens)
    recent_activity = dao_audit.list_recent_for_operator(actor, limit=50)
    vendor_cost = dao_usage.operator_cost_summary(actor)
    oauth_clients = dao_oauth.list_clients(actor)
    feedback_queue = dao_feedback.list_for_operator(limit=50)
    feedback_counts = dao_feedback.queue_counts()
    try:
        dao_backups._b2_config()
        b2_configured = True
    except dao_backups.B2NotConfiguredError:
        b2_configured = False
    return templates.TemplateResponse(
        request, "admin.html",
        {
            "tenants": tenants,
            "handles": handles,
            "api_tokens": api_tokens,
            "oauth_client_groups": oauth_client_groups,
            "recent_activity": recent_activity,
            "vendor_cost": vendor_cost,
            "oauth_clients": oauth_clients,
            "feedback_queue": feedback_queue,
            "feedback_counts": feedback_counts,
            "current_email": actor.email,
            "invite_url": invite_url,
            "public_url": PUBLIC_URL,
            "b2_configured": b2_configured,
            "backup_status": backup_status,
        },
    )


@app.post("/admin/oauth-clients/revoke-tokens")
def admin_revoke_oauth_client_tokens(
    request: Request,
    oauth_client_id: str = Form(...),
    tenant_id: int = Form(...),
):
    """Revoke every active token for one OAuth client + tenant in
    a single click — kills the "claude.ai keeps minting access
    tokens" clutter without N individual revokes."""
    actor: Actor = request.state.actor
    _require_operator_route(actor)
    dao_api_tokens.operator_revoke_client_tokens(
        actor, oauth_client_id, tenant_id,
    )
    return RedirectResponse("/admin#tokens", status_code=303)


@app.post("/admin/api-tokens/{token_id}/revoke")
def admin_revoke_api_token(request: Request, token_id: int):
    """Operator-driven revoke of any tenant's API token.  Records
    ``operator_revoke`` as the reason so the originating tenant
    can see the kill came from the operator and not their own
    /usage page."""
    actor: Actor = request.state.actor
    _require_operator_route(actor)
    try:
        dao_api_tokens.operator_revoke(actor, token_id)
    except NotFoundError:
        raise HTTPException(404)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/api-tokens/{token_id}/suspend")
def admin_suspend_api_token(request: Request, token_id: int):
    """Temporary pause — auth fails until resume.  Use case:
    "I think this token might be compromised but I'm not sure"."""
    actor: Actor = request.state.actor
    _require_operator_route(actor)
    try:
        dao_api_tokens.operator_suspend(actor, token_id)
    except NotFoundError:
        raise HTTPException(404)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/api-tokens/{token_id}/resume")
def admin_resume_api_token(request: Request, token_id: int):
    actor: Actor = request.state.actor
    _require_operator_route(actor)
    try:
        dao_api_tokens.operator_resume(actor, token_id)
    except NotFoundError:
        raise HTTPException(404)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/tenants/{tenant_id}/quotas")
def admin_set_quotas(
    request: Request,
    tenant_id: int,
    monthly_ai_calls: str = Form(""),
    monthly_upload_bytes: str = Form(""),
    daily_ai_cost_micros: str = Form(""),
):
    """Operator quota override editor.  Empty form fields leave
    the existing override unchanged; a literal ``-1`` clears the
    field (reverts to plan default).  Numbers go through the DAO,
    which audit-logs ``quota.override``."""
    actor: Actor = request.state.actor
    _require_operator_route(actor)

    def _parse(v: str) -> int | None:
        v = v.strip()
        if not v:
            return None
        try:
            return int(v)
        except ValueError:
            raise HTTPException(400, f"Cap value must be integer or empty: {v!r}")

    try:
        dao_quotas.set_overrides(
            actor, tenant_id,
            monthly_ai_calls=_parse(monthly_ai_calls),
            monthly_upload_bytes=_parse(monthly_upload_bytes),
            daily_ai_cost_micros=_parse(daily_ai_cost_micros),
        )
    except ForbiddenError:
        raise HTTPException(404)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/tenants/{tenant_id}/backup")
def admin_backup_to_b2(request: Request, tenant_id: int):
    """Operator-triggered B2 upload of a tenant's backup zip.
    Manual surface only — the nightly cron lands later (roadmap
    markers in spec.md).  Builds the zip, uploads, audits, redirects
    back to /admin with a backup_status flag for the flash."""
    actor: Actor = request.state.actor
    _require_operator_route(actor)
    try:
        result = dao_backups.upload_tenant_to_b2_as_operator(
            actor.email, tenant_id,
        )
    except dao_backups.B2NotConfiguredError as exc:
        raise HTTPException(503, str(exc))
    except Exception as exc:  # noqa: BLE001
        # Anything from boto3 (auth, network, bucket) — fail loud
        # rather than silently leaving the audit trail short.
        raise HTTPException(502, f"B2 upload failed: {exc}")
    from urllib.parse import urlencode
    flash = f"uploaded {result['key']} ({result['size']} bytes)"
    return RedirectResponse(
        f"/admin?{urlencode({'backup_status': flash})}",
        status_code=303,
    )


# ── Tenant lifecycle ─────────────────────────────────────────────


@app.post("/admin/tenants/{tenant_id}/plan")
def admin_set_tenant_plan(
    request: Request,
    tenant_id: int,
    plan: str = Form(...),
    reason: str = Form(""),
):
    """Operator-side plan override.  Bypasses Stripe entirely so an
    operator can comp friends/family/beta-testers to Pro without
    making them go through checkout.  The Stripe webhook is
    unaffected — a real subscription that later cancels still
    flips back to free via the usual billing event flow.

    Logged with the operator's email + the optional reason
    string for audit-log traceability."""
    actor: Actor = request.state.actor
    _require_operator_route(actor)
    try:
        dao_tenants.operator_set_plan(
            actor, tenant_id, plan, reason=reason,
        )
    except NotFoundError:
        raise HTTPException(404)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return RedirectResponse("/admin#tenants", status_code=303)


@app.post("/admin/tenants/{tenant_id}/soft-delete")
def admin_soft_delete_tenant(request: Request, tenant_id: int):
    """Operator-only: mark a tenant soft-deleted (30-day grace).
    Members can still see their data; outbound shares pause; the
    eventual hard-delete sweep reads ``hard_delete_after`` to
    decide when to drop the rows.  Reactivate or hard-delete via
    the sibling endpoints."""
    actor: Actor = request.state.actor
    _require_operator_route(actor)
    try:
        dao_tenants.soft_delete(actor, tenant_id)
    except NotFoundError:
        raise HTTPException(404)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/tenants/{tenant_id}/reactivate")
def admin_reactivate_tenant(request: Request, tenant_id: int):
    """Operator-only: clear a soft-delete, restoring the tenant's
    active state.  No-op on an already-active tenant."""
    actor: Actor = request.state.actor
    _require_operator_route(actor)
    try:
        dao_tenants.reactivate(actor, tenant_id)
    except NotFoundError:
        raise HTTPException(404)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/tenants/{tenant_id}/hard-delete")
def admin_hard_delete_tenant(
    request: Request,
    tenant_id: int,
    confirm: str = Form(""),
):
    """Operator-only: permanently delete a tenant and everything
    that references it.  Requires the form to carry
    ``confirm=<tenant_name>`` matching the tenant's current name
    so an accidental click can't nuke a tenant.  All cascades
    fire (boxes, items, rooms, audit_log rows for this tenant,
    …) — there's no undo here."""
    actor: Actor = request.state.actor
    _require_operator_route(actor)
    with db() as conn:
        row = conn.execute(
            "SELECT name FROM tenants WHERE id = ?", (tenant_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404)
    if (confirm or "").strip() != row["name"]:
        raise HTTPException(
            400,
            "Confirmation required: type the tenant name exactly to confirm.",
        )
    try:
        dao_tenants.hard_delete(actor, tenant_id)
    except NotFoundError:
        raise HTTPException(404)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/oauth-clients/{client_id}/revoke")
def admin_revoke_oauth_client(request: Request, client_id: str):
    """Operator-only: revoke a registered OAuth client.  Existing
    access tokens issued under it stay valid until natural
    expiry (we don't iterate api_tokens to mass-revoke); no new
    auth-code or refresh exchange will succeed."""
    actor: Actor = request.state.actor
    _require_operator_route(actor)
    try:
        dao_oauth.revoke_client(actor, client_id)
    except NotFoundError:
        raise HTTPException(404)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/tenants")
def admin_create_tenant_and_invite(
    request: Request,
    name: str = Form(...),
    invitee_email: str = Form(...),
    role: str = Form("maintainer"),
    plan: str = Form("free"),
):
    """Bootstrap a fresh tenant + mint the first-maintainer invite
    in one shot.  The operator never becomes a member; the invitee
    will be the sole maintainer once they accept.  Until acceptance
    the new tenant has zero members and shows ``open_invites=1``
    on the dashboard."""
    actor: Actor = request.state.actor
    _require_operator_route(actor)
    name = name.strip()
    if not name:
        raise HTTPException(400, "Tenant name required")
    # Per-IP throttle: 5/hour by default.  Defends against a
    # stolen operator credential being scripted into mass
    # tenant creation.  Caddy stamps the real client IP on
    # ``X-Forwarded-For``; ``request.client.host`` is the
    # fallback for direct deploys.
    client_ip = (
        (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        or (request.client.host if request.client else "")
    )
    try:
        dao_quotas.check_tenant_creation_rate(client_ip)
    except dao_quotas.QuotaExceeded as exc:
        raise HTTPException(
            429,
            f"Tenant creation rate limit hit ({exc.used} ≥ {exc.cap} "
            "per hour from this IP).  Wait for the window reset.",
        )
    try:
        tenant_id = dao_tenants.create_tenant(
            actor, name, plan=plan, client_ip=client_ip,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    try:
        invite = dao_invites.create(
            actor, email=invitee_email, role=role, tenant_id=tenant_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    base = PUBLIC_URL or str(request.base_url).rstrip("/")
    url = f"{base}/invite/{invite['token']}"
    from urllib.parse import urlencode
    return RedirectResponse(
        f"/admin?{urlencode({'invite_url': url})}",
        status_code=303,
    )


@app.get("/maintenance", response_class=HTMLResponse)
def maintenance(request: Request, cleaned: str = "", update: str = "", imported: str = ""):
    actor: Actor = request.state.actor
    with db() as conn:
        item_count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        box_count = conn.execute("SELECT COUNT(*) FROM boxes").fetchone()[0]
        tenant_name = None
        members = []
        if actor.tenant_id is not None:
            row = conn.execute(
                "SELECT name FROM tenants WHERE id = ?", (actor.tenant_id,),
            ).fetchone()
            tenant_name = row["name"] if row else None
            members = [
                dict(r) for r in conn.execute(
                    "SELECT email, role FROM tenant_members "
                    "WHERE tenant_id = ? ORDER BY joined_at, email",
                    (actor.tenant_id,),
                ).fetchall()
            ]
    on_disk = sum(1 for _ in UPLOAD_DIR.iterdir()) if UPLOAD_DIR.exists() else 0
    referenced = len(_referenced_uploads())
    return templates.TemplateResponse(
        request, "maintenance.html",
        {
            "item_count": item_count, "box_count": box_count,
            "files_on_disk": on_disk, "files_referenced": referenced,
            "orphan_count": max(0, on_disk - referenced),
            "cleaned": cleaned,
            "imported": imported,
            "version": VERSION,
            "git_sha": GIT_SHA[:7] if GIT_SHA else "",
            "update_enabled": bool(WATCHTOWER_URL),
            "update_status": update,
            "changelog_html": CHANGELOG_HTML,
            # Access panel data: the active tenant + its members, replacing
            # the old global STASH_ALLOWED_EMAILS view.  This whole card
            # moves to /usage in roadmap step 13; until then we keep it on
            # /maintenance with tenant-scoped data so the live user has
            # parity with the old behaviour.
            "tenant_name": tenant_name,
            "tenant_members": members,
            "current_email": actor.email,
            "current_role": actor.role,
            "is_operator": actor.is_operator,
        },
    )


@app.post("/maintenance/update")
def maintenance_update(background: BackgroundTasks):
    if not WATCHTOWER_URL:
        return RedirectResponse("/maintenance?update=disabled", status_code=303)
    background.add_task(_trigger_watchtower_update)
    return RedirectResponse("/maintenance?update=triggered", status_code=303)


@app.get("/maintenance/version")
def maintenance_version():
    """Lightweight probe for client-side polling. The maintenance page snapshots
    the version on load and watches this endpoint for a change, which signals
    that watchtower has finished restarting the container with a new image."""
    return {
        "version": VERSION,
        "git_sha": GIT_SHA[:7] if GIT_SHA else "",
    }


@app.post("/maintenance/cleanup")
def maintenance_cleanup():
    refs = _referenced_uploads()
    cleaned = 0
    if UPLOAD_DIR.exists():
        # Walk per-tenant subdirectories — every upload after phase 2
        # lives at UPLOAD_DIR/{tenant_id}/{name}.  Anything in the flat
        # root is either a leftover .tmp from an interrupted write or a
        # pre-migration orphan; both get cleaned.
        for entry in UPLOAD_DIR.iterdir():
            if entry.is_dir() and entry.name.isdigit():
                tid = int(entry.name)
                for path in entry.iterdir():
                    if path.is_file() and (tid, path.name) not in refs:
                        try:
                            path.unlink()
                            cleaned += 1
                        except FileNotFoundError:
                            pass
            elif entry.is_file():
                # Stray file in the flat root (pre-migration leftover or
                # broken write).  Always orphan in the post-phase-2
                # world.
                try:
                    entry.unlink()
                    cleaned += 1
                except FileNotFoundError:
                    pass
    return RedirectResponse(f"/maintenance?cleaned={cleaned}", status_code=303)


@app.get("/maintenance/export")
def maintenance_export():
    """Stream a zip of stash.db + every upload file still referenced.

    Files are written into the zip under their on-disk relative path
    (`uploads/{tenant_id}/{name}`).  The blobs are stored as ciphertext —
    the zip is a coherent snapshot of the on-disk state, not a cleartext
    dump.  Restoring this zip without the matching STASH_KEK is useless,
    by design (see spec § "Encryption at rest")."""
    import io as _io
    import zipfile
    from datetime import datetime

    # Drain the WAL into main.db so the zip captures every committed
    # write. In WAL mode, recent UPDATEs can sit in the -wal sidecar
    # file until checkpointed; zipping just stash.db without this would
    # ship a snapshot that's missing the latest changes (and the next
    # restore would silently roll the user back).
    with db() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    refs = _referenced_uploads()
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if DB_PATH.exists():
            zf.write(DB_PATH, arcname="stash.db")
        for tid, name in sorted(refs):
            p = _tenant_file(tid, name)
            if p.exists():
                zf.write(p, arcname=f"uploads/{tid}/{name}")
    buf.seek(0)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="stash-backup-{stamp}.zip"'},
    )


_SQLITE_MAGIC = b"SQLite format 3\x00"
_ZIP_MAGIC = b"PK\x03\x04"

# Backup restores carry every referenced photo; a 2GB ceiling keeps DoS-on-disk
# bounded while comfortably covering realistic stashes. Caddy is configured to
# permit the same on /maintenance/import.
MAX_IMPORT_BYTES = 2 * 1024 * 1024 * 1024


def _validate_sqlite_file(path: Path) -> None:
    """Raise HTTPException unless `path` is a SQLite DB with a `boxes` table."""
    try:
        conn = sqlite3.connect(path)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='boxes'"
            ).fetchone()
            if not row:
                raise HTTPException(400, "Database is missing the boxes table — not a stash backup")
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise HTTPException(400, f"Database failed integrity check: {integrity[0] if integrity else 'unknown'}")
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        raise HTTPException(400, f"Not a valid SQLite database: {e}")


def _backup_current_db() -> Path | None:
    """Copy the live DB to stash.db.bak-<timestamp> so a bad import is recoverable."""
    if not DB_PATH.exists():
        return None
    from datetime import datetime
    import shutil
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DB_PATH.with_name(f"{DB_PATH.name}.bak-{stamp}")
    shutil.copy2(DB_PATH, backup)
    return backup


def _replace_db_from_path(src_db: Path) -> None:
    """Validate the SQLite file at `src_db` and copy it into the live DB location.

    Uses SQLite's online backup API rather than `os.replace` — on Windows the
    live DB file is held open by any active connection, which makes a rename
    fail with `PermissionError`. `Connection.backup()` swaps page contents
    through the SQLite library and is safe even with concurrent readers.

    With WAL mode enabled on the live DB, pages written by the wipe-step
    transactions (DELETE FROMs etc.) live in the WAL until checkpointed.
    `backup()` overwrites the main DB file's contents from the source but
    doesn't sync the WAL — so subsequent reads can see stale WAL pages
    layered over the freshly-restored main pages. A FULL checkpoint +
    TRUNCATE before close forces the WAL to drain into main and resets
    its size to zero, so the next connection opens a coherent file."""
    with open(src_db, "rb") as f:
        header = f.read(len(_SQLITE_MAGIC))
    if not header.startswith(_SQLITE_MAGIC):
        raise HTTPException(400, "File is not a SQLite database")
    _validate_sqlite_file(src_db)
    _backup_current_db()
    src = sqlite3.connect(src_db)
    dst = sqlite3.connect(DB_PATH)
    try:
        src.backup(dst)
        dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        src.close()
        dst.close()
    migrate_db()


def _extract_db_member_to_path(zf, member: str, dst: Path) -> None:
    """Stream a zip member to disk in 1MB chunks (no full decompress in memory)."""
    import shutil
    with zf.open(member) as src, open(dst, "wb") as out:
        shutil.copyfileobj(src, out, length=1024 * 1024)


def _restore_uploads_from_zip(zf) -> int:
    """Extract upload entries into the per-tenant directory layout.

    Accepts both shapes the export has used:
    * Phase-2+ zips: ``uploads/{tenant_id}/{name}`` (encrypted blob).
    * Pre-phase-2 legacy zips: ``uploads/{name}`` (cleartext); these
      get dropped into the flat root so the migration step can
      relocate + encrypt them on next startup.

    Skips entries with hostile names (path traversal, suspicious
    chars).  Returns count restored."""
    import shutil
    upload_root = UPLOAD_DIR.resolve()
    count = 0
    for name in zf.namelist():
        if not name.startswith("uploads/") or name.endswith("/"):
            continue
        rel = name[len("uploads/"):]
        if ".." in rel or "\\" in rel:
            continue

        parts = rel.split("/", 1)
        if len(parts) == 2 and parts[0].isdigit():
            # Phase-2+ layout: uploads/{tenant_id}/{name}.
            tid_segment, base = parts
            if not _UPLOAD_NAME_RE.match(base):
                continue
            target_dir = UPLOAD_DIR / tid_segment
            target_dir.mkdir(parents=True, exist_ok=True)
            target = (target_dir / base).resolve()
        else:
            # Legacy flat layout; restore into the root so the next
            # migrate_db relocates + encrypts.
            base = rel
            if "/" in base or not _UPLOAD_NAME_RE.match(base):
                continue
            target = (UPLOAD_DIR / base).resolve()

        if not target.is_relative_to(upload_root):
            continue
        with zf.open(name) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out, length=1024 * 1024)
        count += 1
    return count


async def _spool_upload_to_disk(file: UploadFile, dst: Path, max_bytes: int) -> int:
    """Stream `file` to `dst` in chunks. Aborts with 413 if it exceeds `max_bytes`."""
    size = 0
    chunk_size = 1024 * 1024
    with open(dst, "wb") as out:
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise HTTPException(413, "Upload too large")
            out.write(chunk)
    return size


@app.post("/maintenance/import")
async def maintenance_import(file: UploadFile = File(...)):
    """Replace the live DB (and optionally uploads/) from a `.db` or backup `.zip`.

    The current DB is copied to `stash.db.bak-<timestamp>` before replacement so
    a botched import can be reverted by hand. Streams the upload to disk rather
    than buffering — backup zips can be multi-GB on photo-heavy stashes."""
    if not file or not file.filename:
        raise HTTPException(400, "File required")

    spool = DB_PATH.with_name(f"{DB_PATH.name}.import-upload")
    try:
        size = await _spool_upload_to_disk(file, spool, MAX_IMPORT_BYTES)
        if size == 0:
            raise HTTPException(400, "File is empty")

        with open(spool, "rb") as f:
            header = f.read(max(len(_SQLITE_MAGIC), len(_ZIP_MAGIC)))

        if header.startswith(_ZIP_MAGIC):
            import zipfile
            try:
                zf = zipfile.ZipFile(spool)
            except zipfile.BadZipFile:
                raise HTTPException(400, "File is not a valid zip archive")
            with zf:
                if "stash.db" not in zf.namelist():
                    raise HTTPException(400, "Zip is missing stash.db — not a stash backup")
                db_tmp = DB_PATH.with_name(f"{DB_PATH.name}.zipdb-tmp")
                try:
                    _extract_db_member_to_path(zf, "stash.db", db_tmp)
                    _replace_db_from_path(db_tmp)
                finally:
                    try:
                        db_tmp.unlink()
                    except FileNotFoundError:
                        pass
                _restore_uploads_from_zip(zf)
        elif header.startswith(_SQLITE_MAGIC):
            _replace_db_from_path(spool)
        else:
            raise HTTPException(400, "File must be a SQLite .db or a stash backup .zip")
    finally:
        try:
            spool.unlink()
        except FileNotFoundError:
            pass

    return RedirectResponse("/maintenance?imported=1", status_code=303)


# ── Module bottom: schema + filesystem migrations run last ───────────
# Every helper they need is defined above (encryption helpers,
# _tenant_file, _migrate_uploads_to_encrypted_tenant_dirs).  Keeping
# the calls here means the ordering between helper-definition and
# helper-use never trips someone up.
init_db()
migrate_db()
# Any ingest_jobs row in 'processing' at boot can only be orphaned —
# our worker runs in-process (FastAPI BackgroundTasks), so a row
# carrying that state across a restart means the previous process
# died (or hung indefinitely) before completing it.  Flip them to
# 'failed' so the UI surfaces a Retry / Dismiss button instead of
# a permanent spinner.
with db() as _conn:
    _orphaned = _conn.execute(
        "UPDATE ingest_jobs SET status='failed', "
        "  error='worker orphaned (process restart) — retry to re-run', "
        "  completed_at=CURRENT_TIMESTAMP "
        "WHERE status='processing'"
    ).rowcount
    _conn.commit()
if _orphaned:
    obs.get_logger("stash.ingest").warning(
        "ingest.orphan_sweep cleared=%s", _orphaned,
    )
