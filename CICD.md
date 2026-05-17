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
   └──────────────────┘                          build :dev + :dev-sha-X)
              │
              ▼
   stash-staging EC2  ── watchtower polls :dev ─→ auto-pulls + restarts
              │
              ▼
   real-world QA + manual smoke + end-to-end suite (future)
              │
              ▼
   ┌──────────────────┐
   │  dev → main PR   │  ── PR ─→  main         (pr-checks + main-tests:
   └──────────────────┘                          full pytest + UI suite)
              │ approve + merge
              ▼
   ┌──────────────────┐
   │      main        │  ── push ─→ release-please.yml
   └──────────────────┘                  │
                                         │  opens a Release PR with the
                                         │  changelog the conventional
                                         ▼  commits produce
                                ┌────────────────────────┐
                                │  chore(main): release  │  ← merge me
                                │      vX.Y.Z PR         │     to ship
                                └────────────────────────┘
                                         │ merge
                                         ▼
                              release-please cuts tag vX.Y.Z
                                         │
                                         ▼
                                  build.yml fires
                              builds :vX.Y.Z + :X.Y + :X
                                         │
                                         ▼
                       Operator on prod: pin STASH_IMAGE,
                              pull + restart
```

No `:latest` tag anywhere.  Staging tracks `:dev` (mutable, that's
fine — it's the "staging channel" not "the canonical latest
production").  Prod pins to explicit `:vX.Y.Z`, manual cutover —
the whole point of this thread is to keep prod cutover in the
loop after one too many "main merged → prod broke" surprises.

## Workflows

| File | Trigger | What it does |
|---|---|---|
| `pr-checks.yml` | PR to `main` or `dev`, push to `dev` | Fast pytest pass (`-m "not ui"`).  ~30 s.  Required check for any PR. |
| `main-tests.yml` | PR to `main` | Full pytest including Playwright UI suite (mobile + desktop viewports).  ~7 min.  Required check on `dev → main` PRs.  No `push: main` trigger — branch protection guarantees the PR gate already ran. |
| `build.yml` | push to `dev`, tag `v*.*.*` | Build + push image.  Dev pushes are gated by the fast tests as belt-and-suspenders (catches direct-to-dev pushes that skipped the PR).  Tag pushes skip the gate — the dev → main PR already ran the full suite, so re-testing here would just delay the production image. |
| `release-please.yml` | push to `main` | Opens / updates the Release PR with the assembled changelog.  Merging the Release PR creates the version tag. |
| `pr-title.yml` | PR open / edit | Validates conventional-commit PR title (gate for release-please assembling clean changelog sections). |

### Asymmetric test gates, on purpose

The fast path to a production image looks like:

```
   dev → main PR    main-tests runs full suite ──┐
        │ merge                                  │ gated here
        ▼                                        │
        main      (no tests, no build)           │
        │                                        │
        ▼                                        │
   release-please opens Release PR               │
        │ merge                                  │
        ▼                                        │
   release-please tags vX.Y.Z                    │
        │                                        │
        ▼                                        │
   build.yml fires on the tag    (no gate) ◄─────┘
```

Anything that lands on `main` is already known-good — branch
protection makes the PR gate non-negotiable.  Re-running tests on
`push: main` or on the tag push would just spend minutes
verifying what's already verified, and the *only* effect is
delaying the production image.  Speed of release is the win;
correctness is held by the PR gate, not by repetition.

The dev-push build keeps its fast-test gate because direct
pushes to `dev` are allowed (PR requirement on `dev` is optional
for solo / small teams) and we don't want a broken commit
landing on staging just because someone bypassed the PR flow.

## Branch protection

These rules live in GitHub's repo settings, not in this file.  Set
them once via the **Settings → Branches → Branch protection rules**
UI (or via Terraform / the `repository` API).  Documented here so
the next person doesn't have to re-figure-out what we want.

### `main`
- **Require a pull request before merging**: ✓
- **Require approvals**: 1 (lower bound; bump as the team grows)
- **Dismiss stale approvals on new commit**: ✓
- **Require status checks to pass before merging**: ✓
  - `pr-checks / fast-tests`
  - `main-tests / full-suite`
  - `pr-title / lint`
- **Require branches to be up to date before merging**: ✓
- **Restrict who can push to matching branches**: ✓
  - Allow: repo admins (for emergency hotfixes) + `release-please[bot]` (for the Release PR merge that creates the tag)
- **Do not allow bypassing the above settings**: ✓
- **Block force pushes**: ✓
- **Block deletions**: ✓

### `dev`
- **Require a pull request before merging**: optional — depends on team size.  For solo / very-small teams, direct push is fine.  Add the PR requirement once there are multiple committers and "I want a second pair of eyes" matters.
- **Require status checks to pass**: ✓ when PRs are enabled
  - `pr-checks / fast-tests`
- **Block force pushes**: ✓
- **Block deletions**: ✓

### Default branch
Settings → General → **Default branch** → set to `dev`.

This is what makes `git clone` land you on `dev` by default, and
what GitHub uses as the base for new PRs.  Combined with the
protections on `main`, the natural workflow is "branch from dev,
PR to dev, accumulate features, cut a release when ready by
PR'ing dev → main".

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

The hands-on bit.  Two paths — the canonical one through GitHub
and the emergency one over SSH.

### Canonical: through GitHub

1. Open a PR `dev → main`.  GitHub auto-runs `pr-checks` +
   `main-tests`.  Wait for green.
2. Get approval (or self-approve if you're flying solo + the
   branch protection allows).
3. Merge the PR.
4. release-please.yml runs on the `main` push.  Within a minute or
   two, it'll have opened (or updated) a PR titled
   `chore(main): release X.Y.Z`.  The body of that PR is the
   assembled changelog — read it, sanity-check it.
5. Merge the Release PR.  release-please tags `vX.Y.Z`, the tag
   fires `build.yml`, and the image lands in GHCR within ~5 min.
6. On the prod box, point `STASH_IMAGE` in `deploy/.env` at the
   new tag:
   ```
   STASH_IMAGE=ghcr.io/brennan-vanderlaan/stash:v1.47.0
   ```
   then:
   ```
   docker compose pull stash
   docker compose up -d stash
   ```
   Watch logs for a minute, hit `/healthz`, sanity-check the app.

### Emergency: hotfix from main

If prod is broken and dev has unrelated work-in-progress:

1. Branch from `main` (not `dev`) — call it `hotfix/<short-desc>`.
2. Make the fix, write a regression test, PR back to `main`
   directly.
3. Get the PR through `main-tests` green.
4. Merge.  release-please will produce a Release PR for the patch
   bump (e.g. `v1.47.1`).
5. Merge the Release PR.  Cut over prod as above.
6. **Back-port to dev**: open a PR `main → dev` to keep the dev
   branch from drifting behind.  Otherwise the next regular
   release loses the hotfix.

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
9. **A prod release ships** (dev → main → release-please tag).
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
