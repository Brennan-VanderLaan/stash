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
                │ oauth2-proxy│  Google sign-in + emails.txt allowlist
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

**Two-layer email allowlist.** The first gate is oauth2-proxy's `emails.txt`.
The second is `STASH_ALLOWED_EMAILS` inside stash itself, which checks the
`X-Forwarded-Email` header on every request. Both must allow the user.  This
is deliberate: a missing/misnamed `emails.txt` on the host turns the bind
mount into an empty directory, oauth2-proxy loads zero entries, and the
`--email-domain "*"` fallback would otherwise let every Google account in.
With the second layer, stash refuses to boot unless `STASH_ALLOWED_EMAILS`
(or the explicit `FULLY_PUBLIC=true` opt-out) is configured.

**Revoking access.** Sessions are owned by oauth2-proxy / Google, not stash.
To pull access for someone:

1. Remove their email from `emails.txt` *and* `STASH_ALLOWED_EMAILS` in
   `.env`.
2. `docker compose restart oauth2-proxy stash` so both layers reload.
3. To kill any active cookies immediately, also rotate
   `OAUTH2_PROXY_COOKIE_SECRET` in `.env` and restart oauth2-proxy.
   Otherwise, sessions die at the next 1h refresh (configured via
   `OAUTH2_PROXY_COOKIE_REFRESH`).

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

# Allowlist the Google emails that can sign in. Both files matter:
#   * emails.txt        — read by oauth2-proxy (one address per line)
#   * STASH_ALLOWED_EMAILS in .env — read by stash itself (comma-separated)
# Stash refuses to start if STASH_ALLOWED_EMAILS is empty (set FULLY_PUBLIC=true
# only if you knowingly want to disable the app-level gate).
cp emails.example.txt emails.txt
$EDITOR emails.txt
#   one email per line — anyone not listed gets a "not authorized" page

# (Private images only) log in to GHCR so watchtower can pull
echo "$GHCR_PAT" | docker login ghcr.io -u brennan-vanderlaan --password-stdin

# Start everything
docker compose up -d
```

Visit `https://<your-domain>` — you should be bounced to Google sign-in, and
after sign-in see Stash.

## Updates

Click **Check for updates** on the Maintenance page. The app calls
watchtower's HTTP API over the internal network; watchtower pulls newer
images for any container labeled
`com.centurylinklabs.watchtower.enable=true` (stash, caddy, and oauth2-proxy
in this stack), then stops and recreates them.

The browser may flash a connection error mid-update — that's the app being
recreated. Refresh after ~30s and the version on the Maintenance page will be
the new one.

## Rollback

Edit `.env` to pin `STASH_IMAGE` to a specific tag (e.g.
`ghcr.io/brennan-vanderlaan/stash:v0.2.1`) and run:

```bash
docker compose pull && docker compose up -d
```

## Backup

Use the **Download backup** button on the Maintenance page. The zip contains
`stash.db` and every referenced upload. To restore: stop the stack, unzip
into the `stash-data` named-volume mountpoint, start the stack again.

## Adding or removing people

Edit **both** allowlists on the host (the proxy and the app each enforce
their own copy):

1. `emails.txt` — one Google email per line.
2. `STASH_ALLOWED_EMAILS` in `.env` — comma-separated.

Then restart both containers so they pick up the new lists:

```bash
docker compose restart oauth2-proxy stash
```

To kill any sessions issued before the change, also rotate
`OAUTH2_PROXY_COOKIE_SECRET` in `.env` (otherwise the existing cookies stay
valid until the next 1h refresh re-checks the lists).

## What watchtower does NOT cover

Watchtower only swaps image tags on existing containers. Anything structural
needs a one-time `git pull` + `docker compose up -d` on the host:

- adding a new service
- changing port mappings, volumes, or env-var *names*
- editing `Caddyfile` or `emails.txt`
- changing oauth2-proxy options

For a personal, low-churn stack this is fine. If structural changes start
happening often, we can add a config-sync sidecar that does a git pull +
`compose up -d` on demand.
