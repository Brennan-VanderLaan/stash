# Deploy

Stash runs as a five-container compose stack designed for a single small EC2
instance:

```
                  Internet
                     │
                ┌────▼────┐
                │  caddy  │      :80 / :443 (only ports published to host)
                │ TLS+HSTS│
                └────┬────┘
                     │  frontend network
                ┌────▼────────┐
                │ oauth2-proxy│  Google sign-in (any address)
                └────┬────────┘
                     │  backend network (no internet ingress)
        ┌────────────┴───────────┐
        │                        │
  ┌─────▼─────┐            ┌─────▼──────┐
  │   stash   │◄───────────│ watchtower │   on-demand image updates
  │           │  HTTP API  │            │   triggered from /maintenance
  └───────────┘            └─────┬──────┘
                                 │  docker-control network (internal: true)
                          ┌──────▼─────────────┐
                          │ docker-socket-proxy│   narrow API surface to
                          │                    │   /var/run/docker.sock
                          └────────────────────┘
```

**Trust boundaries:**

| Container | Network(s) | Reachable from |
|---|---|---|
| caddy | frontend | internet (80/443) |
| oauth2-proxy | frontend + backend | caddy (frontend) |
| stash | backend | only oauth2-proxy + watchtower |
| watchtower | backend + docker-control | only stash |
| docker-socket-proxy | docker-control (internal) | only watchtower |

Stash has no path from the internet that doesn't go through Google sign-in,
and no host bypasses /var/run/docker.sock — watchtower talks to a tightly
scoped proxy that exposes only the Docker API endpoints it actually needs.

**Single-layer email gate (stash side).** oauth2-proxy is configured with
`OAUTH2_PROXY_EMAIL_DOMAINS: *` so it lets every signed-in Google account
through.  The actual authorisation gate is **stash's `tenant_members`
table**: anyone whose email isn't a maintainer / readonly member of some
tenant on this stash gets a friendly "you're signed in but not signed up"
page (status 403) and can't reach any tenant data.

Why we dropped the older oauth2-proxy `emails.txt` allowlist: the file gate
was a hard wall in front of stash's invite system.  An operator could
create an invite for a friend, but oauth2-proxy refused at the front door
because the friend's email wasn't in the static file — meaning every invite
required an SSH session to edit `emails.txt` + SIGHUP oauth2-proxy.  That
doesn't scale and broke the in-app invite UX.  The file-based gate is
still available (see the commented block in `docker-compose.yml`) if you
want belt-and-suspenders, but the default is the cleaner single layer.

**Letting friends in (the happy path).**

1. Sign in to /admin (or your own tenant's /usage if you're a maintainer)
   and create an invite for their Google email.  Stash gives you back a
   URL like `https://<DOMAIN>/invite/<long-token>`.
2. Send them the link.  They click it, sign in with their Google account,
   and accept — they're now a member.  No file edits, no restarts.
3. Tokens expire after 7 days; re-mint if needed.

**Revoking access.** Sessions are owned by Google + oauth2-proxy, but
authorisation lives in stash:

1. **For tenant members**: go to /usage (you, as maintainer) or /admin
   (operator), find the member in the Members list, click Remove.  Their
   next request 403s with the "no tenant" page.
2. **For API tokens** (Claude.ai connector, etc.): revoke from /usage →
   API tokens, or from /admin if you're an operator.
3. **For active cookies**: stash sessions expire at the next 1h
   oauth2-proxy refresh.  To kill all cookies immediately, rotate
   `OAUTH2_PROXY_COOKIE_SECRET` in `.env` and
   `docker compose restart oauth2-proxy`.

## What you'll need before starting

- A domain you control (e.g. `stash.example.com`).
- An EC2 instance with Docker + the compose plugin installed.
- A Google account (used to create the OAuth client your sign-in flow runs
  against — separate from the accounts that *use* the app).
- The Google email addresses for everyone you want to let in (you, your
  partner, anyone else you trust).

## 1. Point DNS at the server

Create an `A` record for your chosen hostname pointing at the EC2 instance's
public IP. Wait until `dig +short stash.example.com` returns the right IP
before continuing — Caddy needs DNS to be live to get a TLS cert.

## 2. Open the firewall

In the EC2 security group, allow inbound:

| Port | Protocol | Source | Why |
|------|----------|--------|-----|
| 80   | TCP      | 0.0.0.0/0 | ACME HTTP-01 challenge for Let's Encrypt |
| 443  | TCP+UDP  | 0.0.0.0/0 | HTTPS (UDP for HTTP/3) |
| 22   | TCP      | your IP   | SSH for the rare structural change |

Stash's port `8000` should **not** be exposed — auth lives in front of it.

## 3. Create the Google OAuth client

In the [Google Cloud Console](https://console.cloud.google.com/):

1. **Create a project** (or pick an existing one). Anything name works —
   `stash-auth` is fine.
2. **APIs & Services → OAuth consent screen**:
   - User type: **External** (required for `@gmail.com` accounts).
   - App name: whatever you want users to see on the consent page.
   - User support email + developer contact: your email.
   - Scopes: leave the defaults — oauth2-proxy only needs `email`, `profile`,
     `openid`.
   - Test users: add every Google email that will sign in. While the app is in
     "Testing" mode, only listed test users can sign in — which is exactly the
     gate you want, so there's no need to publish it.
3. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Web application**.
   - Authorized redirect URI: `https://<your-domain>/oauth2/callback`
     (the path is literal; replace only the domain).
   - Copy the **Client ID** and **Client secret** — you'll paste them into
     `.env` next.

## 4. Bootstrap the host

```bash
# Install docker + compose plugin (Amazon Linux 2023 example)
sudo dnf install -y docker
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user
# log out / back in so the group takes effect

# Drop the deploy/ directory onto the host (scp, git clone, etc.)
mkdir -p ~/stash && cd ~/stash
# copy docker-compose.yml, Caddyfile, .env.example, emails.example.txt here

# Configure
cp .env.example .env
$EDITOR .env
#   - DOMAIN, ACME_EMAIL
#   - ANTHROPIC_API_KEY, GEMINI_API_KEY
#   - OAUTH2_PROXY_CLIENT_ID / _SECRET (from step 3)
#   - OAUTH2_PROXY_COOKIE_SECRET — generate with:
#       openssl rand -base64 32 | tr -- '+/' '-_'
#   - WATCHTOWER_TOKEN — generate with:
#       openssl rand -hex 32
#   - STASH_KEK — REQUIRED. Wraps every tenant's per-tenant DEK; on-disk
#     photos are AES-256-GCM ciphertext under it.  Generate with:
#       python3 -c "import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"
#     Back the value up to a DIFFERENT bucket / vendor than the data
#     backups.  Losing the KEK = total data loss.
#   - STASH_BOOTSTRAP_MEMBER_EMAIL — your Google email.  On first
#     upgrade, the multi-tenancy migration creates a "Personal" tenant
#     with this email as sole maintainer.  Falls back to the first
#     entry of the (legacy) STASH_ALLOWED_EMAILS env var if unset.
#   - STASH_OPERATOR_EMAILS (optional) — operator accounts that can
#     hit /admin.  No automatic data access — operators still need a
#     tenant invite to see any tenant's content.

# Membership is managed entirely inside stash now (tenant_members
# table, edited via /usage + /admin's invite flow).  The historical
# oauth2-proxy ``emails.txt`` file gate is OFF by default; you only
# need it if you want the belt-and-suspenders "every email must
# pre-exist in a static file" model — see the commented block at
# the top of docker-compose.yml's oauth2-proxy environment.

# (Private images only) log in to GHCR so watchtower can pull
echo "$GHCR_PAT" | docker login ghcr.io -u brennan-vanderlaan --password-stdin

# Start everything
docker compose up -d
```

Visit `https://<your-domain>` — you should be bounced to Google sign-in, and
after sign-in see Stash.

## Production: which image tag to pin

Production pins to an **explicit version tag** — never a floating
tag.  The full SDLC (dev → staging → release → prod) is in
`CICD.md` at the repo root; the operator-side bit here is:

1. **Find the latest release** on GitHub:
   https://github.com/Brennan-VanderLaan/stash/releases
2. **Edit `.env`** on the prod box to point `STASH_IMAGE` at that
   exact tag:
   ```
   STASH_IMAGE=ghcr.io/brennan-vanderlaan/stash:v1.47.0
   ```
3. **Pull + restart** the stash container:
   ```bash
   docker compose pull stash
   docker compose up -d stash
   ```
4. Watch logs for a minute; hit `/healthz`; sanity-check the app.

There is **no `:latest` tag**.  The build pipeline deliberately
doesn't publish one — every prod cutover is an explicit operator
action so a bad commit on main can't auto-roll-out the way it
used to.

### Updates triggered from the app

The **Check for updates** button on `/admin/maintenance` calls
watchtower's HTTP API over the internal network.  Watchtower
will pull whatever tag each container's compose entry points at
(stash is pinned to a version per above; caddy + oauth2-proxy
are on their own image streams).

The browser may flash a connection error mid-update — that's the
container being recreated. Refresh after ~30s and the version on
the Maintenance page will be the new one.

## Staging

stash-staging runs on a separate EC2 instance using the **same**
`docker-compose.yml` as prod.  The difference between the two
environments lives entirely in `.env`:

| Variable | Prod | Staging |
|---|---|---|
| `STASH_IMAGE` | `:vX.Y.Z` (explicit tag, bumped per release) | `:dev` (floating, GHA webhook drives the pull) |
| `DOMAIN` | `stash.example.com` | `stash-staging.example.com` |
| Stripe keys | `sk_live_…` / live `whsec_…` | `sk_test_…` / test mode `whsec_…` |
| `STASH_KEK` | unique to prod — **do not share** | unique to staging — independent backup |
| `WATCHTOWER_PERIODIC_POLLS` | `false` (manual cutover only) | `false` primary; flip `true` only as belt-and-suspenders |
| `WATCHTOWER_POLL_INTERVAL` | `86400` (irrelevant when polls off) | `1800` if polling fallback is enabled |

Everything else (OAuth client, KEK generation procedure, Caddy,
oauth2-proxy) is identical; staging just gets its own values.

### How the auto-update works (event-driven, primary path)

```
   dev branch push  ──►  build.yml: fast tests + buildx push
                              │
                              ▼
                  GHCR :dev tag updated
                              │
                              ▼  curl POST with bearer
                              │   (continue-on-error: true)
                              │
                  staging /api/v1/admin/redeploy
                              │
                              ▼
                   background task: POST to
                   watchtower /v1/update inside
                   the staging docker network
                              │
                              ▼
                  watchtower pulls :dev,
                  recreates stash container
                              │
                              ▼
                  staging on new code, ~30-60 s
                  after build.yml's webhook step
```

End-to-end latency from `git push origin dev` to "running on
staging" is roughly: build.yml runtime (~3-5 min for the image
build + GHCR push) + the curl POST + watchtower's pull time
(~30-60 s for a small layer diff).  Typical is ~5 min total.

### Wiring the webhook (one-time setup)

1. **Mint a bearer token on staging.**  Sign in to
   `https://stash-staging.<your-domain>/usage` as a Google
   account that's in `STASH_OPERATOR_EMAILS` on the staging
   box.  Under **API tokens**, create one named e.g. `gha-redeploy`.
   Copy the `stash_…` plaintext — you'll never see it again.
2. **Add two GitHub Actions secrets** at Settings → Secrets and
   variables → Actions:
   - `STAGING_REDEPLOY_URL` = `https://stash-staging.<your-domain>/api/v1/admin/redeploy`
   - `STAGING_REDEPLOY_TOKEN` = the `stash_…` value from step 1
3. **Smoke-test from your laptop** before relying on GHA:
   ```bash
   curl -fsSL -X POST \
     -H "Authorization: Bearer stash_..." \
     https://stash-staging.<your-domain>/api/v1/admin/redeploy
   ```
   Expected: `{"ok":true,"triggered":"watchtower"}` and a
   container recreate visible in `docker compose logs -f stash`
   on the staging box within seconds.
4. **First real exercise.**  Push a no-op to `dev`; watch
   build.yml in GitHub Actions go green.  The final step
   ("Trigger staging redeploy") should print the same
   `{"ok":true,...}` response.  Then `docker compose logs -f stash`
   on the box shows the recreate.

### Polling fallback (optional)

The webhook is fire-and-forget with `continue-on-error: true` —
if GHA's network blips, the build still succeeds and the image
is in GHCR, but staging won't auto-update until the next webhook
fires.  If you want belt-and-suspenders catch-up:

```
WATCHTOWER_PERIODIC_POLLS=true
WATCHTOWER_POLL_INTERVAL=1800   # 30 min
```

A 30-minute fallback poll is long enough that the webhook is
still the dominant path (so you don't pay GHCR bandwidth on
no-op polls every 5 min) but short enough that a missed delivery
self-heals within half an hour.

### Pinning staging to a specific dev commit

The build also publishes a `:dev-sha-<short>` immutable tag in
addition to `:dev`.  If you need to freeze staging on a particular
commit (forensics, A/B testing a change with a real user before
the next dev push overwrites `:dev`), set:

```
STASH_IMAGE=ghcr.io/brennan-vanderlaan/stash:dev-sha-abc1234
WATCHTOWER_PERIODIC_POLLS=false
```

then `docker compose pull stash && docker compose up -d stash`.
Re-enable the auto-poll later by flipping `STASH_IMAGE` back to
`:dev`.  The webhook still fires on subsequent dev pushes; you
can either ignore it (compose's pinned image won't change since
the tag is immutable) or revoke the GHA token via /usage if you
want hard isolation.

### Verifying the wiring on a fresh staging box

1. `docker compose up -d` — will error fast if STASH_IMAGE is
   unset (that's deliberate; see compose comment).
2. `docker compose logs watchtower | head -20` — confirm the
   "Started Watchtower version …" line and that
   `WATCHTOWER_HTTP_API_UPDATE` is enabled.
3. `docker compose exec stash python -c "import os; print(os.environ.get('STASH_PUBLIC_URL'))"`
   — sanity-check the env reached the container.
4. Run the smoke-test curl from "Wiring the webhook" step 3 — the
   `{"ok":true,...}` reply confirms operator-auth + watchtower
   plumbing are both green.

## Rollback (prod)

If a release goes badly, point `STASH_IMAGE` at the previous
known-good version tag and re-run the pull + restart from above.
GHCR retains every released image; the major / minor floating
tags (`:1`, `:1.47`) let you pin to "latest stable in this minor"
without re-finding the exact patch number.

## Backup

Use the **Download backup** button on the Maintenance page. The zip contains
`stash.db` and every referenced upload. To restore: stop the stack, unzip
into the `stash-data` named-volume mountpoint, start the stack again.

## Adding or removing people

**In-app, no SSH needed.**  Membership lives in stash's `tenant_members`
table, edited through the UI:

| Action | Where | Who can do it |
|---|---|---|
| Invite someone | `/usage` (your tenant) or `/admin` (any tenant) | Maintainer / operator |
| Remove a member | `/usage` → Members | Maintainer |
| Revoke an API token | `/usage` → API tokens | Maintainer |
| Operator-side revoke | `/admin` → API tokens | Operator |

The invite link is sharable by email / message / printed-on-paper —
opening it while signed in accepts it.

Cookies issued before a removal stay valid until the next 1 h oauth2-proxy
refresh; to nuke all sessions immediately, rotate
`OAUTH2_PROXY_COOKIE_SECRET` in `.env` and
`docker compose restart oauth2-proxy`.

## Keeping `.env` in sync with `.env.example`

When `.env.example` gains a new variable (a new feature lands, an
existing knob becomes configurable, etc.) your `.env` falls behind.
Re-copying the example would wipe your actual values.

There's a small helper for the merge:

```bash
# From the deploy/ directory on the host:
git pull
python3 sync-env.py --dry-run    # show what's missing
python3 sync-env.py               # append missing blocks (with comments)
$EDITOR .env                       # fill in any placeholders
docker compose up -d               # restart so containers pick up new vars
```

Existing values in your `.env` are never touched.  Re-running after a
sync is a no-op.  Each appended block carries the comment block that
preceded it in the example so the context isn't lost.

## What watchtower does NOT cover

Watchtower only swaps image tags on existing containers. Anything structural
needs a one-time `git pull` + `docker compose up -d` on the host:

- adding a new service
- changing port mappings, volumes, or env-var *names*
- editing `Caddyfile`
- changing oauth2-proxy options

For a personal, low-churn stack this is fine. If structural changes start
happening often, we can add a config-sync sidecar that does a git pull +
`compose up -d` on demand.
