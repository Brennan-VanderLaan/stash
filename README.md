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

[`deploy/`](deploy/) ships a complete `docker-compose` stack —
Caddy + oauth2-proxy + watchtower in front of stash, Google OAuth
sign-in, an emails allowlist as the first gate, the per-tenant
member surface as the second. Required env vars (`STASH_KEK`,
`OAUTH2_PROXY_*`, `B2_*` for off-site backups) are documented in
[`deploy/.env.example`](deploy/.env.example) with generation
commands inline.

Container images are built on every push to `main`:

```bash
docker pull ghcr.io/brennan-vanderlaan/stash:latest        # follows main
docker pull ghcr.io/brennan-vanderlaan/stash:<X.Y>         # follows minor
docker pull ghcr.io/brennan-vanderlaan/stash:<X.Y.Z>       # exact release
```

Updating a deployed instance: click **Check for updates** on the
Maintenance page; Watchtower pulls the latest image and restarts.
Pin to a specific version by editing `STASH_IMAGE` in your
`deploy/.env`.

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
