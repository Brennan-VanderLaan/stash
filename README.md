# Stash

[![build](https://github.com/Brennan-VanderLaan/stash/actions/workflows/build.yml/badge.svg?branch=main)](https://github.com/Brennan-VanderLaan/stash/actions/workflows/build.yml)
[![release-please](https://github.com/Brennan-VanderLaan/stash/actions/workflows/release-please.yml/badge.svg?branch=main)](https://github.com/Brennan-VanderLaan/stash/actions/workflows/release-please.yml)
[![release](https://img.shields.io/github/v/release/Brennan-VanderLaan/stash?label=release&sort=semver)](https://github.com/Brennan-VanderLaan/stash/releases)
[![image](https://img.shields.io/badge/ghcr.io-stash-blue?logo=docker&logoColor=white)](https://github.com/Brennan-VanderLaan/stash/pkgs/container/stash)

**Know where your shit is.**

Stash is an inventory app for the boxes you live with — moving, attic,
storage unit, garage. Photo each item, label each box, and let an
AI agent answer *"where is my blue mug?"* without you opening anything.

— Hosted SaaS: **<https://stash.swampcats.life>**
— Self-host: **[`deploy/`](deploy/)**

---

## Software that doesn't hate you

> *"None of this platform is about data mining or taking advantage of
> end users. It's about being able to live your life a bit better
> and know where your shit is."*

The full posture is in [`spec.md § Ethos`](spec.md). The short version:

- **Your bill is itemised down to the line.** Five lines: direct
  vendor passthrough, community backups, community free-tier,
  operator payout, margin. The five always sum to what you pay.
  The same numbers feed the public `/about/pricing` page in
  aggregate — so the public claim and the bill can never disagree.
- **The free tier is genuinely usable**, not a 7-day teaser.
- **Operators can't read your data.** Photos + thumbnails are
  encrypted on disk with a per-tenant key. There's no "view as
  user" admin mode. If a human at the platform needs to see your
  stash, they ask you to invite them — same path a partner would
  take.
- **Soft-delete by default.** Deleting a tenant kicks off a grace
  period; one backup is retained on B2 even after that.

## What you get

- **Photo-first inventory.** Snap a photo of an open box; AI
  vision (Gemini) detects each item, suggests a name, crops a
  thumbnail. You confirm + tag. Print labels with QR codes that
  point at the box's detail page.
- **Floorplans.** Draw rooms on a floorplan, drag boxes onto
  rooms, and the index page becomes a mosaic of where everything
  actually lives.
- **Search across the whole stash** by free-text, tag, or room.
  Missing items mark with one tap.
- **Share a box or item with a friend** by email, scoped down to
  read-only or maintainer. They see only what you shared, never
  the rest.
- **Pack with company.** Invite a partner / sister / movers crew
  to your tenant; everyone sees the same boxes, every change
  audit-logs to a row.

## For agents (MCP / API)

Stash speaks the [Model Context Protocol](https://modelcontextprotocol.io)
(spec rev 2025-11-25) at `/mcp` with full OAuth 2.1 + Dynamic Client
Registration. Drop the URL into Claude Desktop, Claude Code, or
claude.ai's web custom-connector dialog and an agent can search,
move, and read photos out of your stash in plain language. The full
tool catalogue lives in [`spec.md § Agent / MCP integration`](spec.md).

A bearer-token REST API at `/api/v1` is available for scripted
clients that don't speak MCP. Mint tokens from `/usage`.

## Run it yourself

Stash is fully self-hostable — [`deploy/`](deploy/) ships a complete
`docker-compose` stack with Caddy + oauth2-proxy + watchtower in
front of stash, Google OAuth sign-in, the per-tenant member
surface, and the `OAUTH2_PROXY_SKIP_AUTH_ROUTES` plumbing that
lets the OAuth + bearer endpoints reach stash. Before you commit
to it, a realistic checklist of what running your own instance
involves:

**Infrastructure (you provide)**

- A host that's online 24/7. A small VPS handles a household-size
  stash easily — DigitalOcean droplet, Hetzner CX11, Linode shared,
  EC2 t4g.small. Budget ~$5–10/month at the low end.
- A domain you control (~$12/year) with DNS pointed at the host.
  SSL/TLS is automatic via Caddy + Let's Encrypt once DNS resolves
  — no cert wrangling — but you do need the domain.
- Baseline ops chops: SSH, Docker Compose, knowing how to restart
  things when watchtower can't auto-update for some reason.

**Third-party accounts (free to register, pay-per-use)**

- A **Google Cloud project** with an OAuth 2.0 client (web
  application). Free, requires walking through the consent-screen
  setup. Credentials feed `OAUTH2_PROXY_CLIENT_ID` / `_SECRET`.
- An **Anthropic API key** — powers the queue's "suggest a box"
  and any AI-flavoured agent calls on the MCP side. Pay-per-call.
- A **Gemini API key** — drives photo ingest (item detection) and
  label-background-art generation. Pay-per-call (free tier exists
  but small).
- A **Backblaze B2 account** with a bucket and application key —
  for the nightly off-site backup target. ~$6/TB/month storage,
  effectively free at household scale.
- For genuine disaster-recovery posture, a **second cloud account
  or different vendor** for the `STASH_KEK` value. Spec is
  explicit (and the deploy docs reinforce): the KEK lives in a
  *different* bucket — and ideally a different vendor — than the
  data backup. Co-locating them defeats the encryption-at-rest
  separation.

**Operational tax** (your weekend, then a few hours a quarter)

- Standing up the Google OAuth client, generating + safely storing
  the `STASH_KEK`, configuring the email allowlist.
- Monitoring container health, log volume, backup success.
- Staying current with security patches, image rebuilds, the
  occasional schema migration.

If that sounds reasonable, [`deploy/.env.example`](deploy/.env.example)
walks you through every variable with copy-pasteable generation
commands. Container images publish on every push to `main`:

```bash
docker pull ghcr.io/brennan-vanderlaan/stash:latest        # follows main
docker pull ghcr.io/brennan-vanderlaan/stash:<X.Y>         # follows minor
docker pull ghcr.io/brennan-vanderlaan/stash:<X.Y.Z>       # exact release
```

Updates are pull-and-restart: click **Check for updates** on the
Maintenance page and Watchtower handles the rest. Pin to a
specific version by editing `STASH_IMAGE` in `deploy/.env`.

**Or skip all of the above.** That's exactly what
**[stash.swampcats.life](https://stash.swampcats.life)** is — a
hosted instance that rolls the host, the domain, the OAuth client,
the AI keys, the Backblaze account, the KEK separation, and the
ongoing patching into one signup. You sign in, you have a stash.
Pricing is itemised the way the ethos demands — vendor
passthrough, community subsidies, operator payout, small margin,
summed equals your bill — so you know exactly what your dollars
cover. Self-host costs the same things in aggregate; the SaaS
just amortises them across many tenants and removes the
weekend-of-yak-shaving.

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate          # .venv\Scripts\activate on Windows
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Then on your phone, open `http://<host-lan-ip>:8000`. Data lives in
`stash.db` (SQLite WAL mode) and `uploads/` alongside `app.py`.

```bash
.venv/bin/pytest                   # 438 tests, ~2 minutes
```

Architecture and design lives in [`spec.md`](spec.md). The status
preface at the top of that file tracks shipped vs deferred phases.

## Releases

[release-please](https://github.com/googleapis/release-please)
manages a "release PR" that bundles unreleased changes. Merging it
cuts a tagged release. Commit messages follow
[Conventional Commits](https://www.conventionalcommits.org/);
`feat:` and `fix:` are surfaced in the changelog. See
[`CHANGELOG.md`](CHANGELOG.md) for the human-readable history.
