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

Three resolved sign-in shapes:

1. **Fresh email, no relationship to any tenant.** Land on a
   "create your stash?" screen. Self-serve tenant creation,
   opt-in. The user becomes the sole maintainer of a Personal
   tenant with free-tier defaults. No operator approval needed.
2. **Sign-in following an invite link.** The invite token in the
   URL bypasses the global allowlist for *this* sign-in only,
   completes authentication, and adds the user as a member of the
   inviting tenant at the granted role. They may also self-create
   their own tenant later from the switcher.
3. **Sign-in with an active object share but no member/invite
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

1. **Schema + actor middleware + i18n seams + SQLite pragmas.** Add
   the new tables, add `tenant_id` to every owned table, backfill all
   existing rows to `tenant_id = 1` plus a "Personal" tenant with the
   live user as sole maintainer. Replace `enforce_email_allowlist`
   with a `current_actor` middleware that resolves
   `email → tenant + role` (also handling the invite / share
   bypass paths from "Sign-up + onboarding"). Wrap every user-facing
   string in `gettext()` and route dates through `babel` from day
   one — even though v1 ships English-only, retrofitting i18n later
   is the kind of mass-edit nightmare we're avoiding. Set WAL +
   busy_timeout pragmas on every connection. Pure additive at the
   tenancy layer; live user keeps working.

2. **Encryption at rest.** Per-tenant DEK + envelope encryption with
   a `STASH_KEK` env var. Encrypt photos and thumbs on write,
   decrypt streaming on read. Re-write existing photos through the
   encrypt path during the live-user migration. CLI `stash-recover`
   tool for audited operator decryption.

3. **DAO module — read paths.** Build `dao/` with one module per
   aggregate (`boxes`, `items`, `rooms`, etc.). Migrate every read
   route to call the DAO. CI lint: any `conn.execute(` outside `dao/`
   fails the build. Add tenancy assertions to every DAO read method.

4. **DAO mutation paths + optimistic concurrency.** Migrate writes
   to DAO, lower-traffic routes first. Mutation methods enforce role
   at the DAO. Routes pre-check role. Add `version` columns and
   `If-Match` semantics on mutable rows.

5. **Email delivery (Postmark) + invites.** Templates for the
   notification surfaces. `/usage/members` page. Token-based invite
   links with the bypass logic from "Sign-up + onboarding". Audit
   entries on send + accept.

6. **Object shares.** `share` action on box / item detail pages.
   "Shared with you" view at `/shared`. Revocation UI. Edge cases
   from the spec wired explicitly: cascade on add, share-follows on
   item move, dedupe for tenant members, paused on
   granting-tenant-soft-delete.

7. **Per-tenant backup + restore + verifiability + pre-migration
   snapshot.** Move `/maintenance/export` and `/maintenance/import`
   into per-tenant `/usage/backup`. Operator keeps the global DR
   variant. Add the weekly verification job and the
   pre-migration snapshot wrapper.

8. **B2 nightly DR.** Per-tenant backup uploads. KEK lives in a
   *separate* bucket (and ideally vendor) than the data. Configurable
   retention.

9. **Telemetry.** Wrap the AI clients (Gemini, Anthropic) and the
   upload path to write `usage_events`. No enforcement yet — just
   data collection, so we have a baseline before the cap hits.

10. **Quotas + enforcement + anti-abuse.** Soft caps in
    router-prefix middleware. Banners on usage page. 429s on gated
    surfaces. Tenant-creation throttle + email-domain blocklist +
    first-interactive-session AI gate + inactivity lifecycle.

11. **API tokens.** `/api/v1` router with bearer auth. Token mint /
    revoke surface in `/usage`. Token traffic counts toward quotas.

12. **Operator dashboard.** `/admin` surface with cross-tenant
    metadata, lifecycle controls (soft-delete / reactivate /
    force-hard-delete), quota overrides, audit-log view, vendor cost
    panel. Explicitly no per-tenant data access.

13. **User usage page + cost transparency + GDPR controls.**
    `/usage` rebuilt as the per-tenant home for plan / role / quotas
    / backups / members / shares / locale-prefs / notification-prefs.
    The five-line cost-transparency block ships in the same view.
    "Download my data" GDPR-portability bundle. `/about/pricing`,
    `/about/sub-processors`, `/about/privacy`, `/about/dpa` static
    pages.

14. **Tenant lifecycle.** Soft-delete UX, reactivate flow, scheduled
    hard-delete job, archived-backup retention on B2.

15. **Tenant switcher (top-right).** Persistent SaaS-pattern
    avatar/initials menu in the global header: list of tenants the
    user is a member of, "Shared with you" entry, account/usage
    link, sign out. Active tenant marked, others one click away.
    Stays consistent across pages.

16. **Logging pass.** Layered `LoggerAdapter`s, request-id
    middleware, structured JSON output. Backfill `audit_log` writes
    on key actions.

17. **Second locale.** Pick a target language (likely Spanish or
    French based on demand), populate `locale/<lang>/messages.po`,
    flip the language picker on. Pure translation work — the
    engineering already shipped in step 1.

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
  fresh emails; invite tokens and active object shares act as
  bypass tickets through the global allowlist for first sign-in.
  See "Sign-up + onboarding".
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
