# Stash · Multi-tenancy + monetisation spec

**Status:** planning. One live user today, owning all existing rows. The
work below is sequenced so that user becomes "Personal tenant, sole
maintainer member" with zero hand-touching.

## Goals

- Support multiple tenants on one deployment, with strict isolation:
  data, files, tags, and AI usage are per-tenant.
- Make tenancy enforcement a layered guarantee — the request layer
  AND the data-access layer both check, so a missed check on one side
  can't leak data across tenants.
- **Encrypt user data on disk so that operators cannot trivially
  browse another person's files.** Per-tenant keys, decryption on
  the fly at access time, deliberate-and-audited recovery path.
  Disk-is-encrypted-by-the-host doesn't count — that's
  cargo-cult security.
- Allow tenants to invite users, scope role-based access (maintainer
  vs readonly), and explicitly share individual boxes or items with an
  outside user.
- Track per-tenant usage (AI calls, storage, backups) with soft quota
  enforcement that degrades gracefully — when AI is capped, browsing
  still works.
- Per-tenant backups with off-site disaster recovery via Backblaze B2.
  Soft-delete tenants with a generous grace period and retain one
  off-site backup beyond it — losing someone's photos is the worst.
- Move global maintenance to an operator-only admin surface; the
  operator surface deliberately does NOT grant data access to
  tenants. Replace `/maintenance` on the user side with a per-tenant
  usage page that includes price transparency.
- Self-serve sign-up, real notification delivery, anti-abuse on the
  free tier, optimistic-concurrency mutations, API tokens, and
  WAL-mode SQLite concurrency tuning — the operational scaffolding
  any "real" SaaS needs but the spec was missing in the first
  draft.

## Ethos: "software that doesn't hate you"

> *"None of this platform is about data mining or taking advantage of
> end users. It's about being able to live your life a bit better
> and know where your shit is."*

Stash takes a deliberately unfashionable position on pricing, trust,
and the data on disk:

- **Show your work.** Every paid tenant's usage page breaks the bill
  into five line items that always add up to what they paid:
  1. **Direct vendor passthrough** — Gemini calls, Anthropic
     calls, Backblaze storage for *this tenant*, host share
     apportioned by storage footprint.
  2. **Community backups** — this tenant's slice of retaining B2
     backups for everyone, including free-tier and post-grace
     archived backups for users we've already let go.
  3. **Community free-tier** — this tenant's slice of the free
     users we subsidise. Small free tier, small subsidy; the line
     is explicit so the size of the subsidy is honest.
  4. **Operator payout** — what is paid to the humans running the
     platform (devs, ops, security, support).
  5. **Margin** — small reinvestment buffer. Goal is "in the
     black, but small."
  A public `/about/pricing` page reports the same numbers in
  aggregate (vendor totals, total operator payout, total margin).
  The two views must reconcile — what every tenant sees on their
  bill, summed, equals what the public page reports.
- **Free tier is genuinely usable.** Initial caps: **100 MB photo
  storage, one retained backup, modest AI quota** (placeholder until
  we have telemetry; see "Open decisions"). Caps may shrink as the
  free base grows, but the tier doesn't disappear.
- **Operators can't read your data.** Two layers:
  1. Support requests go through the user inviting the operator
     into their tenant — same path as inviting a partner. No "view
     as user" mode in the admin dashboard.
  2. User photos and thumbnails are encrypted on disk with
     per-tenant keys (see "Encryption at rest"). A casual `cat` on
     the upload directory returns ciphertext. The operator path to
     decrypt is a CLI tool that audits every invocation and emails
     the affected tenant.
- **Soft-delete by default.** A deleted tenant enters a grace period
  during which the user can sign back in and reactivate; their B2
  backup is retained beyond that for catastrophic recovery.

## Non-goals (for this round)

- A general-purpose RBAC engine. Two roles is enough.
- Object-level ACLs beyond box / item shares. Rooms and floors travel
  with their tenant; they're not individually shareable.
- A full billing / Stripe surface. Quotas exist; payment doesn't yet.
- Public sharing (anonymous URLs). All shares go to a known email.
- Replacing the support-surface gap from the original plan — that
  intentionally deferred.

## Terminology

- **Tenant**: an isolated stash. Owns boxes, items, locations, floors,
  rooms, tags, ingest jobs, and uploads. Has an associated quota.
- **Member**: a `(tenant_id, email)` pair with a role. A user may be a
  member of multiple tenants.
- **Maintainer**: full read/write within a tenant.
- **Readonly**: read + search only. Cannot mutate, ingest, share, or
  invite.
- **Operator**: a global role configured via `STASH_OPERATOR_EMAILS`.
  Can hit `/admin` for cross-tenant views and disaster recovery. Not a
  member of any tenant by default — operators must be explicitly
  invited to a tenant if they want to use it as one.
- **Share**: an object-level grant. A maintainer of tenant A can grant
  a specific email view-or-edit access to one box or one item. The
  recipient sees shared content in a "Shared with you" view, separate
  from their own tenant(s).
- **Actor**: the resolved identity of the current request — email,
  tenant context, role. Computed by middleware and stashed on
  `request.state`.

## Architecture

### Layer responsibilities

| Layer | Owns |
|---|---|
| Reverse proxy (oauth2-proxy) | Sign-in. Rejects non-allow-listed emails before they reach the app. |
| Actor middleware | Resolves email → tenant context + role. Selects active tenant when the user is in multiple. 403s when no membership. |
| REST / API routes | Coarse role checks (e.g. mutations refuse readonly actors). Picks the DAO call to make. |
| DAO | The only thing that talks to SQLite. Every method takes an actor and applies `tenant_id` filters. Refuses operations the actor's role can't perform. |
| DB schema | `tenant_id NOT NULL` on every owned table. Indexes lead with `tenant_id`. |
| Filesystem | `UPLOAD_DIR/{tenant_id}/...`. Serve handler validates the actor's tenant matches the path before opening the file. |

The dual-layer enforcement is deliberate: the route check is fast and
catches obviously-wrong access, the DAO check is the source of truth
and survives a forgotten guard on a new endpoint.

### Schema additions

```text
tenants(
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    plan TEXT NOT NULL DEFAULT 'free',  -- 'free' | 'pro' | 'operator'
    created_at TEXT
)

tenant_members(
    tenant_id INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
    email TEXT COLLATE NOCASE,
    role TEXT NOT NULL,           -- 'maintainer' | 'readonly'
    invited_by_email TEXT,
    invited_at TEXT,
    joined_at TEXT,
    PRIMARY KEY (tenant_id, email)
)

tenant_invites(
    token TEXT PRIMARY KEY,
    tenant_id INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
    email TEXT COLLATE NOCASE NOT NULL,
    role TEXT NOT NULL,
    created_by_email TEXT NOT NULL,
    created_at TEXT,
    expires_at TEXT,
    consumed_at TEXT
)

-- Operator-minted self-onboarding link.  Kept separate from
-- tenant_invites so the recipient names their own tenant on
-- accept (the maintainer-invites-someone-into-an-existing-
-- tenant flow above still uses tenant_invites).  The plan is
-- locked at mint; the link is single-use via the consumed_at
-- sentinel; consumed_tenant_id traces the link to the resulting
-- tenant for audit.
tenant_bootstrap_invites(
    token TEXT PRIMARY KEY,
    plan TEXT NOT NULL,                -- 'free' | 'pro'
    role TEXT NOT NULL DEFAULT 'maintainer',
    created_by_email TEXT NOT NULL,    -- the operator
    created_at TEXT,
    expires_at TEXT,
    consumed_at TEXT,
    consumed_by_email TEXT,
    consumed_tenant_id INTEGER REFERENCES tenants(id) ON DELETE SET NULL
)

-- In-app feedback widget.  Body is the user's free-text; the
-- rest only populates when the user taps "Capture this page" in
-- the widget — opt-in telemetry, never silent.  screenshot +
-- page_html are filenames in the tenant's encrypted upload dir
-- (same pipeline as item photos), so a captured DOM containing
-- unsubmitted form values can't sit cleartext on disk.
feedback(
    id INTEGER PRIMARY KEY,
    tenant_id INTEGER,                 -- nullable; anonymous paths reserved
    actor_email TEXT,
    body TEXT NOT NULL,
    screenshot TEXT,                   -- encrypted-blob filename
    source_url TEXT,
    user_agent TEXT,
    viewport_w INTEGER,
    viewport_h INTEGER,
    status TEXT NOT NULL DEFAULT 'open',  -- open | accepted | rejected | done
    urgent INTEGER NOT NULL DEFAULT 0,   -- operator-set major-blocker flag;
                                         -- urgent rows sort to the top of each
                                         -- kanban column with a 🔥 pill.
    source TEXT NOT NULL DEFAULT 'user_widget',  -- 'user_widget' | 'mcp' | 'operator' …
                                                 -- 'mcp' rows come from agents
                                                 -- driving the visual-sweep rig
                                                 -- via admin_create_feedback.
    operator_notes TEXT,
    created_at TEXT,
    resolved_at TEXT,
    resolved_by TEXT,
    -- Extended telemetry (filled in only on "Capture this page"):
    page_html TEXT,                    -- encrypted-blob filename
    console_log TEXT,                  -- JSON array of console.{error,warn} +
                                       -- window.onerror + unhandledrejection
    focused_selector TEXT,
    scroll_x INTEGER,
    scroll_y INTEGER,
    page_title TEXT,
    color_scheme TEXT,                 -- 'light' | 'dark'
    client_timestamp TEXT,
    perf_timing TEXT                   -- JSON: ttfb, dom interactive, FCP, LCP, transfer sizes
)

-- Public-leaderboard handle opt-in for feedback contributors.
-- Stars count = rows in `feedback` with status='done' and
-- matching actor_email.  No handle, no public display — the
-- email's local-part is never shown.
feedback_handles(
    actor_email TEXT PRIMARY KEY,
    handle TEXT NOT NULL,
    handle_lower TEXT NOT NULL,
    set_at TEXT,
    revoked_at TEXT                    -- operator anti-abuse
)

object_shares(
    id INTEGER PRIMARY KEY,
    tenant_id INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
    target_kind TEXT NOT NULL,    -- 'box' | 'item'
    target_id INTEGER NOT NULL,
    recipient_email TEXT COLLATE NOCASE NOT NULL,
    role TEXT NOT NULL,           -- 'maintainer' | 'readonly'
    created_by_email TEXT NOT NULL,
    created_at TEXT,
    revoked_at TEXT,
    UNIQUE (target_kind, target_id, recipient_email) WHERE revoked_at IS NULL
)

usage_events(
    id INTEGER PRIMARY KEY,
    tenant_id INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
    surface TEXT NOT NULL,        -- 'ai' | 'upload' | 'backup' | 'core' | 'mcp'
    kind TEXT NOT NULL,           -- 'gemini_detect' | 'gemini_art' | 'anthropic_match' | 'upload_bytes' | 'backup_bytes'
    units INTEGER NOT NULL,       -- request count, byte count, etc.
    cost_micros INTEGER NOT NULL DEFAULT 0,
    created_at TEXT
)
-- Index on (tenant_id, surface, created_at) for monthly rollups.

-- High-frequency counters (download bandwidth today; storage
-- snapshots when that ships).  One row per (tenant_id, day,
-- surface, kind) — UPSERT into the same row so a busy serve loop
-- writes O(1) rows/day instead of O(N) per page view.
usage_rollups(
    tenant_id INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
    day TEXT NOT NULL,            -- 'YYYY-MM-DD' UTC
    surface TEXT NOT NULL,        -- 'download'
    kind TEXT NOT NULL,           -- 'download_bytes'
    units INTEGER NOT NULL DEFAULT 0,
    cost_micros INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, day, surface, kind)
)
-- Index on (tenant_id, day) for monthly sparkline rollup.

quotas(
    tenant_id INTEGER PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    monthly_ai_calls INTEGER,
    storage_bytes INTEGER,             -- FLAT cap (current footprint), not
                                       -- monthly cumulative.  Renamed in
                                       -- 2026-05-16 from monthly_upload_bytes
                                       -- when the free tier shifted to a
                                       -- delete-to-free-space model.
    backup_storage_bytes INTEGER,
    daily_ai_cost_micros INTEGER,      -- runaway-MCP / Gemini-art guard,
                                       -- carried in overrides_json today
                                       -- (schema add deferred behind the rename).
    -- NULL on any field = no cap (operator override or paid tier).
    overrides_json TEXT
)

-- KV operator tunables.  Today: ``free_tier_bytes_total`` (default 10 GB);
-- the operator bumps this from /admin when EBS scales out, and the
-- public landing's "N of M free spots open" line reflects it on next
-- read — no restart.  Future tunables (e.g. pause new signups
-- without dropping the cap to 0) ride the same row shape.
deployment_settings(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT,
    updated_by_email TEXT
)

audit_log(
    id INTEGER PRIMARY KEY,
    tenant_id INTEGER,            -- nullable for operator-cross-tenant actions
    actor_email TEXT,
    action TEXT NOT NULL,         -- 'invite.send', 'share.create', 'share.revoke', 'tenant.delete', etc.
    target_kind TEXT,
    target_id INTEGER,
    metadata_json TEXT,
    created_at TEXT
)
```

### Schema migrations to existing tables

Every table that holds tenant-owned data gets `tenant_id INTEGER NOT
NULL` plus a leading `(tenant_id, ...)` index on hot paths:

- `locations`, `floors`, `rooms`
- `boxes`, `items`
- `tags`, `item_tags`, `pending_item_tags`
- `pending_items`, `ingest_jobs`

Junction tables (`item_tags`, `pending_item_tags`) carry a redundant
`tenant_id` column too. The denormalisation is cheap and lets every
join be guarded with the same `WHERE tenant_id = ?` predicate without
relying on the parent row.

Backfill runs once during the migration (see "Migration plan"
below): every existing row gets `tenant_id = 1`, the live user becomes
the sole maintainer of that tenant.

### Filesystem layout

Uploads move from `UPLOAD_DIR/{name}` to `UPLOAD_DIR/{tenant_id}/{name}`.

- `serve_upload` and `serve_thumb` validate that the actor's active
  tenant matches the `{tenant_id}` segment of the URL **before** any
  filesystem touch.
- `_referenced_uploads` returns scoped `(tenant_id, name)` pairs.
- The orphan sweep walks per-tenant directories.
- Backup zip writes paths as `uploads/{tenant_id}/{name}` (per-tenant
  backups only ship that tenant's prefix).

**Photo metadata strip.** `save_photo_bytes` already strips
orientation EXIF (and bakes the rotation into pixels for the vision
pass). Extend the strip to ALL EXIF segments before save: GPS
coords, camera serial, Adobe XMP, raw-profile blobs, MakerNote.
A parent uploading a kid's birthday photo should not be uploading
the home GPS pin.

Migration: `mv` every existing file into `UPLOAD_DIR/1/...`, then
re-write each through the encrypt path (see below) before
reverifying with a checksum + cleanup of the cleartext originals.

### Encryption at rest

The disk-is-encrypted-by-the-host story is cargo-cult security: it
means every operator can browse every tenant's photos with a `cat`,
and the platform's "we won't look at your stuff" claim is
unverifiable. Stash encrypts user data on disk so that casual access
through the filesystem returns ciphertext, and the operator path to
decrypt is deliberate, audited, and visible to the affected tenant.

**Threat model.** Stop a snooping operator, a stolen disk, an errant
log-tail. Allow the operator to assist with documented, audited
recovery when the user asks. NOT aimed at nation-state-resistant
trustlessness — that is a different product.

**Design (envelope encryption).**

- Each tenant gets a 256-bit Data Encryption Key (DEK) generated
  server-side at tenant creation.
- The DEK is encrypted by a Key Encryption Key (KEK) and stored as
  a wrapped blob in the `tenants` row.
- The KEK lives outside the application's own database and uploads
  directory — `STASH_KEK` env var, with the KEK separately backed
  up to a different B2 bucket (and ideally a different vendor)
  than the data backups themselves. Mixing them defeats the
  separation.
- Photos and thumbnails are encrypted with the tenant's DEK using
  AES-256-GCM with per-file random IVs. Files on disk are
  `<iv>||<ciphertext>||<auth_tag>` blobs.
- `serve_upload` / `serve_thumb` decrypt streaming on the fly; the
  cleartext never lands on disk.
- DB columns stay cleartext for now — search depends on names,
  notes, tags. Encrypting them with searchable encryption / blind
  indexes is a future round; v1 ships with photo encryption only.
- Backup zips contain ciphertext + the wrapped DEK. Restore on a
  new deployment requires the KEK; otherwise the backup is
  recoverable only by the original platform.

**Operator decryption path.**

- There is no `/admin` button that decrypts photos. Operators have
  KEK access but no UI that turns it into bulk reading.
- Recovery is a CLI tool (`stash-recover`) the operator runs only
  when a user files a recovery ticket. Every invocation:
  - logs to the global audit log with operator email + tenant +
    reason;
  - emails the affected tenant's maintainers ("operator X decrypted
    files for ticket Y at <ts>") — operators cannot decrypt-and-go-
    quiet.
- "Help debug a critical situation" is exactly this: the user opens
  a ticket, the operator runs the tool, the user sees a record of
  it landing in their inbox.

**Key loss is total loss.** The KEK is the single point of failure;
it gets the same care as a production database password. KEK
rotation is future work — v1 ships with one KEK, backed up
separately from the data, and a documented rotation procedure when
that lands.

**Migration.** Existing photos get re-written through the encrypt
path during the live-user migration. One-time, scripted, with a
checksum verification pass before the cleartext originals are
removed. The migration takes a B2 snapshot first (see
"Pre-migration snapshot" under Backups).

### Sign-up + onboarding

Four resolved sign-in shapes:

1. **Fresh email, no relationship to any tenant.** Land on a
   "create your stash?" screen. Self-serve tenant creation,
   opt-in. The user becomes the sole maintainer of a Personal
   tenant with free-tier defaults. No operator approval needed.
2. **Sign-in following a member-invite link.** Operator (or a
   maintainer of the inviting tenant) pre-named the tenant + bound
   the invite to an expected email.  The token in the URL bypasses
   the global allowlist for *this* sign-in only, completes
   authentication, and adds the user as a member of the inviting
   tenant at the granted role.  Identity-vs-invite rebinding (see
   below) lets the *actual* signed-in email win.  They may also
   self-create their own tenant later from the switcher.
3. **Sign-in following a bootstrap (self-onboarding) link.**
   Operator minted a single-use link with only a plan + role —
   no tenant exists yet.  The recipient signs in via the same
   middleware-bypass path, then *names their own stash* on the
   accept page.  On POST, atomically: race-safe consume of the
   token, create the tenant with the locked-in plan, add the
   recipient as the lone maintainer.  Tier intentionally hidden
   on the accept page so the recipient isn't surprised by
   billing.
4. **Sign-in with an active object share but no member/invite
   relationship.** Same logic as #2 — the share is sufficient
   "you're allowed in" signal; the user can browse the shared
   object and optionally self-create a Personal tenant.

Implication: today's `emails.txt` (oauth2-proxy allowlist) is
replaced by "any email known to stash via membership, invite, or
share is allowed through" — handled by a `current_actor`
pre-resolution that probes those three sources before deciding
whether to 403. Operators retain a separate hard-block list for
abuse cases.

Tenant creation is rate-limited (see "Anti-abuse").

**Identity-vs-invite collision.** Bob is invited at
`bob@example.com`. He signs in via Google as `bob.smith@gmail.com`.
The invite token in the URL is bound to whatever email the *current*
sign-in claims; we don't hold the user on a confirmation screen.
The original inviter sees the bound email in the audit log
("invite redirected from X to Y at <ts>") so surprises are
detectable, and the invited user can pick which Google identity
they want to associate with Stash without an extra UI step.

### Bulk imports

A first-class importer surface lets a user drop a competitor's
export (XLSX / CSV / paired media ZIP) into Stash and walk away
with a working inventory.  Shipped 2026-05-16 with Encircle as the
first parser; the registry shape makes Sortly / HomeBox / MyStuff2
one HEADER_MAP + one parser function each.

**Architecture.**

- ``dao/imports.py`` exposes ``parse(source, filename, bytes) ->
  ParseResult`` with a per-source ``Importer`` registry in
  ``PARSERS``.  Each importer normalises the source export into a
  product-agnostic dict shape (``name``, ``quantity``, ``notes``,
  ``room``, ``tags``, optional embedded photo bytes).
- ``execute_import(actor, items, source)`` is product-agnostic:
  get-or-create a per-import ``Location`` named ``"Imported from
  <Source> (YYYY-MM-DD HH:MM)"``, map each unique ``room`` value
  to a Stash room, land items in a per-room "Loose items" box via
  ``dao_boxes.get_or_create_loose_for_room``.  Items go in
  through the same DAO path the regular ingest queue uses, so
  encryption + quotas + audit log apply unchanged.
- ``undo_import(actor, location_id)`` cascade-deletes the whole
  import — refuses any Location whose name doesn't carry the
  ``IMPORTED_LOCATION_PREFIX`` so a typo'd id can't take out real
  data.  The "undo" UI button posts here; same audit trail as
  every other tenant-side delete.

**Photo extraction (Encircle specifics, lift to other parsers as
needed).**

- **XLSX-embedded images** (Encircle's mobile-app export).
  ``parse_encircle_xlsx`` switches openpyxl out of read-only mode
  so ``ws._images`` is populated.  Each image's cell anchor
  (``_from.row``) cross-references the parsed item via a
  ``_sheet_row`` hint left by ``_normalise_rows`` during parse
  (empty + nameless rows are dropped, so the row→item mapping
  isn't trivial without that hint).
- **Paired media ZIP** (Encircle's web-app "Download Photos &
  Videos" bundle).  ``attach_encircle_media_zip`` walks
  ``Room/Filename.jpg`` entries with an exact → prefix → substring
  fuzzy-match ladder; only fills items that didn't already pick
  up a photo from XLSX.  Receipt + data-tag filename tokens
  skipped — Stash has a single ``photo`` column per item today;
  multi-photo support is V3+.

**Quotas.** Imports count against the tenant's ``storage_bytes``
cap (each extracted photo is just a regular upload through
``save_photo_bytes``).  A free-tier import that would exceed the
100 MB cap stops at the first over-cap photo and surfaces a
"hit your free-tier cap at item N of M — upgrade or drop some
photos" banner; everything imported up to that point stays.

**Funnel.** `/encircle-alternative` is the campaign landing page
for displaced Encircle users (Encircle's consumer Home Inventory
product shut down 2025-12-17).  Page is auth-free + carries a
live "N of M free spots open right now" badge from
`free_tier_capacity()`; CTAs route to `/import` for the upload
or `/signup` for first-time visitors who hit the page without an
account.

### Notifications

Email is the canonical channel:

- **Provider**: Postmark (transactional reliability story matches
  the ethos; SES is the fallback if cost crosses a threshold).
- **Templates**: invite, share-created, share-revoked, share-cascade
  change ("the box you can edit got bigger"), quota-warning
  (80%, 100%), grace-period reminder (T-7, T-1 day),
  backup-failed, backup-restored, tenant-soft-deleted,
  tenant-hard-delete-imminent, operator-decrypted-files
  (mandatory).
- **Per-tenant unsubscribe** is granular by category, not all-off.
  Critical alerts (backup failure, lifecycle, operator decrypt)
  cannot be unsubscribed.
- **Localised** at send time using the recipient member's
  preferred locale (see "Localization").
- **Local dev** writes to a `mail/` directory instead of sending,
  so test fixtures don't depend on a live provider.

Future channels (push notification, webhook) follow the same
queue/template structure.

### Anti-abuse

Free-tier exposure cuts both ways: an attacker mass-creates tenants
to burn AI quota; a single tenant uploads a thousand garbage photos
to hit the 100 MB cap and abandon. Mitigations:

- **Tenant-creation throttle**: max N new tenants from one IP per
  hour; exponential cooldown after sustained signup; max M from a
  given email domain per day.
- **Email-domain blocklist**: opt-in deny list of known
  throwaway-email providers; configurable per deployment.
- **AI surface gated on first-interactive-session**: a freshly
  created tenant cannot hit `/ingest` for the first N minutes /
  until the user has clicked through onboarding. Stops scripts.
- **Inactivity lifecycle**: a free tenant idle for 90 days enters
  soft-delete with the standard grace; B2 backup is retained per
  the standard rules. Stops storage drift from abandoned signups.
- **Per-tenant emergency cost cap**: even with operator override,
  no tenant can exceed `$C / day` in vendor cost without an
  explicit second-confirmation step at the operator surface.

### Concurrent edits

Two maintainers in two tabs editing the same item produce silent
overwrites today. Add optimistic concurrency:

- Every mutable row carries `updated_at` and a `version` integer.
- Mutation routes accept `If-Match: <version>` and respond `409
  Conflict` on mismatch.
- The UI fetches the current version when opening an edit form,
  posts it back, and on 409 surfaces a "this was changed by
  `<maintainer email>` at `<ts>` — refresh and re-apply" prompt.
- Bulk operations from search use a single transaction with all
  involved versions; one mismatch fails the batch atomically.

Light-touch by design: no presence indicators or live-collab; just
"last writer wins" replaced by "winner is whoever submits with the
right version."

### API tokens + CSRF posture

The PWA / mobile capture path on the broader product roadmap will
need non-cookie authentication. Designed once, here:

- Per-tenant scoped tokens. Role = the issuing member's role at
  mint time; revoked if the member's role drops.
- Issued from `/usage` (maintainer-only).
- Stored as argon2id hashes; cleartext shown once at creation.
- `Authorization: Bearer stash_<tenant>_<key>`.
- Lives on a separate router prefix (`/api/v1/...`) so the
  cookie-form path stays simple and CSRF-clean. Mixing the two on
  a single route is rejected at code review.
- Each token has a name, created/last-used timestamps, and a
  revoke button. Audit-log entries on mint and revoke.
- Token traffic counts toward the same per-surface quotas as
  cookie traffic; quota enforcement is auth-mode-agnostic.

**CSRF.** Cookie-authenticated form POSTs stay single-origin
(oauth2-proxy + Caddy gate everything; SameSite=Lax already in
place). The `/api/v1` router is bearer-only and explicitly
disables cookie auth — there's no scenario where a token call
could be triggered cross-origin from a stale browser session.

### Agent / MCP integration

Agents (Claude Desktop, Claude Code, custom agents via the
Anthropic SDK) get first-class access to the stash via the
**Model Context Protocol** — Anthropic's open spec for
tool-using agents.  The goal is "interact with the stash
natively, like the UI": ask an agent where something is,
have it show you a picture, have it move things between
boxes when you're packing.

#### Goals

- Agents can browse + search the stash without screen-scraping.
- Agents can mutate (move, create, edit) when explicitly asked,
  with the same audit trail any human action carries.
- Agents can fetch photo bytes (thumbs or full-res) so an agent
  reply can include "here's what your blue mug looks like"
  rather than just text.
- Tenant isolation + role gates from the REST surface carry
  through unchanged — an MCP session is just a bearer token
  with extra-rich error semantics.
- Operators can kill an agent's access instantly via
  `/admin`'s suspend/revoke surface (already shipped with phase
  11 token hardening).

#### Non-goals (this round)

- Sidecar MCP server.  Built-in HTTP MCP is the primary
  deliverable; a stdio sidecar is dropped, not deferred.  If a
  client only speaks stdio, run it through the Anthropic
  ``mcp-proxy`` (HTTP→stdio bridge) instead of carrying a
  parallel surface in this codebase.
- Multi-agent coordination, agent-to-agent messaging.
- Streaming long tool outputs incrementally.  All tool calls
  return atomic results.

#### Transport

The endpoint is a **single HTTP route at `/mcp`** speaking the
MCP Streamable HTTP transport, spec rev **2025-11-25**
([modelcontextprotocol.io/specification/2025-11-25/basic/transports][mcp-transport]):

[mcp-transport]: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports

- ``POST /mcp`` — body is a single JSON-RPC request,
  notification, or response.  Server picks the response shape
  per call:
  * For requests, ``Content-Type: application/json`` (a single
    response object) or ``text/event-stream`` (SSE stream when
    the tool wants to push intermediate messages).  Stash's
    tools are atomic so most calls return JSON; SSE is the
    optional path.
  * For notifications + responses, ``HTTP 202 Accepted`` with
    no body.
- ``GET /mcp`` — opens an SSE stream for server-initiated
  messages without a preceding client request.  Stash has no
  server-push use cases in v1, so this returns ``405 Method
  Not Allowed``.
- ``DELETE /mcp`` — client-initiated session termination.
  Stash deliberately opts out of MCP's session-id surface
  (rationale below); ``DELETE`` returns ``405``.
- Required headers on every non-initialize request:
  * ``Authorization: Bearer stash_<...>`` — same surface as
    ``/api/v1``.  Phase 11's auto-revoke guards (HTTPS gate,
    URL/header leak scanner) cover this endpoint without
    modification.
  * ``MCP-Protocol-Version: 2025-11-25``.  The 2025-11-25 spec
    says a server *may* fall back to ``2025-03-26`` when the
    header is absent; stash chooses *not* to — missing or
    different versions get ``400 Bad Request`` so a stale
    client breaks loudly rather than silently missing new
    tool semantics.
  * ``Accept: application/json, text/event-stream`` — required
    by the spec on every POST so the server can pick the
    response shape per call.  Stash returns ``400`` if either
    is missing.
- ``Origin`` header validation (spec §"Security Warning"):
  stash MUST ``403`` a request whose ``Origin`` is set and
  not in the deployment's allow-list.  This is the
  DNS-rebinding mitigation — without it, a malicious page can
  drive a localhost MCP from the user's browser.  Allow-list
  is ``STASH_PUBLIC_URL`` by default plus a comma-separated
  ``STASH_MCP_ALLOWED_ORIGINS`` for dev clients (Claude
  Desktop loopback, IDE hosts, etc.).
- Auth shape:
  * Missing/invalid/revoked/suspended bearer ⇒ ``401`` with a
  JSON-RPC ``error`` body (``code = -32001``, "auth required").
  The connection ends; the client is expected to re-auth.
  * Tool-level failures (404/400/409/429) ride inside
  successful ``200 OK`` responses with ``isError: true``,
  per the error mapping below.  This keeps a single
  hallucinated id from killing the agent's session.
- Session lifecycle: stash deliberately opts out of MCP's
  ``MCP-Session-Id`` surface.  We do not return one from
  ``initialize``, do not require one on subsequent requests,
  do not implement resumability via ``Last-Event-ID``.  The
  bearer is the session.  Rationale: every tool call is
  short, atomic, and stateless on the server side; per-session
  state would re-introduce a per-connection footprint we
  don't otherwise carry, and we'd inherit the spec's
  hijacking-mitigation overhead for no current benefit.
  When we ship long-running tools (deferred), we revisit.

#### Auth + multi-tenant

- One bearer = one tenant, full stop.  Tokens minted on `/usage`
  carry the issuing tenant + role; the MCP session inherits.
- The same auto-revoke guards from phase 11 fire here: bearer
  over plain HTTP ⇒ revoked + 401, `stash_<...>` signature in
  the URL or non-Authorization header ⇒ revoked.
- An operator can suspend any tenant's MCP session by suspending
  its token at `/admin` — the next tool call fails 401, the
  agent reconnects (and 401s again), the human sees the failure.
- Future: scoped tokens (read-only, ai-only) ride the existing
  ``api_tokens.scopes`` JSON column.  An MCP server can opt
  into a narrower scope at mint time.

#### Tool catalogue (v1)

Read tools — side-effect-free, idempotent, safe to call freely:

| Tool | Args | Returns |
|---|---|---|
| `me` | — | `{tenant_id, role, plan}` |
| `find_items` | `q: str = ""`, `box_id: int? `, `tag: str = ""`, `limit: int = 50`, `offset: int = 0` | List of items with box name + photo URL inline |
| `get_item` | `item_id: int`, `include_photo: "none"\|"thumb"\|"full" = "none"` | Item dict with optional ``ImageContent`` |
| `list_boxes` | `room_id: int?`, `location_id: int?` | List of boxes with item counts |
| `get_box` | `box_id: int`, `include_items: bool = True` | Box dict + item summary |
| `list_locations` | — | Locations with room/box counts |
| `list_rooms` | `location_id: int?` | Rooms |
| `list_tags` | — | Tag names + per-tag use counts |
| `inventory_room` | `room_id: int` | Composite: every box + items in the room |

Write tools — one-shot, fail loudly on bad targets:

| Tool | Args | Returns |
|---|---|---|
| `move_item` | `item_id: int`, `target_box_id: int` | `{old_box_id, new_box_id}`, MCP error on bad ids |
| `create_item` | `box_id: int`, `name: str`, `notes: str = ""`, `tags: list[str] = []` | New item dict |
| `update_item` | `item_id: int`, `name: str?`, `notes: str?` | Updated item dict |
| `add_tag` / `remove_tag` | `item_id: int`, `tag: str` | Updated tag list |
| `mark_missing` | `item_id: int` | `{is_missing: bool}` |

Operator-scoped tools — gated on the bearer's operator flag, 404
when called by a tenant-scoped token:

| Tool | Args | Returns |
|---|---|---|
| `admin_list_feedback` | `status?`, `urgent?`, `source?`, `limit`, `offset` | Feedback rows with `has_screenshot` / `has_page_html` flags for cheap triage |
| `admin_get_feedback` | `id`, `include?: list[str]` | Single feedback row; `include` opts in to `screenshot` (base64 data URL), `page_html` (text, 256 KB cap + truncated flag), `console_log` (parsed JSON), `perf_timing` (parsed JSON), or `all` |
| `admin_set_feedback_status` | `id`, `status` | Updated row; audit-logs the transition |
| `admin_set_feedback_urgent` | `id`, `urgent: bool` | Updated row; flips the major-blocker flag that floats it to the top of the queue |
| `admin_create_feedback` | `body`, `tenant_id?`, `source?='mcp'`, `urgent?`, … | Inserts a feedback row tagged with `source` (defaults to `'mcp'`).  Used by the visual-sweep rig to drop layout findings into the same queue as user-submitted bugs |
| `admin_feedback_counts` | — | `{open, accepted, rejected, done, urgent}` for the dashboard tiles |

Each write tool walks the same DAO methods the REST API uses,
so the audit log and quota enforcement are uniform across UI /
API / MCP traffic.

Deferred for v2:

- `create_box`, `delete_item`, `delete_box` — destructive
  enough to warrant explicit human confirmation in the UI for
  now.  When agents prove themselves we lift these in.
- `generate_box_art` — direct AI invocation through MCP would
  loop AI through AI; better to keep this on the human surface.
- `recrop_item` / `replace_photo` — agents shouldn't be
  rewriting source-of-truth photo data without the user
  watching.

#### Resource catalogue (v1)

MCP "resources" are read-only addressable surfaces an agent
discovers via `resources/list` and reads via `resources/read`.
Stash exposes:

- `stash://items/{id}` — item JSON.  Equivalent to `get_item`
  without the photo.
- `stash://boxes/{id}` — box + items JSON.
- `stash://rooms/{id}` — room + boxes JSON.
- `stash://locations/{id}` — location + floors + rooms JSON.

Resources are useful when an agent wants to pull stable URIs
into its prompt context without invoking a tool every time.
The data is identical to the read-tool output; the URI form
is what enables agents like Claude Desktop to "remember" a
specific box across turns.

#### Photo content

`get_item(item_id, include_photo=...)` returns:

- `"none"` (default): item JSON + a `photo_url` string the
  agent can render in a UI link.  No image bytes, minimal
  bandwidth.
- `"thumb"`: 320 px JPEG returned as MCP `ImageContent` (base64
  + `mime: "image/jpeg"`).  Agents that "look at" items in
  bulk should default to this.
- `"full"`: full-resolution JPEG.  Costs the agent's full
  context window per item; gated by quota the same as any
  upload-bytes consumer (each `full` fetch counts toward the
  daily AI cost cap to keep agents from hammering the source
  files).

The photo bytes path goes through the same encryption-at-rest
+ tenant-scope checks as `/uploads/{name}` — no new file
serving surface, just a different output encoding.

#### Quota + soft warnings

- `X-Quota-Warning` from the REST layer surfaces to the agent
  as a non-fatal MCP warning attached to the next tool result.
  Format: `{"warnings": ["monthly_ai_calls=85%", ...]}` in the
  `_meta` field of the JSON-RPC response.
- 429 from a write tool surfaces as an MCP error with
  `data: {retry_after: <seconds>, reset_at: <iso>}` so a
  well-behaved agent can back off.
- The daily AI cost cap is the runaway-MCP guard from phase
  10; an agent that triggers it sees 429s on every AI-flavoured
  tool until UTC midnight or until an operator raises the cap.

#### Error mapping

REST layer → MCP error contract:

| REST | MCP behaviour |
|---|---|
| 200 | Tool result, `isError: false` |
| 400 | Tool result, `isError: true`, message reflects the validation problem |
| 401 (token revoked / suspended / over HTTP) | JSON-RPC error `code = -32001`, kill the connection |
| 403 (role insufficient) | Tool result, `isError: true`, "Token role lacks permission" |
| 404 (item / box not found in actor's tenant) | Tool result, `isError: true`, "Not found" — never 403, no leak about other tenants |
| 409 (optimistic concurrency conflict) | Tool result, `isError: true`, "Stale read, refresh and retry" |
| 429 (quota exceeded) | Tool result, `isError: true`, `data: {retry_after, reset_at}` |
| 5xx | JSON-RPC error `code = -32603`, transient |

#### Logging + telemetry

- Every MCP request is a regular HTTP request to `/mcp`, so the
  per-request log line + audit-log integration from phase 16
  apply unchanged.  Agent traffic shows up with
  `actor_email = api_token:<id>` and the bearer's tenant_id in
  the structured fields.
- New telemetry distinction: `surface = "mcp"` on every
  `usage_events` row generated by an MCP tool call, replacing
  the default `"core"` for non-AI tools.  Enables the
  cost-transparency block to break out agent-vs-human usage in
  phase 13.
- Per-tool counters (`mcp.find_items.calls` /
  `mcp.move_item.calls` etc.) land as ``kind`` values so an
  operator audit can see "your agent did 5,000 searches and
  3 moves" at a glance.

#### Deployment

- Single endpoint, no sidecar.  ``/mcp`` ships in the same
  container as the rest of stash.  Caddy proxies through with
  the standard TLS/headers flow.
- No new env vars; the bearer mechanism is already configured.
  Per-deployment MCP can be disabled by setting
  ``STASH_MCP_ENABLED=false`` (default true) — useful for
  early-stage deploys that want to lock down the agent surface
  while sorting out the prompt + tool semantics.
- Claude Desktop / Code config example:

  ```json
  {
    "mcpServers": {
      "stash": {
        "url": "https://stash.example.com/mcp",
        "headers": {
          "Authorization": "Bearer stash_..."
        }
      }
    }
  }
  ```

#### Versioning

- Tool schemas declared in code with explicit JSON Schema; any
  breaking change bumps the tool name (``find_items_v2``) +
  keeps the old form for two minor versions.
- Resource URIs are stable forever; the JSON shape they return
  follows the same versioning rule.
- The MCP protocol version is pinned to ``2025-11-25``.
  Bumping it requires a code change (header allow-list +
  whatever transport semantics shift between revs) + a release
  note.  The previous rev (``2025-03-26``) is **not** accepted
  by stash; even though the spec lets servers fall back, we
  hard-fail to keep tool semantics straight.

#### Open questions

- **Image bandwidth caps.**  Should `include_photo: "full"`
  count as a single AI call (priced at $cost_per_image) or as
  upload-bytes (priced per MB)?  The cleaner model is
  upload-bytes since the bytes are stash's own; lean toward
  that.  Resolved when phase 13 ships.
- **Tool gating per token scope.**  Today every token can call
  every tool.  When we ship scoped tokens, do we want
  per-tool scopes (`mcp.find_items` vs `mcp.move_item`) or
  bucket-level (`read` vs `write`)?  Bucket-level is cheaper;
  start there.
- **Embedded confirmations.**  Spec'd for no-confirm one-shot
  writes per the explicit user direction; revisit if agents
  start making expensive mistakes that audit-log can't undo.

### SQLite concurrency

With multi-tenant write fan-out (a single ingest commits to
`pending_items` + `tags` + thumbs in quick succession), `database
is locked` becomes a real failure mode. Configure at every
connection:

- `PRAGMA journal_mode = WAL` (persists once enabled).
- `PRAGMA busy_timeout = 5000` so contended writes retry rather
  than fail immediately.
- `PRAGMA synchronous = NORMAL` paired with WAL for the
  throughput bump.

Applied in the `db()` helper. A future move to Postgres is
sketched but explicitly not v1.

### Roles

| Capability | Maintainer | Readonly | Share recipient (maintainer) | Share recipient (readonly) |
|---|---|---|---|---|
| Browse / search tenant content | ✓ | ✓ | only the shared object subtree | only the shared object subtree |
| Create/edit/delete boxes and items | ✓ | ✗ | ✓ on shared object | ✗ |
| Ingest photos | ✓ | ✗ | ✗ | ✗ |
| Generate AI suggestions / label art | ✓ | ✗ | ✗ | ✗ |
| Invite tenant members | ✓ | ✗ | ✗ | ✗ |
| Create object shares | ✓ | ✗ | ✗ | ✗ |
| Trigger backup / restore | ✓ | ✗ | ✗ | ✗ |
| View usage / billing | ✓ | partial (no $) | n/a | n/a |

Role enforcement lives on every DAO mutation method as a guard
clause. Routes also pre-check, so the 403 lands without a DB round
trip when possible.

### Sharing model

Two layers, both implemented:

**Tenant invites** add a member to a tenant. The whole stash is
visible to that member at the granted role. Implemented via
`tenant_invites` (single-use tokens) + `tenant_members`.

**Object shares** grant a specific email access to one box (and
everything inside it) or one item. The recipient need not be a member
of the granting tenant. Implemented via `object_shares`.

Recipient experience:
- A signed-in user sees their own tenant(s) by default.
- Shares land in a separate **"Shared with you"** view that lists each
  shared box/item with the granting tenant's name + the role the
  granter assigned. Clicking through opens that single object's
  detail surface — not the wider tenant.
- Box shares cascade: editing the box edits its items (within the
  granted role). Item shares are scoped to that item only.
- Edits attribute to the recipient's email in the granting tenant's
  audit log.

Revocation: any maintainer of the granting tenant can revoke a share.
Revocation is immediate — sets `revoked_at`, the next request from
the recipient 403s.

**Edge cases:**

- **Items added to a shared box later** — visible to the recipient
  at the same role as the box share. The grant is on the *box*; new
  items inherit by virtue of being children. Audit-log a
  "share-cascade" line so the granter sees the grant widening.
- **Item moved to another box** — the per-item share follows the
  item; per-box shares scope by box, so an item moving out of a
  shared box loses that share. The recipient sees a one-time
  "this item moved out of {Box} and is no longer shared with you"
  notification.
- **Recipient is also a tenant member** — dedupe at access-resolution
  time. Effective role = `max(membership_role, share_role)`. The
  Shared-with-you view hides shares the user already has tenant-level
  access to (no point cluttering it).
- **Granting tenant soft-deleted** — recipient access pauses for the
  grace period: the share row stays alive but the access check
  returns 403 with a "this stash is suspended" message. On
  reactivation the share resumes; on hard-delete it's revoked.
- **Member who created the share leaves the tenant** — the share
  survives. Shares are tenant-owned, not member-owned. The audit
  trail keeps the original `created_by_email` for forensics.
- **Recipient email re-targeting** (someone deletes the gmail
  account, recreates it) — out of scope; we trust whatever email
  oauth2-proxy validates at sign-in time. Same threat model as
  every other email-keyed system.

### Backups

Per-tenant, replacing the global zip:

- `/maintenance/export` → operator-only on `/admin` (ops-wide bundle).
- New per-tenant `/usage/backup` → zip of just *this* tenant's DB
  rows (filtered) + uploads under `{tenant_id}/`.
- Per-tenant SQLite export uses `iterdump()` filtered to that
  tenant's rows, OR a fresh empty DB seeded by SELECT-and-INSERT —
  whichever is faster to validate; bias toward the seeded approach
  because it preserves schema exactly and dodges integrity quirks.
- Restore is a one-way replace **for that tenant only**. The
  operator path keeps the existing whole-DB restore as a DR hatch.

**Off-site DR via Backblaze B2:**
- Nightly job uploads each active tenant's backup zip to B2 with
  `s3://stash-backups/{tenant_id}/{YYYY-MM-DD}.zip` keying.
- Configurable retention (e.g. 30 daily, 12 monthly).
- B2 credentials live in env (`B2_KEY_ID`, `B2_APPLICATION_KEY`,
  `B2_BUCKET`, `B2_ENDPOINT`). Use the S3-compatible API via boto3 to
  avoid a bespoke SDK.
- Backup bytes count toward the tenant's `backup_storage_bytes` quota
  (so we don't accidentally pay for a hoarder).
- **The KEK travels in a different bucket** (and ideally a different
  vendor) than the data. Co-locating them defeats the whole
  encryption-at-rest separation; the spec is explicit about this so
  no future contributor "tidies up" by merging them.

**Verifiability.** A weekly verification job picks a random tenant,
restores yesterday's B2 backup into a scratch DB + scratch upload
dir using a scratch-DEK derived from the same KEK, asserts row
counts vs the audit log, and confirms every photo reference resolves
to an extractable + decrypt-able file. Failure emails operators and
writes to the global audit log. Without this step, "we have backups"
is a story rather than a guarantee — most teams learn that
distinction on a bad day.

**Pre-migration snapshot.** Every schema migration takes a B2 zip of
the live DB before the first DDL runs. Keyed by git SHA + timestamp
under `s3://stash-backups/_migrations/<sha>-<ts>.zip`, retained for
90 days. The rollback story is then explicit: revert the binary,
restore the snapshot, done. Same job runs on the live-user
migration.

### Telemetry & quotas

**Counters:** every AI call, every successful upload, every backup
write logs to `usage_events` keyed by tenant. Cost is approximated
from a hard-coded price table (Gemini Flash, Gemini 3 Pro Image,
Anthropic Opus, B2 storage) — refined later when there's real billing.

**API surfaces** group endpoints into independently-cap-able buckets.
This maps to FastAPI router prefixes:

- `ai`: `/ingest`, label-art generation, suggest_box.
- `upload`: any route that calls `save_photo_bytes` for a
  user-supplied photo (replace photo, floorplan upload, etc.).
- `core`: everything else — browsing, search, edit metadata, generate
  labels (no AI), maintenance UI.

**Soft caps:**
- `<80%` usage → no warning.
- `80–99%` → banner on the usage page; `X-Quota-Warning` response
  header for any AI/upload call.
- `≥100%` → the offending surface 429s on new requests; in-flight
  ones complete. `core` is never gated by AI/upload caps so the user
  can still browse, edit metadata, and pay/upgrade when that ships.

The race between counter writes and cap checks is acknowledged and
accepted — we'd rather over-serve by a few requests than block a
write that already started.

### Operator surface (`/admin`)

A separate router prefix gated by an operator-only middleware. Reuses
the `enforce_email_allowlist`-style pattern:

- `STASH_OPERATOR_EMAILS=alice@example.com,bob@example.com` env var.
- `current_actor` middleware tags the actor `is_operator=True` if
  email is in that list.
- `/admin/*` routes require `is_operator`; everything else ignores
  the flag.

**Hard rule: operators cannot read tenant data through `/admin`.**
The dashboard exposes counts, usage, billing, lifecycle state, and
audit-log entries — not boxes, items, photos, or names. Any
operator who genuinely needs to see a tenant's data goes through the
same invite path a partner would: ask the tenant to send them a
maintainer invite. This trades operator convenience for a story we
can stand behind ("we cannot snoop your stash; we don't even have a
button for it").

Surfaces:
- All-tenant list with metadata + usage rollups (no content).
- Per-tenant lifecycle: soft-delete, reactivate, force hard-delete
  (GDPR right-to-erasure), trigger an off-cycle backup.
- Per-tenant restore from B2 — operator confirms then a tenant
  maintainer drives the actual restore from `/usage` after sign-in.
- Operator-only deployment controls at
  `/admin/maintenance/{update,cleanup,export,import}`: watchtower-
  driven container update, orphan cleanup, whole-platform backup
  export, whole-platform DB import.  All four 404 for non-operators
  to keep the surface opaque.
- Operator-only DR: full-DB import (at
  `/admin/maintenance/import`) for the catastrophic case where the
  whole deployment needs reseeding. Logs prominently to the global
  audit log.
- Quota override editor.
- Vendor cost panel — what the platform paid each vendor last
  cycle, total revenue, gross margin. The numbers shown here are the
  source of truth for the in-app price-transparency block.
- Onboarding-link minter — single-use plan-bearing magic link
  for recipient-self-naming (see "Sign-up + onboarding" shape #3).
- Free-tier capacity card — used / available / total slots for
  the free pool + a "bump the pool" form against
  ``deployment_settings.free_tier_bytes_total``.  Slot count is
  pool ÷ per-tenant cap; bump takes effect on next read (no
  restart).  Public landing's "N of M free spots open right now"
  badge reads from the same source.
- In-app feedback triage kanban with captured-DOM + screenshot
  follow-up routes + urgent-flag toggle + source-tagged rows
  (``user_widget`` vs ``mcp``).  Same opacity rule: non-operators
  404 on the whole surface.

### User-facing usage page (replaces `/maintenance` for tenants)

- Plan + role at the top.
- Three meters: storage, AI calls, backup retention — each with this
  cycle's used / cap, plus a sparkline of the last 30 days.
- **Cost transparency block — the five-line breakdown.** Reflects
  the public ethos verbatim:

  | Line | What it is |
  |---|---|
  | Direct vendor passthrough | This tenant's actual usage of Gemini Flash / Gemini 3 Pro Image / Anthropic Opus / B2 storage / host share. Per-vendor sub-rows with unit price + units used. |
  | Community backups | This tenant's slice of B2 retention costs for free-tier backups + post-grace archived backups for users we've already let go. |
  | Community free-tier | This tenant's slice of subsidising free users. Small free tier, small subsidy; the line is explicit so the size is honest. |
  | Operator payout | What goes to the humans running the platform — devs, ops, security, support. Itemised at the aggregate `/about/pricing` view, summed here. |
  | Margin | Reinvestment / runway. Goal is "in the black, but small." |

  These five always sum to the cycle's bill. Free-tier tenants see
  the same table with $0 in the bill column; the line items still
  display so the cost-of-service is visible (and so a user
  considering Pro can see what they would actually be paying for).

  Operator-side, `/admin` shows the aggregate version; the per-tenant
  numbers feed the public `/about/pricing` page directly so the
  public claim and the operator view can never disagree.
- Backups list with download links. Per-tenant restore.
- **Data export (GDPR Article 20).** A "Download my data" button
  that produces a JSON+ZIP bundle of every row this tenant owns
  (boxes, items, tags, audit log) plus the photos in cleartext —
  separate from the encrypted-blob backup zip, which is a
  recovery format, not a portability format.
- Members + invites table.
- Outbound shares (boxes/items I've shared) with revoke.
- Locale + notification-category preferences (see "Localization"
  and "Notifications").
- **Delete tenant** action with the soft-delete grace period clearly
  explained ("you have 30 days to reactivate; one backup will be
  retained beyond that for one year"). Maintainer-only.

The only "destructive" actions exposed here are per-tenant restore
and tenant soft-delete; the operator surface handles cross-tenant DR
and post-grace hard-delete.

### GDPR + privacy posture

Stash holds personal data (photos, item descriptions, possibly home
address via EXIF GPS — stripped on save, but still). The platform
needs to be defensible under GDPR even when run small.

**Lawful basis.** Contract (terms of service for the user) +
legitimate interest (security logging). Marketing emails are *not*
sent — the platform doesn't have any.

**Data subject rights.**

- **Access** (Art. 15) → "Download my data" on the usage page (see
  above). Returns JSON metadata + cleartext photos.
- **Rectification** (Art. 16) → covered by maintainer role.
- **Erasure** (Art. 17, "right to be forgotten") → soft-delete then
  hard-delete via cron, or operator force-hard-delete on user
  request. Archived B2 backup is purged on the same trigger; the
  one-year retention is explicit at delete time and the user
  consents or chooses immediate purge.
- **Restriction** (Art. 18) → soft-delete with read-only browsing
  is the available equivalent.
- **Portability** (Art. 20) → same JSON+ZIP bundle as Access,
  expressly machine-readable.
- **Object** (Art. 21) → no marketing or profiling, so this is
  effectively the same as Erasure for Stash.

**Sub-processors.** Public list at `/about/sub-processors`: Google
(Gemini), Anthropic, Backblaze (B2), Postmark (email), the host
provider. The page is updated whenever the list changes.

We don't make time-based notification commitments here. We do
best-effort email when the list changes, we vet where data goes
before adding a sub-processor, and the durable promise is the
"Download my data" bundle plus the self-host story: a user who
isn't comfortable with a particular sub-processor can leave at any
time with their data intact. The portability of the data is the
real guarantee, not the calendar.

**Breach disclosure.** Where GDPR applies, we comply with the
Article 33 / 34 floor (notification to the supervisory authority
within 72 hours, to affected users without undue delay). The
mechanical capability — pulling the affected-tenant list from audit
logs and templating the email — exists by the time the lifecycle
work ships, so the regulatory floor is a "we've already built it"
rather than a paper commitment.

**Data residency.** Default deployment is in the platform's home
region; B2 supports EU buckets so an EU-residency tier is
*possible* but not v1. Acknowledged in `/about/privacy`.

**No tracking, no analytics.** No third-party JS, no pixel
tracking, no first-party analytics that aren't already covered by
the audit log. The `/about/privacy` page says so plainly.

**Cookie surface.** Only the oauth2-proxy session cookie + the
`tenant_id` selector cookie. Both are functional, not consent-
required under PECR / ePrivacy. The page says so.

**DPA.** A standard data processing addendum lives at `/about/dpa`
for users who need one (B2B / EU compliance). The doc references
the sub-processor list directly.

### Localization

UI is English-only at v1 of multi-tenancy, but the seams for
localization land in the *first* refactor so they don't have to be
retrofitted.

**Approach.** Stdlib `gettext` with a simple wrapper. Strings are
extracted with `xgettext` / `babel`, translation memory lives in
`locale/<lang>/LC_MESSAGES/messages.po`. Jinja templates use a
`{% trans %}` block helper.

**Scope.**

- Web UI strings.
- Email templates (per-recipient locale at send time).
- Notification copy.
- Date / time / number formatting via `babel.dates` and
  `babel.numbers`. Currency (`$` vs `€` etc.) is per-tenant in the
  cost-transparency block.
- Pluralization: gettext plural-forms.
- RTL support: a `dir="rtl"` toggle on `<html>` driven by locale;
  CSS uses logical properties (`margin-inline-start` etc.) so
  RTL flips for free.

**Locale resolution order**: per-member preference (set in `/usage`)
→ `Accept-Language` header → fallback to deployment default (`en`).

**Translation pipeline.** Crowdin or POEditor when there's a
non-English target; until then the `messages.pot` template lives in
the repo and the workflow is "PR a new locale + .po file." No
machine translation by default — the ethos doesn't want users
reading auto-translated UI for product copy.

**v1 ships English-only**, but with `i18n.gettext()` wrapping every
user-facing string, dates rendered through `babel`, and the locale
preference column on `tenant_members`. Adding the second language
is a translation task, not an engineering task — that's the
guarantee.

### Tenant lifecycle

- **Active**: normal use.
- **Soft-deleted**: `tenants.deleted_at IS NOT NULL`. Sign-in lands
  on a "this stash is scheduled for deletion on
  YYYY-MM-DD — reactivate?" screen. Read-only browsing allowed during
  grace; mutations and AI calls disabled.
- **Reactivated**: clearing `deleted_at` returns the tenant to
  active. Members, shares, and quotas resume.
- **Hard-deleted**: after the grace period (default 30 days, or
  immediately on operator override for GDPR), DB rows + uploads are
  removed. The most recent B2 backup is retained for one year
  beyond hard-delete in a separate `s3://stash-backups/_archived/`
  prefix; only operators can restore from there.

Schema:

```text
tenants(
    ...
    deleted_at TEXT,                 -- soft-delete timestamp
    hard_delete_after TEXT,          -- when the cron may purge
    archived_backup_key TEXT,        -- B2 key of the retained zip (post-hard-delete)
    archived_backup_until TEXT,      -- when that zip will be purged
    -- Free-tier inactivity archive (distinct from the soft-delete grace
    -- + post-grace archived backup above).  A free tenant that hasn't
    -- shown audit-log activity in 180 days gets zipped + uploaded to B2
    -- cold storage and its EBS slot freed; signing back in triggers an
    -- operator-approved restore.  Pro tenants are never archived.
    archived_at TEXT,                -- non-NULL = currently archived
    archive_b2_key TEXT              -- B2 object key for the cold bundle
)
```

**Free-tier inactivity archive vs. soft-delete archive.** Two superficially
similar archive surfaces, deliberately separate:

| | Soft-delete archive (`archived_backup_*`) | Inactivity archive (`archived_at` / `archive_b2_key`) |
|---|---|---|
| Trigger | User clicked "delete tenant"; grace expired | Free tenant has 180 days of no audit-log activity |
| Eligible | Any tenant | Free-plan tenants only — Pro is never archived for inactivity |
| State during | Tenant row already gone (hard-deleted) | Tenant row still present, flagged archived; sign-in triggers restore |
| Storage | B2 `_archived/` prefix, 1-year retention | B2 cold-storage bundle, separate operator budget |
| User reachable? | No — they explicitly deleted it | Yes — sign back in and your data comes back |

A scheduled job promotes soft-deleted tenants past their
`hard_delete_after` to hard-delete. A second job purges archived
backups past their retention.

### Logging & observability

Every log line carries enough context that a single grep tells the
story of a request:

- `request_id`: short uuid stamped by the request middleware.
- `actor_email` and `tenant_id` from `current_actor` (both nullable
  for unauthenticated paths and operator cross-tenant work).
- `surface`: which API surface group ran (`ai`, `upload`,
  `core`, `admin`).
- `layer`: which architectural layer emitted the log
  (`route`, `dao`, `vision`, `backup`, `quota`).
- `action`: short verb-noun string for the operation
  (`box.update`, `share.create`, `quota.exceeded`).

Implementation:

- Stdlib `logging` + a `LoggerAdapter` per layer that pulls these
  fields out of `contextvars` set by middleware.
- Structured (JSON) lines in production, pretty in dev.
- Audit-worthy events ALSO write to `audit_log` so users can see
  their own tenant's history. Privacy-sensitive events (operator
  cross-tenant inspection) write to the global audit log only —
  there is no operator-on-tenant action that can read tenant data,
  by design, so this stays a thin surface (e.g.
  `tenant.lifecycle.soft_delete` from operator).
- Sentry / external aggregator is deferred to the support-surface
  work; the structured stdlib output already gives the shape we'd
  ship to it.

## Roadmap

In order. Each step ends in a testable, deployable state.

**Status (2026-05-16, end of day).** Phases 1–4 + 6 + 9 + 10 + 11 +
12 + 15 + 16 + 18 + 19 shipped; phase 5 now [shipped] end-to-end
with self-serve free-tier signup landing on 2026-05-16 (the last
piece — fresh-email path was the only outstanding shape).  Phase
13 promotes to [partial] (cost-transparency block rebuilt on
/about/transparency with the math actually closing across three
tables — per-Pro variable, fixed AWS, pool-clearing arc).  Phase
14 promotes to [partial] (180-day inactivity archive policy +
schema landed; the sweep + B2 round-trip + recovery UI still
deferred).  Phases 7, 8 still partial as before.  In-app feedback
widget shipped out-of-phase as the support-loop replacement and
keeps growing (urgent flag + MCP create tool + source-tagged rows
this round).  Pre-MCP security audit drove a pass that closes the
audit's P0/P1 list: per-share file allow-list, healthz bypass,
bearer auto-revoke on HTTP/URL-leak, operator suspend/resume,
SameSite=lax (oauth2-proxy needs Lax-not-Strict to survive
Google's cross-site OAuth callback; Lax still blocks cross-origin
form POST on modern browsers), app-level security headers, and
the comprehensive auth-coverage test suite (69 cases).  Built-in
MCP endpoint at ``/mcp`` (spec rev 2025-11-25) is live with the
full tool + resource catalogue.  OAuth 2.1 authorization server at
``/oauth/{authorize,token,register}`` plus the RFC 8414 + 9728
discovery surfaces lets claude.ai's web custom-connector dialog
talk to stash without per-user JSON config.  **New this round**:
the displaced-Encircle-user funnel (landing page → bulk importer
with photo extraction → self-serve signup → cost-transparent
free tier with capacity-tunable pool), plus a Playwright UI
regression suite so the next CSS-specificity bug fails CI in a
real browser instead of surviving three rounds of patch attempts.

**Recent polish ships (2026-05-09 → 2026-05-12).** Day-to-day
workflow + reliability work, mostly tightening surfaces that had
already shipped:

* **Ingest hardening.** Packing-session box picker on /ingest
  (form-only state, no session table — leave the page or reload,
  session ends) pre-fills the sort queue's box selection via
  ``pending_items.suggested_box_id``.  Detection-scope radio
  (Auto / Single item / Many items) tunes the Gemini prompt so
  a one-item photo stops splitting into a dozen fake items.
  Process-global ``_INGEST_SEMAPHORE`` (default 1, override via
  ``STASH_INGEST_CONCURRENCY``) serialises workers so a multi-
  photo upload doesn't flood a small VM.  Gemini client carries
  a 120 s timeout (``STASH_GEMINI_TIMEOUT_MS``) so hung calls
  fail loudly instead of wedging the job at "processing".
  Boot-time orphan sweep flips any ``processing`` rows to
  ``failed`` so a restart auto-recovers wedged jobs.  Retry is
  failed-only (was: retry-on-processing spawned duplicates that
  re-detected the same items); Dismiss available on
  ``processing`` rows as the genuine escape hatch.
* **Label polish.** Per-box ``label_orientation`` (landscape /
  portrait) persisted; portrait previews render upright (50.8 ×
  101.6 viewBox, no rotation) while the sheet/PDF paths still
  rotate into the physical landscape cell.  Portrait layout
  word-wraps name + notes into multiple lines with ellipsis
  truncation, bumps the box-ID font fraction to 0.18 so the ID
  doubles as a "find from across the room" handle.  Background
  art opacity bumped 0.3 → 0.5 (the AI art was nearly invisible
  at 0.3 on most papers).  "Room colours" toggle on /labels gives
  each label a pastel wash of its room's colour at 18% opacity
  behind the art (per-box ``boxes.color`` overrides the room
  colour); colour values are regex-whitelisted before they hit
  the SVG.  Sort-queue bbox overlay is now JS-positioned from
  the IMG element's ``getBoundingClientRect()`` instead of CSS
  percentages on the inline-block frame, so it sticks to the
  actually-rendered photo when ``max-height`` shrinks the
  rendered image below the frame's intrinsic width.
* **Admin dashboard.** ``dao_tenants.list_all`` returns
  ``last_activity_at`` per tenant (max ``audit_log.created_at``);
  ``list_members`` returns ``last_active_at`` per member.
  Per-tenant member roster disclosure on /admin with the
  last-active dates.  API tokens panel gets a client-side
  filter bar (tenant / state / role / name substring).  New
  read-only ``dao/audit.list_recent_for_operator`` powers a
  Recent activity card so cross-tenant burst patterns
  (e.g. mass ``oauth.token.issue`` against one client) are
  spottable without paging through tenants.  Admin tables now
  sit in horizontal-scroll wrappers + filter bar stacks below
  640 px so the page works on phones.
* **Sassy error pages.** ``StarletteHTTPException`` handler
  renders ``templates/error.html`` parameterised by status code
  (401, 403, 404, 405, 413, 422, 429, 500, 503) — Siberian
  Forest cat or wise tortoise mascot inline-SVG, headline,
  quip, signature.  HTML-vs-JSON split is strict: ``/api/*``,
  ``/mcp/*``, ``Accept: application/json``, and
  ``X-Requested-With: XMLHttpRequest`` keep the
  ``{"detail": "..."}`` JSON contract; everything else gets the
  rendered page.  Custom raise-site detail messages surface in
  a separate ``error-detail`` block under the headline rather
  than replacing the personality.

**Recent polish ships (2026-05-13 → 2026-05-16).** Operator-surface
security pass + in-app feedback overhaul + onboarding rethink:

* **RBAC pass on `/maintenance`.** The user-facing maintenance
  page used to host deployment controls — watchtower-driven
  container update, orphan cleanup, whole-platform backup
  export, and whole-platform DB import — all reachable by any
  signed-in tenant member.  Any tenant could trigger a restart,
  download every tenant's encrypted blobs, walk every tenant's
  filesystem, or wipe the whole platform.  All four moved to
  ``/admin/maintenance/{update,cleanup,export,import}`` behind
  ``_require_operator_route`` (404-opaque to non-operators,
  per the operator-surface opacity rule).  ``/maintenance`` now
  carries only the three cards safe for any tenant member:
  Version, Access (tenant + members read-only), Changelog.
  ``_run_orphan_cleanup`` + ``_produce_full_backup_zip``
  extracted as pure helpers so tests don't need operator creds
  for between-test cleanup.  New ``as_operator`` side-effect
  fixture promotes the test actor on the live module without
  app reload.
* **In-app feedback widget — extended telemetry on opt-in.**
  The floating "tell us what's wrong" button now captures a
  coherent snapshot when the user taps "Capture this page":
  screenshot + ``document.documentElement.outerHTML`` (capped
  512 KB) + console.error/warn ring buffer + window.onerror +
  unhandledrejection trail (50 entries) + focused-element CSS
  selector + scroll position + page title +
  prefers-color-scheme + client clock + Navigation Timing +
  First Contentful Paint + Largest Contentful Paint.
  Always-on passive ``PerformanceObserver`` (with
  ``buffered: true``) catches LCP entries that fired before
  the widget script loaded.  Nothing sent silently — the bare
  body+URL+UA path is preserved for users who don't tap the
  capture button.  Page HTML rides the same tenant-encrypted-
  blob pipeline as the screenshot so unsubmitted form values
  can't sit cleartext on disk.  New operator route
  ``GET /admin/feedback/{id}/page_html`` serves the captured
  DOM with ``Content-Security-Policy: sandbox`` +
  ``X-Content-Type-Options: nosniff`` + attachment disposition
  so the operator's browser can never *execute* the captured
  page.  Admin queue cards surface 📸 + 📄 + ↗ icons for
  follow-up views.  MCP ``admin_get_feedback`` gains an
  ``include`` array (``screenshot`` / ``page_html`` /
  ``console_log`` / ``perf_timing`` / ``all``) — screenshots
  return as base64 data URLs, HTML as text (capped 256 KB with
  a truncation flag), console + perf parsed to structured
  shapes.  ``admin_list_feedback`` rows gain ``has_screenshot``
  + ``has_page_html`` booleans for cheap triage scanning.
* **html2canvas colour-function fix.** Recent Chrome serialises
  a resolved ``color-mix()`` as ``color(srgb …)`` whenever the
  alpha channel is non-1 — especially inside ``box-shadow``.
  html2canvas 1.4.1 dies on that with "Attempting to parse an
  unsupported color function 'color'", and the existing
  copy-computed-to-inline trick just copied the literal
  ``color(...)`` straight into the clone.  Fix: the onclone
  callback routes every computed value through a 1×1 canvas
  round-trip.  The canvas paints with the browser's own colour
  engine and reads back rgba bytes, so ``color()``,
  ``color-mix()``, ``oklch()``, and ``oklab()`` all get
  normalised before h2c sees them.  Nested cases like
  ``linear-gradient(color-mix(...), red)`` work because the
  outer function resolves as a whole.  PROPS list broadened to
  cover text-shadow, text-decoration-color, caret-color,
  accent-color, and column-rule-color.
* **Onboarding-link flow — operator mints, recipient
  self-names.** Replaces the operator-pre-names-everything
  flow.  Old flow: operator typed a tenant name + invitee
  email; server created the tenant immediately; recipient
  redeemed a magic link to join.  New flow: operator mints a
  self-onboarding link with only a plan + role at
  ``POST /admin/onboarding-links``; whoever clicks first signs
  in, names their own stash, and becomes its sole maintainer.
  Single-use enforced atomically at redeem time via the
  ``consumed_at`` sentinel — two parallel redeems get exactly
  one tenant (loser sees NotFoundError and rolls back the
  orphan).  Per-IP throttle on redeem
  (``check_tenant_creation_rate``) defends against a stolen
  operator credential being scripted into mass tenant
  creation.  New ``tenant_bootstrap_invites`` table — kept
  separate from ``tenant_invites`` so the maintainer-invites-
  members-into-own-tenant flow stays untouched.  Tier hidden
  on the accept page so a free-tier recipient doesn't worry
  about being billed.

**Recent ships (2026-05-16, afternoon → evening tranche).** Two
parallel tracks landed on top of the morning's RBAC / feedback /
onboarding pass:

* **Encircle-refugee funnel — landing → import → signup.**
  Encircle's consumer Home Inventory product shut down 2025-12-17
  leaving a documented cohort searching for a replacement.  Three
  coupled pieces land the funnel:
  * `/encircle-alternative` campaign landing page — auth-free,
    ad-attribution-friendly, anti-SaaS-pricing-defaults copy.
    Mobile-first header/hero/footer refresh on the public surface
    shipped alongside (`style(public)`) so the page reads well
    on phone-width ad traffic.
  * `dao/imports.py` parser registry.  `parse(source, filename,
    bytes) -> ParseResult` with a per-source `Importer` registry
    (`PARSERS` dict); adding Sortly / HomeBox / MyStuff2 later is
    one HEADER_MAP + one parser function each.
    `execute_import(actor, items, source)` is product-agnostic —
    creates a per-import `Location` named `"Imported from <Source>
    (YYYY-MM-DD HH:MM)"`, maps each unique `room` value to a Stash
    room, lands items in a per-room "Loose items" box via the
    existing `dao_boxes.get_or_create_loose_for_room` helper.
    `undo_import` cascade-deletes the whole import but refuses any
    Location whose name doesn't carry the `IMPORTED_LOCATION_PREFIX`
    so a typo'd id can't take out real data.
  * Photo extraction across two paths (XLSX-embedded for the
    mobile-app export + paired media ZIP for the web-app "Download
    Photos & Videos" bundle).  `parse_encircle_xlsx` switches
    openpyxl out of read-only mode so `ws._images` is populated;
    each image's cell anchor cross-references the parsed item via
    a `_sheet_row` hint left during normalisation.
    `attach_encircle_media_zip` walks `Room/Filename.jpg` entries
    with an exact → prefix → substring fuzzy-match ladder and only
    fills items that didn't already pick up a photo from XLSX.
    Receipt + data-tag filename tokens skipped — Stash has one
    `photo` column per item today; multi-photo support is V3+.
  * `/encircle-alternative` CTAs wired to `/import`; the funnel
    actually closes.
* **Self-serve free-tier signup + operator-tunable pool.**
  * New `POST /signup` route lets an authed-but-tenantless user
    create their own free tenant.  Replaces the `no_tenant.html`
    dead end ("ask for an invite") with a real form.
    Middleware-level bypass for `/signup` so the route reaches
    a fresh user with no tenant yet; rest of the app still 303s
    no-tenant browsers to `/signup` so the loop closes from any
    URL they landed on.
  * Reuses `dao_tenants.create_self_serve_tenant` (already shipped
    for the bootstrap-invite flow); per-IP tenant-creation throttle
    applies same as every other tenant-create path.
  * `signup.html` renders three branches — capacity available
    (form), capacity full (Pro upgrade + waitlist email), or POST
    against a freshly-filled pool (409 + "just missed it" banner).
  * **`deployment_settings` table** is the operator-tunables KV
    surface.  `free_tier_bytes_total` defaults to 10 GB; the
    operator bumps it from /admin when they scale their EBS volume.
    Slot count = pool ÷ free per-tenant cap (100 MB) → 102 slots
    at the default.  `dao_quotas.free_tier_capacity()` returns the
    full picture: total bytes, per-slot bytes, total slots, used
    slots, available, is_full.
  * `/admin` gains a "Free-tier capacity" card with used /
    available / total tiles + a "bump the pool" form.  Changes
    take effect on next read — no restart.
  * Public landing (`/`) + `/encircle-alternative` show a live
    "N of M free spots open right now" line under the CTA; copy
    flips to "Free tier full — leave us an email and we'll let you
    know when slots open" when the pool's exhausted.
* **100 MB flat free tier + cost-transparency rebuild.**
  * `quotas.monthly_upload_bytes` renamed → `storage_bytes`
    (idempotent ALTER) with FLAT semantics — "current footprint on
    disk at any time", not "bytes uploaded per calendar month".
    Delete to free space, same model as a phone's photo library.
    `check_or_raise(surface="upload")` reads `storage_footprint`
    (a walk of the encrypted upload dir) instead of summing the
    per-month event ledger.
  * AI calls + AI cost stay windowed (those reset cleanly on
    month / day boundaries); storage doesn't.
  * `/about/transparency` rewritten from a single mixed-unit cost
    ledger into **three tables**: (1) per-Pro variable costs with
    Typical / Heavy columns and an explicit Net-per-Pro → pool row
    ($1.90 typical, $0.08 heavy at $4 Pro pricing); (2) the fixed
    AWS bill (today's ~$33 / mo summed line-by-line + scale-tier
    upper bounds); (3) a pool-clearing arc that derives breakeven
    Pro counts from (1) and (2) (`~18 Pros today`, scaling to
    `~160–530 Pros` at the upper deploy tier).  Replaces the old
    "Remainder ≈ $0.50–$1.00" claim that didn't reconcile against
    the per-Pro deductions above it.  Every number on the page
    follows from the table above it now.
  * `/about/pricing` copy: free = "100 MB total photo storage";
    Pro = "5 GB total photo storage"; flat-cap language consistent
    everywhere, no more "uploads per month".
* **180-day inactivity → B2 cold archive (policy + schema; sweep
  deferred).** Free accounts that go 180 days without audit-log
  activity get zipped + uploaded to B2 cold storage on a separate
  operator budget (so archive growth doesn't cannibalise the
  active-backup line); local EBS slot frees up.  Sign back in →
  operator triggers restore → data comes back exactly as it was.
  Pro accounts are never archived for inactivity.  `tenants.
  archived_at` + `tenants.archive_b2_key` added (see Tenant
  lifecycle schema above); `dao_tenants.list_all` already computes
  `last_activity_at` via subquery against `audit_log.created_at`,
  so the 180-day check just reads that watermark.  /about/pricing
  + /about/transparency + /signup carry the policy up-front so
  newcomers see the trade before committing.  **Out of scope this
  commit**: the sweep job (background or operator-triggered), the
  B2 round-trip, and the recovery UI — all land in phase 14.
* **Free-tier copy revamp.** Household-zone framing replaces the
  closet/college-dorm framing ("the garage, the basement, the
  workshop, the moving-day stack, the kids' bins, the holiday
  decorations").  Grandfather clause spelled out: if the per-tenant
  cap ever drops for new signups (vendor cost moves, paying base
  shrinks), existing free accounts keep their original cap — the
  operator squeezes the new-signup pool, not yours.  Resolution
  lever called out: high-res phone shots are 3–6 MB, drop the
  resolution and the 100 MB stretches a long way (AI surfaces
  prefer higher-res but accuracy degrades gracefully).
* **Feedback queue — urgent flag + agent-source rows.**
  * `feedback.urgent` integer flag (0/1).  Urgent rows sort to
    the top of each kanban column with a 🔥 pill + red left
    border.  Operator flips per row via /admin; MCP exposes the
    same surface as `admin_set_feedback_urgent`.
  * `feedback.source` column (`'user_widget'` default; legacy
    rows backfilled via ADD COLUMN WITH DEFAULT).  New MCP tool
    `admin_create_feedback` inserts rows tagged `source='mcp'`
    so visual-sweep findings land in the same triage queue as
    real-user submissions with a clean "filter automated noise"
    pill.  /admin renders the source as a pill on each card.
* **Visual-sweep dev tool (out-of-tree).** New `tools/sweep/`
  standalone — captures the public surface across an 18-viewport
  device palette (phones → tablets → laptops, sorted ascending
  width), asks Gemini Flash to flag layout bugs, streams results
  into a local annotation gallery in real time.  Group-by-route
  gallery view is default; "Flat" toggle drops back to the
  original auto-fill grid.  No imports from stash — designed to
  graduate to a generic Playwright pattern toolbelt later.  The
  natural pairing with `admin_create_feedback` is "agent walks
  the sweep manifest, one MCP call per layout finding."
* **UI regression net via Playwright.** New `tests/ui/` —
  pytest-playwright suite that drives a real browser against
  uvicorn spawned in a subprocess.  In-process TestClient cannot
  catch CSS / JS bugs; feedback #37/#41/#46 lived entirely in a
  one-line CSS specificity collision and survived three rounds of
  patch attempts because nothing in the tree ever rendered the
  page in a browser to validate.  Verified by reintroducing the
  original CSS bug temporarily: all four UI tests caught it.
  Pytest now defaults to `xdist` parallel mode so the full suite
  (including the browser tests) stays under a sane CI budget.
* **Floorplan + queue polish.**
  * Floorplan: box-preview modal close + watchdog made
    unconditional (so a hung /preview fetch can't leave it
    unkillable), root-cause fix on the unkillable Loading dialog
    (CSS specificity collision masked the close handler from
    landing on the right element), eraser paints over the
    background image instead of refusing the stroke.
  * Sort queue: "just in a room (no box yet)" picker option (lets
    a maintainer accept an item to a room without committing to a
    specific box up-front), crop controls moved under the photo
    + scrollable on mobile, scroll position preserved across
    Accept / Reject reloads.

See per-phase `[shipped]` / `[partial]` markers below.

1. **[shipped]** **Schema + actor middleware + i18n seams + SQLite
   pragmas.** Add the new tables, add `tenant_id` to every owned
   table, backfill all existing rows to `tenant_id = 1` plus a
   "Personal" tenant with the live user as sole maintainer. Replace
   `enforce_email_allowlist` with a `current_actor` middleware that
   resolves `email → tenant + role` (also handling the invite /
   share bypass paths from "Sign-up + onboarding"). Wrap every
   user-facing string in `gettext()` and route dates through
   `babel` from day one — even though v1 ships English-only,
   retrofitting i18n later is the kind of mass-edit nightmare we're
   avoiding. Set WAL + busy_timeout pragmas on every connection.
   Pure additive at the tenancy layer; live user keeps working.

2. **[shipped]** **Encryption at rest.** Per-tenant DEK + envelope
   encryption with a `STASH_KEK` env var. Encrypt photos and thumbs
   on write, decrypt streaming on read. Re-write existing photos
   through the encrypt path during the live-user migration. CLI
   `stash-recover` tool for audited operator decryption.

3. **[shipped]** **DAO module — read paths.** Build `dao/` with one
   module per aggregate (`boxes`, `items`, `rooms`, etc.). Migrate
   every read route to call the DAO.  Tenancy assertions on every
   DAO read method.  Lint enforced as a ratchet
   (`tests/test_dao_ratchet.py`): app.py inline `conn.execute(`
   count is capped, and any *other* module shipping raw SQL fails
   the suite — the small allow-list is `app.py`, `dao/`, `vault.py`,
   `tests/`.

4. **[shipped]** **DAO mutation paths + optimistic concurrency.**
   Writes migrated to DAO, role gates on every mutation method,
   routes pre-check role.  `version` columns on mutable rows;
   `If-Match` on the box-edit form is the first surface to consume
   the contract — stale tokens 409 with `_conflict_http`, missing
   tokens fall back to last-write-wins for backwards compatibility.

5. **[partial]** **Email delivery (Postmark) + invites + sign-up.**
   * **[shipped]** Self-serve free-tier sign-up — the fresh-email
     shape from "Sign-up + onboarding".  `POST /signup` creates the
     tenant via `dao_tenants.create_self_serve_tenant`, refuses
     when `dao_quotas.free_tier_capacity()` reports `is_full=True`,
     and surfaces a waitlist-email path for the full case.  Public
     landing + `/encircle-alternative` carry a live capacity badge
     ("N of M free spots open").  Operator-tunable pool via the
     new `deployment_settings` table — `free_tier_bytes_total`
     defaults to 10 GB (≈102 slots at the 100 MB per-tenant cap);
     the operator bumps it from `/admin` on the back of an EBS
     scale-out.
   * **[shipped]** Token-based invite links with the bypass logic
     from "Sign-up + onboarding".  `dao/invites.py` (mint / get /
     redeem / list / revoke).  Per-tenant `/usage` surface with the
     mint form + outstanding-invites table + revoke.  Identity-vs-
     invite collision handled per spec — actual oauth2-proxy email
     wins, audit logs the rebind.  Audit entries on send / accept /
     revoke.
   * **[shipped]** Bootstrap (self-onboarding) invites.  Operator-
     minted single-use link carrying only a plan + role — no
     tenant, no recipient email — at
     ``POST /admin/onboarding-links``.  Whoever clicks first names
     their own stash on accept and becomes its sole maintainer.
     New ``tenant_bootstrap_invites`` table; ``dao_invites``
     gains ``create_bootstrap`` / ``get_bootstrap_by_token`` /
     ``redeem_bootstrap`` / ``list_open_bootstrap_for_operator`` /
     ``any_token_exists`` (the last one extends the middleware
     bypass).  ``dao_tenants.create_self_serve_tenant`` is the
     bypass-the-operator-check helper the redeem path calls.
     Race-safe consume with ``UPDATE … WHERE consumed_at IS NULL``
     + ``rowcount=0`` check; if the loser of the race created an
     orphan tenant it gets rolled back before the NotFoundError.
     Per-IP throttle on redeem via
     ``check_tenant_creation_rate`` so a stolen operator
     credential can't mint+redeem in a loop to mass-create
     tenants.  Replaces the prior
     ``POST /admin/tenants`` operator-pre-names-tenant route
     entirely.
   * **[deferred]** Postmark templates + transactional send.  Until
     this lands, invite URLs are copy-pasted out-of-band; the
     `/usage` page round-trips the link into `?invite_url=…` so
     it's one tap to copy.

6. **[shipped]** **Object shares.** `share` action on box / item
   detail pages.  "Shared with you" view at `/shared`.  Revocation
   UI.  All four edge cases wired:
   * Cascade-on-add: a box share grants the same role on every
     item currently and eventually in the box.
   * Follows-on-move: per-item shares stick to the item across
     box moves; per-box shares scope by box, so an item moving
     out loses access via the box share.
   * Dedupe with membership: ``max(membership_role, share_role)``
     so a readonly share never narrows a maintainer membership.
   * Paused on soft-delete: a soft-deleted granting tenant
     filters out of the recipient's view + access checks; resumes
     on reactivate.
   Recipient surface is read-only (`/shared/box/{id}`,
   `/shared/item/{id}`) for tonight; maintainer-role write paths
   for share recipients are deferred.

7. **[partial]** **Per-tenant backup + restore + verifiability +
   pre-migration snapshot.**
   * **[shipped]** `dao/backups.py` builds a per-tenant zip
     (filtered DB rows + `uploads/{tid}/` slice + manifest).
     Filtering via `src.backup(dst)` then DELETE-then-VACUUM so
     schema fidelity is exact.  Maintainer-only download at
     `/usage/backup`.  Audit-logs `backup.export` per pull.  The
     operator-side full-DB DR variant stays at `/maintenance/export`.
   * **[deferred]** Per-tenant *import* / restore, the weekly
     verification job, and the pre-migration snapshot wrapper.

8. **[partial]** **B2 nightly DR.** Per-tenant backup uploads.
   KEK lives in a *separate* bucket (and ideally vendor) than the
   data. Configurable retention.
   * **[shipped]** boto3 S3-compatible upload helper keyed at
     `s3://<bucket>/<tenant_id>/<YYYY-MM-DD>.zip`.  Manual
     "Upload to B2" button per tenant on `/admin`; 503s cleanly
     when env vars aren't set.  `B2_KEY_ID` / `B2_APPLICATION_KEY`
     / `B2_ENDPOINT` / `B2_BUCKET` documented in
     `deploy/.env.example` with the loud KEK-separate-bucket
     warning.  Audit-log + `usage_events.backup_bytes` recorded
     per upload.
   * **[deferred]** Nightly scheduler (in-process APScheduler vs
     sidecar — pending an ops-side decision), retention pruner,
     KEK-separate-bucket bootstrap helper, weekly verification
     restore job (cross-cuts with phase 7's verification piece).

9. **[shipped]** **Telemetry.** Wrap the AI clients (Gemini,
   Anthropic) and the upload path to write `usage_events`. No
   enforcement yet — just data collection, so we have a baseline
   before the cap hits.
   * **[shipped]** `dao/usage.py` (`record` + `summary`).  Hooks at
     `save_photo_bytes` (post-encode bytes), `process_ingest_job`
     (gemini_detect), `queue_match` (anthropic_match),
     `generate_box_art` (gemini_art), and the B2 upload path
     (backup_bytes).  `/usage` renders three meters + AI breakdown.
   * **[shipped]** Bandwidth + storage panels.  New
     ``usage_rollups`` table keyed on
     ``(tenant_id, day, surface, kind)`` with an
     ``ON CONFLICT DO UPDATE`` UPSERT so high-volume metrics
     (today: downloads) don't bloat the events log.  ``serve_upload``
     + ``serve_thumb`` ``record_rollup`` the decrypted byte count;
     ``dao_usage.summary`` unions both tables.  Storage footprint
     walks ``UPLOAD_DIR/{tid}/`` on render — current on-disk
     bytes, dropping when items are deleted (the cumulative
     ``upload_bytes`` meter doesn't, by design).  Stress + accuracy
     tests pin: 8-thread × 100-increment UPSERT sums to exactly
     800 (no lost writes), cross-tenant isolation holds under
     load, /uploads + /thumbs byte counts exactly match
     K * len(plaintext), day-boundary writes split into separate
     rows, 2000 rollups complete in under 5 s.
   * **[shipped]** Monthly cost rollup + sparklines.
     ``dao_usage.monthly_summary(tenant_id, months_back=12)``
     unions events + rollups into the last 12 UTC months (oldest
     first), zero-filling quiet months so the sparkline x-axis
     stays continuous.  Inline-SVG ``_sparkline_svg`` Jinja global
     renders a polyline + end-of-line dot per metric; /usage
     "Trends" card shows AI calls / AI cost / upload / download
     per month.  Server-rendered so the chart works without JS
     and prints cleanly.

10. **[shipped]** **Quotas + enforcement + anti-abuse.**
    * `dao/quotas.py` — three caps per tenant
      (``monthly_ai_calls``, ``storage_bytes``,
      ``daily_ai_cost_micros``) with plan defaults (free vs pro)
      + per-tenant overrides via the existing ``quotas`` table
      (the daily-cost field rides in the JSON blob since it
      arrived after the schema).  **Storage is a FLAT cap**, not
      monthly cumulative — ``check_or_raise(surface="upload")``
      reads the current on-disk footprint and refuses if total +
      new > cap.  The original ``monthly_upload_bytes`` column was
      renamed to ``storage_bytes`` in 2026-05-16 when the free
      tier shifted to a delete-to-free-space model (same model as
      a phone's photo library).  AI calls + AI cost stay windowed
      (those reset cleanly on month / day boundaries); only
      storage went flat.
    * Free tier: 100 MB flat storage, 5 GB on Pro.  Free-tier slot
      count is gated by the operator-tunable
      ``deployment_settings.free_tier_bytes_total`` (default 10 GB)
      — see phase 5 + the "Free-tier capacity" /admin card.
    * Enforced at every AI call site (``/ingest``,
      ``queue_match``, ``generate_box_art``) and the upload
      path (``save_photo_bytes``).  ``daily_ai_cost_micros`` is
      the runaway-MCP guard — Gemini-art's high per-call cost
      hits this cap fast even when the monthly call count is
      well under.
    * Soft warning band (80–99%): ``X-Quota-Warning`` response
      header on every non-noisy response (skip thumbs / uploads
      / static).  ``/usage`` meters render used/cap/% with a
      banner in the warning + exceeded bands.
    * Tenant-creation throttle: per-IP cap on
      ``POST /admin/tenants`` (default 5/hour via
      ``STASH_TENANT_CREATION_PER_HOUR``).  Counts against
      ``audit_log.tenant.create`` rows tagged with the source
      IP; missing IPs share an ``unknown`` bucket so a stripped
      X-Forwarded-For doesn't bypass.
    * Operator override editor: per-tenant inline form on
      ``/admin`` lets an operator pin custom caps.
      ``-1`` clears an override (revert to plan default).
      Audit-logs ``quota.override``.
    * **[deferred]** Email-domain blocklist (config plumbing
      lives in ``deploy/.env.example`` since phase 1, no
      enforcement yet); first-interactive-session AI gate
      (waits for self-serve onboarding); inactivity lifecycle
      (lives with phase 14).

11. **[shipped]** **API tokens.** `/api/v1` router with bearer
    auth. Token mint / revoke surface in `/usage`.
    * `dao/api_tokens.py` — mint / authenticate / list / revoke.
      Token shape ``stash_<43 url-safe chars>``; only the SHA-256
      hash lands in the DB (plaintext shown ONCE at mint).
    * `current_actor` middleware short-circuits to bearer when
      `Authorization: Bearer` is present; valid token →
      synthetic Actor with `tenant_id`, `role`, no operator
      flag, no shares.  Per-request log line records
      `api_token=<id>` so agent traffic is greppable.
    * `/api/v1` (api.py): `/me`, `/boxes`, `/boxes/{id}`,
      `/boxes/{id}/items`, `/items` (search), `/items/{id}`,
      POST `/items/{id}/move`, `/locations`, `/rooms`, `/tags`.
      Tenant-scoping holds across every endpoint.
    * `/usage` carries a maintainer-only "API tokens" card with
      a one-tap copy block on mint + per-row revoke.
    * Audit log: `api_token.create` + `api_token.revoke` rows.
    * **[deferred]** Quota enforcement on token traffic (waits
      for phase 10), scoped tokens (read-only / ai-only via the
      `scopes` JSON column already present in the schema).

12. **[shipped]** **Operator dashboard.** `/admin` surface with
    cross-tenant metadata, lifecycle controls (soft-delete /
    reactivate / force-hard-delete), quota overrides, audit-log
    view, vendor cost panel. Explicitly no per-tenant data access.
    * **[shipped]** Tenant roster (counts + plan + lifecycle
      state — never content).  `dao.tenants.list_all` +
      `create_tenant`; `dao.invites.create` operator-bypass for
      cross-tenant minting.  Self-onboarding link mint form (the
      "Create tenant + invite first maintainer" flow was replaced
      in May 2026 — operator now mints a plan-bearing link via
      `POST /admin/onboarding-links`, recipient names the tenant
      themselves; see phase 5).  GET + POST gates 404 (not 403)
      for non-operators so the surface stays opaque.
    * **[shipped]** RBAC pass on `/maintenance` (May 2026).
      Deployment controls (watchtower-driven update, orphan
      cleanup, whole-platform backup export + DB import) used to
      live on the user-facing maintenance page and were reachable
      by any signed-in tenant member.  All four moved to
      ``/admin/maintenance/{update,cleanup,export,import}``
      behind ``_require_operator_route``; non-operators get the
      opaque 404 instead of 403.  `/maintenance` now carries only
      Version + Access + Changelog cards.
    * **[shipped]** In-app feedback triage queue (replaces the
      deferred support loop).  Operator-only `/admin/feedback`
      kanban with status pills (open / accepted / rejected /
      done), per-row screenshot + captured-DOM links, CSV/JSON
      export, MCP triage tools (``admin_list_feedback``,
      ``admin_get_feedback`` with optional ``include`` array,
      ``admin_set_feedback_status``, ``admin_set_feedback_urgent``,
      ``admin_create_feedback``, ``admin_feedback_counts``).
      ``feedback`` + ``feedback_handles`` schema in "Schema
      additions".  Done feedback earns the submitter a star —
      counted via `WHERE status='done' AND actor_email=…`, no
      new column.  Public-leaderboard handle is explicit opt-in
      so a star never reveals an email's local-part.  Urgent
      flag (``feedback.urgent``) floats major-blocker rows to
      the top of each column with a 🔥 pill + red left border.
      Source tagging (``feedback.source``) distinguishes
      ``user_widget`` from ``mcp``-driven sweep findings so
      operators can filter automated noise.
    * **[shipped]** Free-tier capacity card.  Used / available /
      total tiles for the free pool + a "bump the pool" form
      against ``deployment_settings.free_tier_bytes_total``.
      Audit-logs ``settings.change`` per bump.  Slot count =
      pool ÷ per-tenant cap; ``dao_quotas.free_tier_capacity()``
      counts active (not soft-deleted, not inactivity-archived)
      free-plan tenants for the "used" tile.
    * **[shipped]** Tenant last-activity column +  per-member
      ``last_active_at`` (joined on ``audit_log.actor_email``)
      surfaced as a disclosure under each tenant row.
    * **[shipped]** Quota override editor — per-tenant inline
      form with placeholder=current-cap and ``-1``-clears-the-
      override semantics.  Audit-logs ``quota.override``.
      (Cross-listed in phase 10; surfaced here because the
      operator panel is where it lives.)
    * **[shipped]** Audit-log read view —
      ``dao/audit.list_recent_for_operator`` joined on
      ``tenants.name``; rendered as a "Recent activity" card
      with the last 50 entries.  Cross-tenant NULL-tenant rows
      (operator actions like ``oauth.client.register``) keep
      ``tenant_name = null`` and stay visible.
    * **[shipped]** Cross-tenant API token panel with
      operator-revoke / suspend / resume +  client-side filter
      bar (tenant / state / role / name substring).  No server
      round-trip; works on hundreds of tokens.
    * **[shipped]** Lifecycle controls.
      ``dao_tenants.soft_delete`` stamps ``deleted_at`` +
      ``hard_delete_after = now + 30d`` (grace window before the
      eventual hard-delete sweep — share-pause behaviour from
      phase 6 already keys off ``deleted_at``).
      ``reactivate`` clears both columns; idempotent on active
      tenants.  ``hard_delete`` drops the row + every cascade-
      referenced row, audit-logs at ``tenant_id=NULL`` so the
      record survives the cascade, and refuses to delete the
      operator's own tenant (would lock them out).  /admin POST
      routes for all three.  Hard-delete requires a typed
      ``confirm=<tenant_name>`` form field so an accidental
      click can't nuke a tenant.
    * **[shipped]** Vendor cost panel.
      ``dao_usage.operator_cost_summary`` aggregates AI spend
      across every tenant for the current UTC month: per-kind
      breakdown (``gemini_detect``, ``gemini_art``,
      ``anthropic_match``, …) + per-tenant rollup with names +
      AI calls + AI cost + upload bytes + download bytes.
      Hard-rule honoured: names + counters + costs only,
      never tenant content.
    * **[shipped]** Operator-side OAuth client list.
      ``dao_oauth.list_clients`` (already shipped in phase 19)
      rendered on /admin with per-row revoke.  Revoke flips
      ``revoked_at`` so new auth-code + refresh exchanges fail;
      existing access tokens keep working until natural expiry
      (we don't iterate ``api_tokens`` to mass-revoke).

13. **[partial]** **User usage page + cost transparency + GDPR
    controls.**
    * **[shipped]** `/usage` rebuilt as the per-tenant home for
      plan / role / quotas / sparkline trends / outbound shares /
      API tokens / backups download / billing card.  Quota meters
      with 80%/100% banners.  Stripe upgrade card when billing is
      configured.
    * **[shipped]** Public marketing pages: `/about/pricing`,
      `/about/transparency` (rebuilt 2026-05-16 with three tables
      that actually close the math — per-Pro variable, fixed AWS,
      pool-clearing arc; see "Recent ships" above),
      `/about/sub-processors`, `/about/privacy`, `/about/dpa`,
      `/about/terms`.  Mobile-first header / hero / footer pass on
      the public surface alongside.
    * **[deferred]** The five-line per-tenant cost-transparency
      block on /usage (Direct vendor passthrough / Community
      backups / Community free-tier / Operator payout / Margin)
      that reconciles to the aggregate `/about/pricing` view —
      the aggregate cost panel exists on /admin (phase 12) but
      the per-tenant breakdown UI hasn't shipped.  "Download my
      data" GDPR-portability bundle also deferred.

14. **[partial]** **Tenant lifecycle.**
    * **[shipped]** Soft-delete / reactivate / hard-delete
      controls on /admin (cross-listed in phase 12).
      ``deleted_at`` + ``hard_delete_after`` columns, 30-day grace
      window, hard-delete requires typed ``confirm=<tenant_name>``.
    * **[shipped, policy + schema only]** 180-day free-tier
      inactivity → B2 cold archive.  ``tenants.archived_at`` +
      ``tenants.archive_b2_key`` columns added (separate from the
      soft-delete `archived_backup_*` columns — see "Free-tier
      inactivity archive vs. soft-delete archive" in the Schema
      section).  Policy declared on /about/pricing +
      /about/transparency + /signup.  Pro tenants are never
      archived for inactivity.
    * **[deferred]** Scheduled hard-delete job (cron-driven
      promotion past `hard_delete_after`), archived-backup
      retention pruner, the inactivity sweep itself (background
      or operator-triggered), the B2 cold-storage upload path,
      and the operator-approved recovery flow that brings an
      archived tenant back.

15. **[shipped]** **Account menu (top-right) with tenant switcher
    + sign-out.** Persistent avatar/initials dropdown in the
    global header.  Always renders for any signed-in user (so the
    Sign-out link is reachable even on a single-tenant account);
    the inner Switch-tenant section only renders when the user has
    >1 membership or any shares (no clutter for the common case).
    * Cookie ``stash_active_tenant`` is the source of truth — read
      in the actor middleware and only honoured when its value
      matches an entry in ``actor.memberships``, so a stale or
      forged cookie silently falls back to ``memberships[0]``
      (worst case: brief "wrong tenant" view, never a lockout).
    * ``POST /tenants/switch`` validates membership before setting
      the cookie (HttpOnly, SameSite=Lax, Secure=auto over HTTPS),
      then redirects to a validated ``next`` (open-redirect guard
      rejects ``//`` and off-scheme paths).
    * CSS-only ``<details>``-based dropdown — works without JS.
      Account menu panel carries (a) the signed-in email header,
      (b) the tenant-switcher section gated on multi-membership /
      shares, (c) a "Sign out" link to ``/oauth2/sign_out?rd=/``
      (handled by oauth2-proxy; stash never sees the request).
    * Tests cover: switcher section appears for multi-tenant +
      hides for single-tenant, switch route sets cookie +
      redirects, 404 on non-member tenant, 400 on non-int,
      invalid cookie falls back silently, ``next=//evil`` blocked.

16. **[shipped]** **Logging pass.** Layered `LoggerAdapter`s,
    request-id middleware, structured JSON output.  Backfill
    `audit_log` writes on key actions.
    * `obs.py` — per-request contextvars (`request_id`,
      `actor_email`, `tenant_id`, `surface`), `get_logger(layer)`
      that merges them into every record's `extra`, JSON +
      pretty formatters toggled by `STASH_LOG_FORMAT` (default
      pretty).  `STASH_LOG_LEVEL` for verbosity.
    * `current_actor` middleware stamps a fresh request_id (or
      trusts an inbound `X-Request-Id` capped at 64 chars), binds
      the actor + tenant context, emits a per-request
      `METHOD path -> status in Nms` line, sets `X-Request-Id`
      on the response so a log-grep workflow correlates without
      devtools digging.
    * Audit-log backfill: every box / item / location / floor /
      room / share / invite / backup / tenant mutation writes an
      `audit_log` row inside the same transaction (via the
      canonical `obs.write_audit`).  Rolled-back mutations never
      leave orphan audit entries.

17. **Second locale.** Pick a target language (likely Spanish or
    French based on demand), populate `locale/<lang>/messages.po`,
    flip the language picker on. Pure translation work — the
    engineering already shipped in step 1.

18. **[shipped]** **Built-in MCP endpoint.** Single ``/mcp``
    route speaking Streamable HTTP rev 2025-11-25, bearer-auth
    via the existing api_tokens surface.
    * ``mcp_server.py`` — JSON-RPC dispatch + tool/resource
      registry + header validation (Origin allow-list,
      Accept, MCP-Protocol-Version pinned at ``2025-11-25``,
      no fallback).
    * 15 tools: ``me``, ``find_items``, ``get_item``,
      ``list_boxes``, ``get_box``, ``list_locations``,
      ``list_rooms``, ``list_tags``, ``inventory_room``,
      ``move_item``, ``create_item``, ``update_item``,
      ``add_tag``, ``remove_tag``, ``mark_missing``.  Read tools
      idempotent + cheap; write tools one-shot + fail loud on
      bad targets.
    * 4 resources: ``stash://{items,boxes,rooms,locations}/{id}``.
    * Photo bytes: ``get_item(include_photo='none|thumb|full')``
      returns MCP ``ImageContent`` with base64-encoded JPEG.
      ``full`` records ``upload_bytes`` telemetry against quota
      so a hammering agent shows up in the cap meters.
    * Telemetry: every tool call writes ``surface='mcp'`` with
      ``kind='mcp.<tool_name>'`` so the cost-transparency block
      breaks out agent-vs-human usage.
    * Quota integration: 80–99% band surfaces in
      ``_meta.warnings`` on every tool result; 429 surfaces as
      ``isError: true`` with the cap details.
    * Error mapping: NotFoundError → tool error,
      ForbiddenError → tool error, ConflictError → tool error,
      ValueError / TypeError → tool error.  Auth failures
      (revoked / suspended / over-HTTP) come through the same
      phase-11 path → 401 + JSON-RPC error code -32001.
    * GET /mcp returns 405 (no server-push v1).
      DELETE /mcp returns 405 (we opt out of MCP-Session-Id).
    * Kill switch: ``STASH_MCP_ENABLED=false``.
    * Origin allow-list: ``STASH_PUBLIC_URL`` plus optional
      comma-separated ``STASH_MCP_ALLOWED_ORIGINS`` for dev
      clients (Claude Desktop loopback, IDE hosts).

19. **[shipped]** **OAuth 2.1 authorization server.** Stash plays
    both resource-server (``/mcp``) and authorization-server
    roles for MCP per spec rev 2025-11-25 §"Authorization".  Lets
    claude.ai's web custom-connector dialog (and any
    discovery-aware MCP client) bootstrap without per-user JSON
    config.
    * Discovery (RFC 9728 + RFC 8414):
      ``/.well-known/oauth-protected-resource`` and
      ``/.well-known/oauth-authorization-server`` — both public.
    * Authorization-code grant with PKCE (S256 only — spec
      mandates).  Authorization codes are 60 s single-use,
      tied to PKCE challenge + redirect_uri + client_id.
    * Refresh token rotation per OAuth 2.1 §4.3.1 — the old
      refresh token is consumed on every successful exchange.
    * Resource indicators (RFC 8707) — every issued access
      token is bound to ``audience=<public_url>/mcp``; the
      bearer path on /mcp validates this.  Legacy user-minted
      tokens (NULL audience) keep working for backwards
      compatibility.
    * Dynamic Client Registration (RFC 7591) at
      ``POST /oauth/register`` — public clients (PKCE, no
      client_secret) and confidential clients (with one-time
      secret) both supported.  Disable via
      ``STASH_OAUTH_DCR_ENABLED=false``.
    * Consent UX: ``GET /oauth/authorize`` renders a tenant
      picker so a user with multiple memberships chooses which
      one the agent gets access to at consent time.  Approve
      bounces to the client's ``redirect_uri?code=…&state=…``;
      deny bounces with ``error=access_denied``.
    * Bearer leak guards (phase 11) cover OAuth-issued tokens
      automatically — same auto-revoke on HTTP, same operator
      suspend/revoke from /admin.
    * 401 responses on /mcp carry ``WWW-Authenticate: Bearer
      resource_metadata=…`` so a discovery-aware client finds
      the AS without manual config.
    * Deploy: oauth2-proxy ``OAUTH2_PROXY_SKIP_AUTH_ROUTES`` lets
      the OAuth + bearer endpoints reach stash without a Google
      session cookie.  ``/oauth/authorize`` stays gated (it's
      browser-driven and needs the user identity).
    * **[deferred]** Operator OAuth client list at /admin
      (registrations are visible via DCR audit-log but a
      first-class panel is nicer).  Step-up authorization (spec
      §"Scope Challenge Handling") for granular scopes
      (``mcp.read`` / ``mcp.write``).  Client ID Metadata
      Documents — overkill for a single-deploy stash.


(Support / Sentry — deferred.  In-app feedback shipped out-of-phase;
see phase 12 + the "Recent polish ships" block.)

## Migration plan (live user)

Done on a feature branch with a copy of the production DB to validate.

1. Run additive schema migration (new tables + `tenant_id` columns;
   columns nullable for the migration only).
2. `INSERT INTO tenants (id, name, plan) VALUES (1, 'Personal', 'pro')`.
3. `INSERT INTO tenant_members (tenant_id, email, role, joined_at)
   VALUES (1, '<live user email>', 'maintainer', CURRENT_TIMESTAMP)`.
4. `UPDATE <every owned table> SET tenant_id = 1 WHERE tenant_id IS
   NULL`.
5. Tighten the schema: `ALTER TABLE ... DROP COLUMN`-equivalent or
   add a CHECK; SQLite's column constraints make
   `NOT NULL DEFAULT 1` the realistic choice.
6. `mkdir UPLOAD_DIR/1`; `mv UPLOAD_DIR/*.* UPLOAD_DIR/1/`.

A **migration regression test** restores the existing prod backup zip
into a fresh DB, runs the migration, and asserts: every row has
`tenant_id = 1`, every photo file lives under `UPLOAD_DIR/1/`, the
live user can sign in and reach every previously-accessible URL with
no 403/404.

## Test strategy

- **CI grep:** `git grep -n 'conn.execute(' -- '!dao/'` returns zero
  hits, or the build fails. Same for any direct
  `(UPLOAD_DIR / )` outside the file-IO helper.
- **Tenancy isolation tests:** for every read DAO method, assert that
  rows of tenant B don't appear when querying as tenant A. Use a
  dual-tenant fixture.
- **Role enforcement tests:** every mutation method called with a
  readonly actor 403s before any side-effect.
- **Object-share tests:** recipient sees only the shared object;
  attempts to access sibling items in the same box (when sharing was
  per-item) 403.
- **Quota tests:** a tenant past its AI cap gets 429 on `/ingest`,
  but `/`, search, and label generation keep returning 200.
- **Migration round-trip:** existing prod backup restores into the
  new schema and the live user's session reaches every page.
- **Backup round-trip per tenant:** ship the existing
  `test_export_includes_floorplans_and_background_art` /
  `test_import_round_trip_restores_floorplans_and_background_art`
  but assert tenant_id is preserved and rows of other tenants don't
  leak.
- **UI regression net:** `tests/ui/` is a pytest-playwright suite
  that drives a real browser against uvicorn spawned in a
  subprocess.  In-process TestClient cannot catch CSS / JS bugs;
  feedback #37/#41/#46 lived entirely in a one-line CSS
  specificity collision and survived three rounds of patch
  attempts because nothing in the tree ever rendered the page in
  a browser to validate.  Any non-trivial CSS / JS change should
  ship a UI test that fails when reverted.
- **Parallel test runs.** Default pytest invocation uses
  ``pytest-xdist`` (worker count auto-derived from CPU count) so
  the suite — including the UI tests — stays under a sane CI
  budget.  Tests that genuinely cannot run in parallel (DB-global
  state, env-var mutation) declare ``@pytest.mark.serial`` and
  land in the dedicated single-worker group.

## Commit message conventions

Every commit follows [Conventional Commits](https://www.conventionalcommits.org/) —
that's what release-please reads to build the CHANGELOG and decide
version bumps.  Format:

```
<type>(<scope>): <subject>

[optional body]

[optional footer]
```

**Types we use** (anything else is ignored by release-please and
disappears from the changelog):

- `feat` — new user-facing functionality.
- `fix` — bug fix.
- `docs` — spec / README / comment-only changes.
- `test` — test-only changes.
- `refactor` — internal restructuring with no behaviour change.
- `chore` — release commits, dependency bumps, CI tweaks.

**Scopes are optional but conventional ones in this project include:**
`tenancy`, `crypto`, `db`, `i18n`, `maintenance`, `security`,
`queue`, `index`.  Pick one if it exists, omit if the change is
genuinely cross-cutting.

**Breaking changes** get one of (or both):

- `!` after the type / scope: `feat(tenancy)!: ...`.
- A `BREAKING CHANGE: <description>` footer.

release-please bumps minor in the 0.x series and major in 1.x+ for
either form.

**Multi-step work that lands as several commits** (e.g. a phased
roadmap step) — every individual commit gets a real conventional
type.  Subjects like `phaseN(M/N): ...` aren't recognised types, so
release-please drops them on the floor and the work disappears from
the changelog.  We learned this the hard way once; the lesson lives
here so we don't repeat it.

## Open decisions

- **Free-tier numerical caps.** Storage cap resolved at **100 MB
  flat** (delete-to-free-space, not monthly cumulative).  The
  free pool itself is operator-tunable via the
  ``deployment_settings.free_tier_bytes_total`` row; default
  10 GB → 102 slots.  AI quota is still TBD — pick after the
  telemetry step lands a baseline of typical free-user behaviour.
  Caps may shrink as the free base grows; the tier itself is
  permanent, and existing free accounts are grandfathered (cap
  changes apply to new signups only).
- **Paid tier shape.** Single Pro tier with transparent cost
  breakdown, vs. metered "pay for what you use" billing. Both fit
  the ethos; metered is more legible but harder to operate. Decide
  after the cost-transparency block exists and we know what real
  per-tenant cost curves look like.
- **Stripe integration.** Resolved — Stripe Checkout + webhook-driven
  entitlement landed.  Pro tier = bigger quotas only (see
  ``_PLAN_DEFAULTS``); future iteration may add feature gates.  See
  ``dao/billing.py`` + ``/usage`` upgrade card + ``/webhooks/stripe``.
  Configurable via ``STRIPE_SECRET_KEY`` + ``STRIPE_WEBHOOK_SECRET`` +
  ``STRIPE_PRICE_ID_PRO`` env vars; stash hides the upgrade CTA when
  any are unset.
- **AI quota grace.** When a free tenant blows through their AI cap
  mid-ingest, do we (a) hard-fail the rest of the batch, (b) finish
  the current photo and 429 the next one, or (c) burn into a
  per-tenant overdraft and surface it as "this batch put you $0.12
  over budget; please upgrade." Lean toward (b) — predictable,
  doesn't punish users for one over-the-line photo.
- **Encryption-key custody for "no platform vendor lock-in."** A
  user who wants to migrate to self-hosted needs the KEK. Today
  that means an operator hand-off; eventually it could be a
  per-tenant-derived key the user controls (passphrase). Trade-off
  is "operator can't help with debug" if the user holds the only
  key. Defer until the self-host story is real demand.
- **Second locale target.** Pick the first non-English language
  based on actual demand — Spanish and French are likely candidates
  but the choice is downstream of seeing where users actually come
  from.
- **Sub-processor for translation.** If we ever pay for human
  translation, the translator becomes a sub-processor (they read
  user-visible product copy but not user data). Pick a vendor with
  a clean DPA; or keep translation contributor-driven via PRs.

## Resolved decisions

- **Operator emergency access** (originally: "log to tenant audit
  log AND global?"): there is no operator emergency-access path
  through the admin surface. Operators read only metadata + usage;
  for tenant-data access they request a maintainer invite from the
  user, same flow as a partner. The one exception is photo
  decryption via the `stash-recover` CLI, which is a separate
  audited path with mandatory tenant notification.
- **Encryption at rest**: per-tenant DEK + envelope encryption with
  `STASH_KEK` for wrapping. Photos + thumbnails encrypted on disk;
  DB columns stay cleartext for searchability in v1. Operator
  decrypt path is a CLI tool that audits + emails the affected
  tenant.
- **`FULLY_PUBLIC`**: removed after the migration. Tests explicitly
  set up a tenant + actor; there is no "no allowlist" mode in
  production any more.
- **Tenant switcher**: top-right header, modern SaaS pattern (avatar
  / initials → dropdown). See roadmap step 15.
- **Tag uniqueness**: per-tenant `(tenant_id, name)` unique; no
  global tag namespace.
- **Tenant deletion**: soft-delete with 30-day grace, then hard-delete
  via cron, archived backup retained on B2 for one year. Operator
  can force immediate hard-delete for GDPR. UX described in "Tenant
  lifecycle".
- **Sign-up flow**: self-serve tenant creation on first sign-in for
  fresh emails (shipped 2026-05-16 via ``POST /signup``, gated by
  the operator-tunable free-tier capacity pool); invite tokens and
  active object shares act as bypass tickets through the global
  allowlist for first sign-in.  See "Sign-up + onboarding".
- **Free-tier inactivity policy**: 180 days of no audit-log
  activity → B2 cold archive (recoverable; see Tenant lifecycle
  schema for the ``archived_at`` / ``archive_b2_key`` columns
  and the contrast with the soft-delete archive surface).  Pro
  accounts are never inactivity-archived.  Policy declared on
  /about/pricing + /about/transparency + /signup so newcomers
  see the trade before committing.
- **Notification provider**: Postmark; SES as fallback if cost
  becomes the deciding factor. Local dev writes to `mail/`.
- **Anti-abuse posture**: tenant-creation throttle, email-domain
  blocklist, AI gated until first interactive session, inactivity
  lifecycle. No captcha by default — the ethos doesn't want that
  wall.
- **Concurrent edits**: optimistic concurrency via `version` column
  and `If-Match`; 409 surfaces a refresh prompt. No live-collab.
- **API tokens**: `/api/v1` router, bearer auth, per-tenant scoped
  tokens minted from `/usage`.
- **CSRF posture**: cookie forms stay single-origin (oauth2-proxy +
  Caddy + SameSite=Lax); API router is bearer-only.
- **SQLite pragmas**: `journal_mode=WAL`, `busy_timeout=5000`,
  `synchronous=NORMAL` at every connection.
- **Photo metadata strip**: full EXIF strip on save (GPS, MakerNote,
  XMP, raw-profile), not just orientation.
- **Identity-vs-invite collision**: invite token binds to whatever
  email the current sign-in claims; redirection logged to the
  granting tenant's audit log.
- **GDPR posture**: Article 15 / 17 / 20 are explicit user-facing
  features (Download my data, soft+hard delete, machine-readable
  export). Sub-processors disclosed at `/about/sub-processors`.
  72-hour breach notification capability lands with the lifecycle
  work. No marketing or analytics, so Articles 21/22 are largely
  trivial.
- **Localization seams**: `gettext` wrapper + `babel` formatting
  ship in the very first refactor (roadmap step 1) so the second
  language is a translation task rather than an engineering task.
  v1 production deployment is English-only.

## Out of scope (this spec)

- Mobile capture / PWA install (item #3 from the original plan — it
  ships parallel to but independent of multi-tenancy).
- Stripe / billing wiring (quotas exist; payment doesn't).
- Public anonymous shares.
- Item versioning / edit history beyond the audit log.
- Insurance-recovery branding pass.
- Searchable encryption / blind indexes for DB columns (photos +
  thumbs are encrypted in v1; names/notes/tags stay cleartext for
  search, encrypting them is a future round).
- Per-tenant custom domains.
- Tenant ownership transfer (the maintainer who created the tenant
  effectively owns it; transfer is operator-side for now).
- Real-time collaborative editing (optimistic concurrency only).
- Push notifications / webhooks (email-only for v1).
- KEK rotation tooling (the rotation procedure is documented but
  not automated).
- Field-level audit log (action-level only — names/notes changes
  appear as `item.update` entries without before/after diffs).

---

*Living document. Update as decisions land or open questions resolve.
Each section above maps to one or more roadmap steps; if a section's
behaviour changes, change it here first.*
