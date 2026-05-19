# CI/CD — branches, builds, deploys

Living document.  Pairs with `Spec.md` (product surface) and
`deploy/README.md` (the prod operator playbook).

## The shape

```
   ┌──────────────────┐
   │  feature branch  │  ── PR ─→  dev          (pr-checks: fast tests)
   └──────────────────┘
              │ merge
              ▼
   ┌──────────────────┐
   │      dev         │  ── push ─→  build.yml  (fast tests +
   └──────────────────┘                          build :dev + :dev-sha-X +
                                                 POST /api/v1/admin/redeploy
                                                 to staging w/ bearer token)
              │
              ▼
   stash-staging EC2  ── redeploy webhook → watchtower → pull :dev → restart
                        (event-driven, ~30-60 s after build completes;
                         optional polling fallback in deploy/README.md)
              │
              ▼
   real-world QA + visual verification on /admin feedback kanban
              │
              ▼
                  release-please.yml watching dev opens
                  ┌────────────────────────┐
                  │  chore(dev): release   │  ← merge me to ship
                  │      vX.Y.Z PR         │    (release-tests.yml
                  └────────────────────────┘     runs full Playwright
                              │ merge            on this PR specifically)
                              ▼
                  release-please cuts tag vX.Y.Z at dev tip
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
       build.yml fires on the tag      main-sync.yml fires on the tag
              │                               │
              ├─► builds :vX.Y.Z              └─► ff main to the tag
              │   + :X.Y + :X
              │
              └─► POSTs /api/v1/admin/redeploy
                  on prod, AFTER the docker push
                  (must fire post-push or watchtower
                  re-checks an unchanged digest and
                  silently no-ops — that's how v1.50.1
                  failed before this was fixed)
                              │
                              ▼
                  prod's watchtower pulls the new
                  image at whichever tag prod is
                  pinned to (typically :1 or :X.Y)
```

**One PR per release, zero manual prod cutover.**  release-please
watches ``dev``, so its Release PR opens on dev and merging it
cuts the tag directly.  ``build.yml`` builds the image and pings
prod's redeploy webhook in the same job (so the ping always
follows the push).  ``main-sync.yml`` ff's main in parallel for
the "what's released right now" view.  Prod's ``STASH_IMAGE``
pin decides how aggressive the auto-update is — ``:1`` follows
every minor + patch within 1.x, ``:1.49`` only tracks patches
within 1.49.x, ``:v1.49.0`` freezes prod entirely (webhook
becomes a no-op).

No `:latest` tag.  Staging tracks `:dev`, prod tracks the operator-
chosen pin.  The "deliberate operator action" is now merging the
Release PR — every step downstream is automation.

### Pending-release preview on staging

Staging's `:dev` images carry a synthetic "if I released right
now, this is what would ship" preview at the top of the
`/maintenance` changelog page.  Generated at build time by
`tools/pending_changelog.py`:

1. Walks conventional commits from the most recent `v*.*.*`
   tag to `HEAD`.
2. Predicts the next semver bump (any `feat!`/BREAKING → major,
   any `feat` → minor, otherwise patch).
3. Renders a markdown section that mirrors release-please's Node
   release-type config (Features / Bug Fixes / Performance
   Improvements / Reverts).  The `chore` / `ci` / `docs` / etc.
   types are deliberately hidden — same as what the actual
   `dev → main` Release PR will display.
4. Prepends to `CHANGELOG.md` inside the build context.

The image's `VERSION` env-var becomes e.g. `1.49.0-dev.b0220ad`
so the running-version display under "Version" reads the same
way.  Tag-triggered (production) builds skip this entire path —
they ship release-please's exact CHANGELOG.md + manifest version.

Net effect: a maintainer browsing staging's `/maintenance` page
sees the in-flight release notes accumulate every time a
conventional commit lands on `dev`.  The actual release PR on
`dev → main` will read identically, so there's no surprise at
release time.

### Caddyfile reload on staging/prod (out-of-band config)

Most of the deployment surface ships baked into the `stash`
container image — code changes ride the `:dev` → `:vX.Y.Z` tag
flow that watchtower handles automatically.  Two artifacts in
`deploy/` are NOT in that flow:

- `deploy/Caddyfile` is bind-mounted into the `caddy` container
  (`./Caddyfile:/etc/caddy/Caddyfile:ro`), not baked into a
  custom image.
- `deploy/docker-compose.yml` is what the operator runs on the
  host — same story.

So any change to those files needs to be pulled onto the
deployment host explicitly:

```bash
# on the staging or prod host
cd /path/to/stash
git pull
docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
# (or: docker compose up -d caddy   — recreates the container)
```

`caddy reload` is the gentler form — no dropped connections,
TLS state preserved.  If a directive is structurally new (e.g.
adding a whole `handle` block, not just tweaking a `max_size`),
`docker compose up -d caddy` is the safer call because
hot-reload is less forgiving than restart on those.

Cases this has come up:

- Bumping `/ingest`'s `request_body max_size` from 50 MB → 110 MB
  to fit phone-camera photo batches (feedback #80, 2026-05-18).
- Adding the `/robots.txt → /__stash_robots_txt` rewrite to
  work around oauth2-proxy's hardcoded handler.

If we ever start changing the Caddyfile or compose file
multiple times per release cycle, the right move is to either
(a) bake the Caddyfile into a stash-edge image so it rides the
normal `:dev` flow, or (b) add a thin GHA that SSHs to staging
and reloads on relevant path changes.  Neither is worth the
complexity today — these edits are rare.

## Workflows

| File | Trigger | What it does |
|---|---|---|
| `pr-checks.yml` | PR to `main` or `dev` (skipped for `release-please--*` head) | Fast pytest pass (`-m "not ui"`).  ~30 s.  Required check for feature PRs.  Skipped on the Release PR (release-tests.yml runs the full suite there). |
| `release-tests.yml` | PR to `dev` with head `release-please--*` | Full pytest including Playwright UI suite (mobile + desktop viewports).  ~7 min.  Required check on the Release PR specifically — that PR's merge is the release ceremony. |
| `build.yml` | push to `dev`, tag `v*.*.*` | Build + push image.  Dev pushes are gated by the fast tests (catches direct-to-dev pushes that skipped the PR).  Tag pushes skip the gate — the Release PR already ran the full Playwright suite. |
| `release-please.yml` | push to `dev` | Opens / updates the Release PR (on dev) with the assembled changelog.  Merging it creates the version tag at dev's tip. |
| `main-sync.yml` | tag `v*.*.*` | Fast-forwards main to the tagged commit.  The prod redeploy webhook moved into `build.yml` (post-push) so it can't race the image upload. |
| `pr-title.yml` | PR open / edit | Validates conventional-commit PR title (gate for release-please assembling clean changelog sections). |

### Asymmetric test gates, on purpose

The fast path to a production image looks like:

```
   feature PR → dev     pr-checks (fast tests, ~30s)
        │ merge
        ▼
   dev push           build.yml's test job (fast, gates :dev image)
        │
        ▼
   release-please Release PR
   (on dev, head ``release-please--…``)   release-tests runs ──┐
        │ merge                            full Playwright     │ gated here
        ▼                                  suite               │
   release-please tags vX.Y.Z at dev tip                       │
        │                                                      │
        ▼                                                      │
   build.yml fires on the tag    (no gate) ◄────────────────────┘
```

The Release PR is the only place the full Playwright suite
runs — that's the "are you sure you want to ship this?" gate.
Feature PRs get the fast suite via pr-checks; direct dev pushes
get the fast suite via build.yml's embedded test job.  Tag
builds skip tests entirely because the Release PR they came from
already exercised the full suite.

Net: full Playwright fires exactly once per release.  Fast tests
fire on every feature change.  No double-billing anywhere.

## Branch protection

These rules live in GitHub's repo settings, not in this file.  Set
them once via the **Settings → Branches → Branch protection rules**
UI (or via Terraform / the `repository` API).  Documented here so
the next person doesn't have to re-figure-out what we want.

### `dev`
- **Require a pull request before merging**: optional — depends on team size.  For solo / very-small teams, direct push is fine.  Add the PR requirement once there are multiple committers and "I want a second pair of eyes" matters.
- **Require status checks to pass**: ✓ when PRs are enabled
  - `pr-checks / fast-tests` (feature PRs)
  - `release-tests / full-suite` (Release PR specifically — the merge ceremony)
- **Allow auto-merge**: ✓ — release-please's Release PR can be set to auto-merge when checks pass.
- **Block force pushes**: ✓
- **Block deletions**: ✓
- **Allow** `release-please[bot]` to bypass review requirements on Release PRs.

### `main`
- Largely ceremonial under the new model.  Release decisions happen on `dev` via release-please's Release PR; `main` is kept for branch-protection anchoring + historical browsing.  Optional protections if you want main to keep mirroring the latest released code:
- **Require a pull request before merging**: ✓
- **Restrict who can push**: only admins.  `main` no longer auto-updates (nothing pushes to it).
- **Block force pushes**: ✓
- **Block deletions**: ✓

If you want `main` to track the latest release, fast-forward it manually after each tag (or add a small workflow that does it).

### Default branch
Settings → General → **Default branch** → set to `dev`.

This is what makes `git clone` land you on `dev` by default, and
what GitHub uses as the base for new PRs.  The natural workflow
is "branch from dev, PR to dev, accumulate features.  When the
rolling release-please PR on dev looks good, merge that — it
cuts the tag directly."

## Image tags

| Trigger | Tags pushed |
|---|---|
| push to `dev` | `:dev` + `:dev-sha-<short>` |
| tag `vX.Y.Z` | `:vX.Y.Z` + `:X.Y` + `:X` |
| push to `main` | _(nothing — release-please handles tagging, the tag fires the build)_ |

`:dev` is the only floating tag in the registry.  It exists so
watchtower on stash-staging has a stable name to poll.  The
matching `:dev-sha-<short>` immutable tag is published in the same
build so an operator can roll staging back to a specific commit
without a fresh build.

## Cutting a release

### Canonical: one PR, one merge

1. Land conventional commits on `dev` as you work.  Each push
   updates the rolling Release PR that release-please maintains
   on `dev` (titled `chore(dev): release X.Y.Z`).
2. When you're ready to ship, open that Release PR and read the
   assembled changelog.
3. Merge the Release PR.  `release-tests.yml` fires the full
   Playwright suite on it; once green, hit merge.
4. release-please cuts the `vX.Y.Z` tag at `dev`'s tip, which
   fires `build.yml` and publishes `:vX.Y.Z` + `:X.Y` + `:X` to
   GHCR within ~5 min.
5. On the prod box, point `STASH_IMAGE` in `deploy/.env` at the
   new floating-minor tag:
   ```
   STASH_IMAGE=ghcr.io/brennan-vanderlaan/stash:1.49
   ```
   then:
   ```
   docker compose pull stash
   docker compose up -d stash
   ```
   Watch logs for a minute, hit `/healthz`, sanity-check the app.

### Emergency: hotfix straight to dev

Same as the canonical flow — the only difference is urgency.

1. Branch from `dev` — call it `hotfix/<short-desc>`.
2. Make the fix, write a regression test, PR back to `dev`.
3. Get the PR through `pr-checks`.  Merge.
4. release-please updates the Release PR with the new fix at the
   top.  Run `release-tests` and merge it.
5. Cut over prod as in step 5 above — with `:1.49` floating-minor
   pin, the in-app maintenance button is enough; you don't even
   need to edit `.env`.

There's no longer a "branch from main" path — main isn't where
releases happen.  Hotfixes and features both flow through dev.

### Setting up the release-please PAT

By default, GHA workflows triggered by a `GITHUB_TOKEN`-driven
push are deliberately invisible to other workflows.  release-please
uses `GITHUB_TOKEN` when it creates the version tag, which means
**without a PAT, `build.yml` and `main-sync.yml` don't fire on
the tag push**.  You'd be stuck firing them manually each release
with `gh workflow run build.yml --ref v1.X.Y` and `gh workflow
run main-sync.yml --ref v1.X.Y`.

To close the loop:

1. **Generate a fine-grained PAT** at https://github.com/settings/personal-access-tokens
   - Resource owner: yourself (or the org).
   - Repository access: **Only select repositories** → `stash`.
   - Permissions:
     - **Contents**: Read and Write
     - **Pull requests**: Read and Write
   - Expiration: as long as you're comfortable with (1 year is
     fine for solo / small-team operations; rotate before it
     lapses or release-please breaks silently).
2. **Copy the `github_pat_…` value** — only shown once.
3. **Add a repo secret** at Settings → Secrets and variables →
   Actions → New repository secret:
   - Name: `RELEASE_PLEASE_TOKEN`
   - Value: the `github_pat_…` from step 2.
4. **No code change needed** — `release-please.yml` already reads
   `secrets.RELEASE_PLEASE_TOKEN || github.token`, so the PAT
   takes over automatically once it's present.

After this is wired, every Release PR merge fires the full chain:
tag → `build.yml` builds image + POSTs prod webhook (in order, so
the webhook can't race the upload) → `main-sync.yml` ff's main in
parallel.  No manual workflow_dispatch calls.

### "Cut release" in-app

(Deferred.)  Planned: a `/admin` button that, when clicked,
fetches available `:vX.Y.Z` tags from GHCR, lets the operator
pick one, writes it to a deploy-config file watchtower (or a
custom hook) picks up.  Same operator surface as the existing
free-tier-pool bumper.  Lower priority than HA + zero-downtime
deploys (the user explicitly flagged that as the next ask).

## Verification loop (feedback → fix → confirmed)

The dev pipeline above gets code to staging fast.  The
verification loop is the human-in-the-loop step that confirms
the fix actually works before it gets promoted to prod.  Tracked
on the `/admin` feedback kanban.

### The states

```
   open  ──────►  accepted  ──────►  needs_verification  ──────►  done
                                          │   ▲              ▲
                                          │   │              │
                              ✗ Still     │   │              │ auto-promoted by
                              broken      │   │              │ release-finalise GHA
                              ┌───────────┘   │              │ when fix_commit_sha
                              ▼               │              │ is in the vX.Y.Z tag's
                            open              │              │ commit range
                                              │              │
                                          ✓ Verified         │
                                          (operator on       │
                                           stash-staging)    │
                                              └──────────────┘
                                              (just sets the
                                               verified_in_staging_at
                                               stamp; status stays
                                               needs_verification
                                               until release ships)
```

Plus `rejected` for "won't fix" — terminal, no transitions out.

### The two environments + where state lives

This loop touches two stash deployments:

- **stash-prod** runs the canonical app at https://stash.swampcats.life.
  This is where users submit feedback (the floating widget POSTs
  to prod), where the feedback table lives, where the MCP server
  the AI talks to lives, and where the /admin kanban + verify /
  reopen buttons live.
- **stash-staging** runs the same app at https://stash-staging.swampcats.life
  on a separate EC2.  Tracks the `:dev` image tag — pushes to
  the dev branch land here within minutes.  Its own DB
  (sandbox), its own Stripe (test keys).  This is where the
  operator goes to **visually look at the fix**, NOT where
  feedback state is mutated.

The "magic glue" — feedback rows, fix-commit tracking,
verification stamps, the kanban — lives on prod.  Staging is the
mirror where the operator confirms with their eyes.

### How it flows

1. **User submits feedback** via the floating widget on a
   stash-prod page.  Lands in prod's feedback table as
   `status='open'`.
2. **AI reads the prod queue** via the prod MCP server's
   `admin_list_feedback` tool and triages.
3. **Operator triages** on stash-prod's `/admin` — accept /
   reject / leave open.  No code change yet.
4. **AI (or human contributor) commits a fix** on the `dev`
   branch.  The commit message carries a trailer
   ```
   Fixes-feedback: 63, 64
   ```
   so any tooling that walks the git log can find which
   feedback ids the commit addresses.
5. **AI calls the prod MCP tool**
   `admin_mark_feedback_needs_verification` once per fixed id
   with `(feedback_id, fix_commit_sha, fix_summary)`.  The
   prod-side row's status flips to `needs_verification`; prod's
   /admin kanban now shows the commit SHA + summary on the card.
6. **build.yml fires** on the dev push → image tagged `:dev` →
   watchtower on stash-staging pulls within minutes.  Prod
   is untouched.
7. **AI tells the operator** (in chat): "fix is on staging,
   please verify #63 + #64 at <staging-url-for-the-bug-path>."
8. **Operator's two-tab dance** — the only manual step:
   1. Open the bug-path on **stash-staging** in one tab.  The
      prod-side kanban card surfaces a 🧪 **staging** link
      (when `STASH_STAGING_URL` is configured) that rewrites
      the source URL's host to staging's, so this is one click,
      not a hand-typed URL.
   2. Exercise the bug.
   3. Switch back to the **stash-prod** /admin tab.
   4. On the prod-side kanban card, click one of:
      - **✓ Verified on staging** — stamps
        `verified_in_staging_at` + `verified_in_staging_by` on
        the prod row.  Status stays `needs_verification` (the
        fix isn't fully done until it ships to prod) but the
        card now reads "⏳ awaiting prod release".
      - **✗ Still broken** — bounces back to `open` with the
        `fix_commit_sha` cleared so the AI digs deeper rather
        than re-pointing at the same commit.  Optional operator
        note is appended to `operator_notes` for audit.
9. **A prod release ships** (release-please's Release PR on dev
   merges, tag fires).
   Operator pings the AI in chat: "I just shipped v1.47.0".  The
   AI:
   1. `git log <prev-tag>..v1.47.0 --pretty=%H` to collect the
      commit SHAs in the release.
   2. Calls the prod MCP tool
      `admin_list_feedback_awaiting_release(commit_shas=[...])`
      which returns rows that are eligible to promote
      (status=needs_verification + verified_in_staging_at
      stamped + fix_commit_sha in the list).
   3. Iterates: for each returned row, calls
      `admin_mark_feedback_done_on_release(feedback_id, release_tag="v1.47.0")`.
   4. Reports back to the operator: "Promoted 4 rows to done in
      v1.47.0: #63, #64, #65, #82".

   **Unverified rows are NOT promoted** — the visual gate is
   non-negotiable.  Rows in needs_verification with no ✓ stay
   put until the operator catches up; they'll be eligible for
   the next release.

   Deliberately MCP-driven and not GHA-automated: the loop's
   only been exercised in a few releases so far, and an
   AI-operator conversation is a healthier control surface
   while patterns stabilise.  Future work: when the cadence is
   well-understood, a release-finalise.yml GHA can do this same
   sequence on tag push.

### Why state lives on prod, not staging

The alternative — feedback table on staging, verify buttons on
staging — would mean the prod-facing /admin doesn't know what's
fixed, and the AI's MCP integration has to talk to two databases.
Worse, staging's DB resets on schema migrations or fresh
deployments, losing operator verification stamps.  Keeping the
feedback DB on prod means:

- One MCP integration (prod).  The AI doesn't have to know about
  staging at all.
- Verification stamps survive staging redeploys.
- The "what's in flight" view is canonical: every feedback row
  in `needs_verification` represents a real prod-side commitment
  to ship a fix.

If the AI ever needs to drive staging programmatically (seed
test data, run an MCP-driven sweep against the staging build),
we'd wire a separate `claude_ai_Stash_Staging` MCP integration —
no state changes flow back from staging to prod via that
channel, it's a one-way "look at staging" tool.

### The contract

- **AI never marks a row `done` directly.** `done` means "shipped
  to prod in a tagged release" and the only way there is through
  `needs_verification → verified_in_staging → release`.
- **Commit trailer + MCP call go together.** The trailer is for
  audit + grep; the MCP call updates the DB row.  Skip either
  and the kanban won't reflect reality.
- **`fix_commit_sha` is the join key.** It's what
  `release-finalise.yml` uses to map "commits in this release" →
  "feedback rows shipped".
- **Bounce-back-to-open clears the SHA.** A failed verification
  is not a small fix away from passing — it's "the AI was wrong
  about what was broken, start over from triage."

### Tooling

- **MCP tool**: `admin_mark_feedback_needs_verification(feedback_id,
  fix_commit_sha, fix_summary)`.  AI calls this once per fixed id
  after committing.
- **MCP tool**: `admin_list_feedback_awaiting_release(commit_shas)`.
  Returns the rows eligible to promote on release day.
- **MCP tool**: `admin_mark_feedback_done_on_release(feedback_id,
  release_tag)`.  AI calls this once per eligible row when the
  operator says "I shipped v1.X.Y".
- **MCP tool**: `admin_set_feedback_status` (existing, widened to
  accept `needs_verification`) — for emergency manual transitions.
- **Operator UI**: ✓ Verified / ✗ Still broken buttons on the
  kanban for cards in `needs_verification`.

No GitHub Actions in the verification loop today.  When the
cadence is well-exercised we can wire a `release-finalise.yml`
GHA that walks the git log on tag push and calls the same MCP
tools — but until then, an AI-operator conversation is the
sane control surface.  See the "future" notes near the end of
this document.

## Future: zero-downtime / HA

The user flagged this as the next ask after this CI/CD work:

> "I'll be following on to this with needing HA for production
> because users hate the app breaking while using it when I cut
> releases."

Not in scope for this commit, but flagging the gotchas so the
follow-up isn't surprises:

* **SQLite is single-writer**.  HA via two stash containers
  against the same DB file works only if WAL mode is on (it is —
  spec § "SQLite concurrency") AND only one container writes at a
  time.  Two writers will race on `BEGIN IMMEDIATE`.  Realistic
  posture: one writer + N readers, with the writer flipping during
  a brief cutover.
* **Upload directory is shared state**.  Two containers need to
  see the same encrypted upload blobs.  EBS multi-attach OR a
  network filesystem (EFS) is the path.
* **Encryption key (STASH_KEK) must match across replicas**.  This
  is fine — set the same env var on both.  Just don't forget.
* **Background workers** (ingest, B2 backups) need either a single-
  active-leader pattern or idempotent operations.  Today the
  ingest worker is in-process; for HA it'd want a separate worker
  container.
* **Sticky sessions** for OAuth + tour state aren't required (DB-
  backed), but the `stash_active_tenant` cookie's Domain attribute
  needs to allow both replicas (they're on the same hostname
  behind the ALB).

The cleanest near-term step is **blue/green via two compose
projects** on the same EC2: while `stash-prod-blue` is live,
roll out the new image to `stash-prod-green`, smoke-test on a
private domain, then flip Caddy's upstream.  That's the minimum
viable cutover-without-downtime path before going to true HA.
