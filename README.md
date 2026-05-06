# Stash

[![build](https://github.com/Brennan-VanderLaan/stash/actions/workflows/build.yml/badge.svg?branch=main)](https://github.com/Brennan-VanderLaan/stash/actions/workflows/build.yml)
[![release-please](https://github.com/Brennan-VanderLaan/stash/actions/workflows/release-please.yml/badge.svg?branch=main)](https://github.com/Brennan-VanderLaan/stash/actions/workflows/release-please.yml)
[![release](https://img.shields.io/github/v/release/Brennan-VanderLaan/stash?label=release&sort=semver)](https://github.com/Brennan-VanderLaan/stash/releases)
[![image](https://img.shields.io/badge/ghcr.io-stash-blue?logo=docker&logoColor=white)](https://github.com/Brennan-VanderLaan/stash/pkgs/container/stash)

Tiny webapp for organizing stuff into boxes with photos. Designed to live on
a small EC2 instance behind oauth2-proxy + Caddy, scoped to a couple of
trusted Google accounts. Optimized for one-handed mobile use — print labels,
scan a box's QR code, see what's inside.

## Container Images

Built and published to GHCR by [`build.yml`](.github/workflows/build.yml) on
every push to `main` and on every release tag:

```bash
docker pull ghcr.io/brennan-vanderlaan/stash:latest        # follows main
docker pull ghcr.io/brennan-vanderlaan/stash:<X.Y>         # follows minor (e.g. 0.3)
docker pull ghcr.io/brennan-vanderlaan/stash:<X.Y.Z>       # exact release
```

The release badge above shows the latest version.
[All published tags →](https://github.com/Brennan-VanderLaan/stash/pkgs/container/stash)

## Production deploy

See [`deploy/README.md`](deploy/README.md) for the full bootstrap: Caddy +
oauth2-proxy + watchtower compose stack, Google OAuth client setup, EC2
firewall + DNS, and the email allowlist of who can sign in.

## Updating a deployed instance

Click **Check for updates** on the Maintenance page in the running app —
Watchtower pulls the latest image and restarts the container. To pin to a
specific version, edit `STASH_IMAGE` in your `deploy/.env` and run
`docker compose pull && docker compose up -d`.

## Local development

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Then on your phone, open `http://<host-lan-ip>:8000`. Data lives in
`stash.db` (SQLite) and `uploads/` alongside `app.py`.

Run the test suite with `./venv/bin/pytest`.

## Releases

Releases are managed by
[release-please](https://github.com/googleapis/release-please) — every push
to `main` updates a "release PR" that bundles unreleased changes and bumps
the version. Merging that PR cuts a tagged release.

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/);
`feat:` and `fix:` are surfaced in the changelog, `chore:` / `ci:` / `docs:`
are hidden. The PR-title check enforces this on incoming PRs.

See [`CHANGELOG.md`](CHANGELOG.md) for the human-readable history.
