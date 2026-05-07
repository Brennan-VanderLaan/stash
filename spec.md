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

## Ethos: "software that doesn't hate you"

Stash takes a deliberately unfashionable position on pricing and
trust:

- **Show your work.** The usage page lists every paid backend in
  play (Gemini, Anthropic, Backblaze B2, host), what each one
  charges per unit, what *this tenant* used last cycle, and the
  platform's gross margin on that cycle. No black-box "Pro tier
  $5/mo" — users see where the money goes.
- **Small margins on top of real cost, not max-extract pricing.**
  Goal is in-the-black, not gouging. The per-tenant breakdown should
  make it obvious that the price is close to actual cost.
- **Free tier is genuinely usable.** Initial caps: **100 MB photo
  storage, one retained backup, modest AI quota** (placeholder until
  we have telemetry; see "Open decisions"). Caps may shrink as the
  free base grows, but the tier doesn't disappear.
- **Operators can't read your data.** Support requests are handled
  by the user inviting the operator into their tenant — same path
  as inviting a partner. No backdoor, no "view as user" mode in the
  admin dashboard. (See "Operator surface" below.)
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
    surface TEXT NOT NULL,        -- 'ai' | 'upload' | 'backup' | 'core'
    kind TEXT NOT NULL,           -- 'gemini_detect' | 'gemini_art' | 'anthropic_match' | 'upload_bytes' | 'backup_bytes'
    units INTEGER NOT NULL,       -- request count, byte count, etc.
    cost_micros INTEGER NOT NULL DEFAULT 0,
    created_at TEXT
)
-- Index on (tenant_id, surface, created_at) for monthly rollups.

quotas(
    tenant_id INTEGER PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    monthly_ai_calls INTEGER,
    monthly_upload_bytes INTEGER,
    backup_storage_bytes INTEGER,
    -- NULL on any field = no cap (operator override or paid tier).
    overrides_json TEXT
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

Migration: `mv` every existing file into `UPLOAD_DIR/1/...`. Same
filenames, just deeper path. The `_thumb_path` helper rebuilds inside
the new prefix.

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
- Operator-only DR: full-DB import (today's
  `/maintenance/import`) for the catastrophic case where the whole
  deployment needs reseeding. Logs prominently to the global audit
  log.
- Quota override editor.
- Vendor cost panel — what the platform paid each vendor last
  cycle, total revenue, gross margin. The numbers shown here are the
  source of truth for the in-app price-transparency block.

### User-facing usage page (replaces `/maintenance` for tenants)

- Plan + role at the top.
- Three meters: storage, AI calls, backup retention — each with this
  cycle's used / cap, plus a sparkline of the last 30 days.
- **Cost transparency block.** A table of every paid backend (Gemini
  Flash, Gemini 3 Pro Image, Anthropic Opus, Backblaze B2, host
  share), what each charges per unit, this tenant's last-cycle usage
  in those units, vendor cost on the tenant's behalf, what the
  tenant paid (free tier shows $0), and the platform's margin. Same
  data the `/admin` vendor cost panel rolls up — different framing,
  same source numbers, so the public claim and the operator view
  can never disagree.
- Backups list with download links. Per-tenant restore.
- Members + invites table.
- Outbound shares (boxes/items I've shared) with revoke.
- **Delete tenant** action with the soft-delete grace period clearly
  explained ("you have 30 days to reactivate; one backup will be
  retained beyond that for one year"). Maintainer-only.

The only "destructive" actions exposed here are per-tenant restore
and tenant soft-delete; the operator surface handles cross-tenant DR
and post-grace hard-delete.

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
    archived_backup_key TEXT,        -- B2 key of the retained zip
    archived_backup_until TEXT       -- when that zip will be purged
)
```

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

1. **Schema + actor middleware.** Add the new tables, add `tenant_id`
   to every owned table, backfill all existing rows to `tenant_id =
   1` plus a "Personal" tenant with the live user as sole
   maintainer. Replace `enforce_email_allowlist` with a
   `current_actor` middleware that resolves
   `email → tenant + role`. Routes still talk to the DB the old way —
   but every request has a resolved actor on
   `request.state`. Pure additive; live user keeps working.

2. **DAO module — read paths.** Build `dao/` with one module per
   aggregate (`boxes`, `items`, `rooms`, etc.). Migrate every read
   route to call the DAO. CI lint: any `conn.execute(` outside `dao/`
   fails the build. Add tenancy assertions to every DAO read method.

3. **DAO mutation paths.** Same pattern, lower-traffic-first.
   Mutation methods enforce role at the DAO. Routes pre-check role
   (so a forbidden action 403s without DB work) but the DAO is the
   source of truth.

4. **Tenant invites.** `/usage/members` page. Token-based invite
   links. Single-use. Audit log entry on send + accept.

5. **Object shares.** `share` action on box / item detail pages.
   "Shared with you" view at `/shared`. Revocation UI.

6. **Per-tenant backup + restore.** Move `/maintenance/export` and
   `/maintenance/import` into the per-tenant `/usage/backup` flow.
   Operator surface keeps a global DR variant.

7. **B2 nightly DR.** Background job on a cron schedule (or a
   long-running asyncio task pinned to wall-clock midnight). Tenant
   backups uploaded with the keying above. Configurable retention.

8. **Telemetry.** Wrap the AI clients (Gemini, Anthropic) and the
   upload path to write `usage_events`. No enforcement yet — just
   data collection, so we have a baseline before the cap hits.

9. **Quotas + enforcement.** Soft caps wired into router-prefix
   middleware. Banners on usage page. 429s when surfaces are gated.

10. **Operator dashboard.** `/admin` surface with cross-tenant
    metadata, lifecycle controls (soft-delete / reactivate /
    force-hard-delete), quota overrides, audit-log view, vendor cost
    panel. Explicitly no per-tenant data access.

11. **User usage page + cost transparency.** `/usage` rebuilt as the
    per-tenant home for plan/role/quotas/backups/members/shares,
    plus the public cost-transparency block driven from the same
    vendor cost numbers `/admin` rolls up.

12. **Tenant lifecycle.** Soft-delete UX, reactivate flow, scheduled
    hard-delete job, archived-backup retention on B2.

13. **Tenant switcher (top-right).** Persistent SaaS-pattern
    avatar/initials menu in the global header: list of tenants the
    user is a member of, "Shared with you" entry, account/usage
    link, sign out. Active tenant marked, others one click away.
    Stays consistent across pages.

14. **Logging pass.** Layered `LoggerAdapter`s, request-id middleware,
    structured JSON output. Backfill `audit_log` writes on key
    actions.

(Support / Sentry / in-app feedback link — deferred.)

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

## Open decisions

- **Free-tier numerical caps.** Initial values: 100 MB photo storage,
  one retained backup. AI quota is still TBD — pick after the
  telemetry step lands a baseline of typical free-user behaviour.
  Caps may shrink as the free base grows; the tier itself is
  permanent.
- **Paid tier shape.** Single Pro tier with transparent cost
  breakdown, vs. metered "pay for what you use" billing. Both fit
  the ethos; metered is more legible but harder to operate. Decide
  after the cost-transparency block exists and we know what real
  per-tenant cost curves look like.
- **Stripe integration.** Out of scope for this round — quotas exist
  so the billing boundary is wired when payment lands.
- **AI quota grace.** When a free tenant blows through their AI cap
  mid-ingest, do we (a) hard-fail the rest of the batch, (b) finish
  the current photo and 429 the next one, or (c) burn into a
  per-tenant overdraft and surface it as "this batch put you $0.12
  over budget; please upgrade." Lean toward (b) — predictable,
  doesn't punish users for one over-the-line photo.

## Resolved decisions

- **Operator emergency access** (originally: "log to tenant audit
  log AND global?"): there is no operator emergency-access path.
  Operators read only metadata + usage; for tenant-data access they
  request a maintainer invite from the user, same flow as a
  partner. Audit log only ever holds the lifecycle/quota actions an
  operator *can* take, which all log to the global audit log.
- **`FULLY_PUBLIC`**: removed after the migration. Tests explicitly
  set up a tenant + actor; there is no "no allowlist" mode in
  production any more.
- **Tenant switcher**: top-right header, modern SaaS pattern (avatar
  / initials → dropdown). See roadmap step 13.
- **Tag uniqueness**: per-tenant `(tenant_id, name)` unique; no
  global tag namespace.
- **Tenant deletion**: soft-delete with 30-day grace, then hard-delete
  via cron, archived backup retained on B2 for one year. Operator
  can force immediate hard-delete for GDPR. UX described in "Tenant
  lifecycle".

## Out of scope (this spec)

- Mobile capture / PWA install (item #3 from the original plan — it
  ships parallel to but independent of multi-tenancy).
- Onboarding flow (item #4 — same).
- Stripe / billing.
- Public anonymous shares.
- Item versioning / edit history beyond the audit log.
- Insurance-recovery branding pass.

---

*Living document. Update as decisions land or open questions resolve.
Each section above maps to one or more roadmap steps; if a section's
behaviour changes, change it here first.*
