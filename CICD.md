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
| `main-tests.yml` | PR to `main`, push to `main` | Full pytest including Playwright UI suite (mobile + desktop viewports).  ~7 min.  Required check on `dev → main` PRs. |
| `build.yml` | push to `dev`, tag `v*.*.*` | Fast tests as a gate, then build + push image.  `:dev` + `:dev-sha-X` for dev pushes; `:vX.Y.Z` + `:X.Y` + `:X` for version tags. |
| `release-please.yml` | push to `main` | Opens / updates the Release PR with the assembled changelog.  Merging the Release PR creates the version tag. |
| `pr-title.yml` | PR open / edit | Validates conventional-commit PR title (gate for release-please assembling clean changelog sections). |

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
