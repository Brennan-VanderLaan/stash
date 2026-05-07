# Changelog

## [0.16.0](https://github.com/Brennan-VanderLaan/stash/compare/v0.15.0...v0.16.0) (2026-05-07)


### Features

* drag boxes between rooms on the floorplan + sort form fix ([78322d5](https://github.com/Brennan-VanderLaan/stash/commit/78322d58615ef27ada03eb0d09a07486657e4126))
* item DnD + zoom-12x + responsive modals + full-width floorplan ([24db91c](https://github.com/Brennan-VanderLaan/stash/commit/24db91c2ba771f450c23e5fac93a4676948e0076))

## [0.15.0](https://github.com/Brennan-VanderLaan/stash/compare/v0.14.0...v0.15.0) (2026-05-07)


### Features

* tile photo mosaic at deep zoom + global modal centering ([7e62ca9](https://github.com/Brennan-VanderLaan/stash/commit/7e62ca94abdd67d6a9392d8742c5e5bfe09689cb))

## [0.14.0](https://github.com/Brennan-VanderLaan/stash/compare/v0.13.0...v0.14.0) (2026-05-06)


### Features

* floorplan navigation rework + box colors + zoom-tier detail ([48a6c4a](https://github.com/Brennan-VanderLaan/stash/commit/48a6c4a84da0d5c97cf3f808216d387aa0b67623))

## [0.13.0](https://github.com/Brennan-VanderLaan/stash/compare/v0.12.1...v0.13.0) (2026-05-06)


### Features

* floorplan game-UI — pan + zoom + boxes-as-tiles + tap preview ([54130fc](https://github.com/Brennan-VanderLaan/stash/commit/54130fc385b78de9248254f772bc4ab6437c8da7))
* room color picker in the edit modal ([b2e2066](https://github.com/Brennan-VanderLaan/stash/commit/b2e2066fdba916ecd54fa69ada338132555903bb))
* search results open in a modal instead of navigating away ([352f1a2](https://github.com/Brennan-VanderLaan/stash/commit/352f1a29eaa9a63c0050da4cde1725f66094aed4))

## [0.12.1](https://github.com/Brennan-VanderLaan/stash/compare/v0.12.0...v0.12.1) (2026-05-06)


### Bug Fixes

* search dropdown grouping + cascade, mobile box card polish ([dc5c52c](https://github.com/Brennan-VanderLaan/stash/commit/dc5c52cdbc9afe50327d8adc0d8847e6d4fd5e84))

## [0.12.0](https://github.com/Brennan-VanderLaan/stash/compare/v0.11.0...v0.12.0) (2026-05-06)


### Features

* Cricut-ready multi-page PDF export for labels ([53619e4](https://github.com/Brennan-VanderLaan/stash/commit/53619e44eeb88cf846af8543e6ad74c45c8b761d))


### Bug Fixes

* ingest mid-upload reload, dropdown popup contrast ([4090e0f](https://github.com/Brennan-VanderLaan/stash/commit/4090e0f3ed2d2d0cc14bc24bcd101a62ef04400a))

## [0.11.0](https://github.com/Brennan-VanderLaan/stash/compare/v0.10.0...v0.11.0) (2026-05-06)


### Features

* faceted search redesign + link color + room view parity ([2b7d535](https://github.com/Brennan-VanderLaan/stash/commit/2b7d535d0bc1f2802cb6d6c2442f8d8e7f91e3c8))

## [0.10.0](https://github.com/Brennan-VanderLaan/stash/compare/v0.9.2...v0.10.0) (2026-05-06)


### Features

* full-surface UX pass — wider canvas, brand turtle, mascot empties ([d605fda](https://github.com/Brennan-VanderLaan/stash/commit/d605fda3c1f3cb0c526985ac160070984e1a1422))

## [0.9.2](https://github.com/Brennan-VanderLaan/stash/compare/v0.9.1...v0.9.2) (2026-05-06)


### Bug Fixes

* sort queue UX, mobile nav, new-box room picker, grouped box dropdown ([cbe4f99](https://github.com/Brennan-VanderLaan/stash/commit/cbe4f99add025606b9b7914c6638638fb08961df))

## [0.9.1](https://github.com/Brennan-VanderLaan/stash/compare/v0.9.0...v0.9.1) (2026-05-06)


### Bug Fixes

* stop OOM-killing the container on first thumb load ([4ae35df](https://github.com/Brennan-VanderLaan/stash/commit/4ae35dfe922263dc668524f893caa0e121c83607))

## [0.9.0](https://github.com/Brennan-VanderLaan/stash/compare/v0.8.0...v0.9.0) (2026-05-06)


### Features

* locations, rooms, and an interactive floorplan editor ([1b02349](https://github.com/Brennan-VanderLaan/stash/commit/1b02349692279705d0bc7985d4a4f52d24bac0b6))
* modal preview, parallel-safe AJAX art, item-context, brighter print ([1660051](https://github.com/Brennan-VanderLaan/stash/commit/1660051e2b1aacf212f47d0b333b14524496be35))
* multi-floor locations + drag-to-move/resize rooms ([b07871b](https://github.com/Brennan-VanderLaan/stash/commit/b07871b961003f223dd0f09b0ea165e75a911d48))
* serve downscaled thumbs from /thumbs/{name} for grid + list views ([6a3dc7b](https://github.com/Brennan-VanderLaan/stash/commit/6a3dc7b4791ad4b474df2bbb5c7ee262e7a559d7))


### Bug Fixes

* actually take advantage of docker layer cache on every commit ([f09ca7d](https://github.com/Brennan-VanderLaan/stash/commit/f09ca7d2b62b0ad170e78178dfd1d6a6da4e0c27))
* lowercase image name for GHCR cache refs ([fbacf3b](https://github.com/Brennan-VanderLaan/stash/commit/fbacf3b8cf768083640a13b7ee764c1f7815e870))

## [0.8.0](https://github.com/Brennan-VanderLaan/stash/compare/v0.7.0...v0.8.0) (2026-05-06)


### Features

* labels page overhaul + multi-page printing + Nano Banana 2 art ([b4ca9f1](https://github.com/Brennan-VanderLaan/stash/commit/b4ca9f17e141f9b289289df79406f2c2455b762e))
* pencil + watercolor style for label art, clearer regen button ([6783808](https://github.com/Brennan-VanderLaan/stash/commit/67838080fe8199f23e496af17140c3ca52d273df))


### Bug Fixes

* make Caddy's per-path body caps mutually exclusive ([a280518](https://github.com/Brennan-VanderLaan/stash/commit/a2805183fde829c4909fb4d15187f97dad378c3f))
* simpler Caddyfile body-size scoping for /maintenance/import ([b9a2157](https://github.com/Brennan-VanderLaan/stash/commit/b9a21577452e2883cc467081c2cb926fe3545934))

## [0.7.0](https://github.com/Brennan-VanderLaan/stash/compare/v0.6.0...v0.7.0) (2026-05-06)


### Features

* live status + auto-reload on the update flow ([897fe1c](https://github.com/Brennan-VanderLaan/stash/commit/897fe1c8329749e0d929803c3a6e67a53e96548c))


### Bug Fixes

* allow multi-GB backup imports end-to-end ([410b605](https://github.com/Brennan-VanderLaan/stash/commit/410b605f46ea1b8fa215d2e5d5a8bb6195fcb7f9))

## [0.6.0](https://github.com/Brennan-VanderLaan/stash/compare/v0.5.0...v0.6.0) (2026-05-06)


### Features

* lighter, more readable maintenance page + changelog styling ([ce55147](https://github.com/Brennan-VanderLaan/stash/commit/ce55147d9eff935b6546212309f311a976968d54))


### Bug Fixes

* pin DOCKER_API_VERSION on watchtower so the daemon stops rejecting it ([c50d495](https://github.com/Brennan-VanderLaan/stash/commit/c50d49529b5d1e35cbc342c4e76b5eaee7112854))

## [0.5.0](https://github.com/Brennan-VanderLaan/stash/compare/v0.4.0...v0.5.0) (2026-05-06)


### Features

* import a .db file or backup zip from the maintenance page ([f065ac1](https://github.com/Brennan-VanderLaan/stash/commit/f065ac1a938cac18d2256f11f63972da29223ecd))


### Bug Fixes

* allow PING + VERSION on docker-socket-proxy so watchtower can update ([9ca66f2](https://github.com/Brennan-VanderLaan/stash/commit/9ca66f23788a25d1448f9cec7be1bf067bf0ed7c))

## [0.4.0](https://github.com/Brennan-VanderLaan/stash/compare/v0.3.0...v0.4.0) (2026-05-06)


### Features

* enrich release notes with container image + deploy info ([b9d1a68](https://github.com/Brennan-VanderLaan/stash/commit/b9d1a6826bee1e703d7a6cf937025c63cc4adc9d))
* QR labels point at the live box URL and show box ID + description ([a7c3114](https://github.com/Brennan-VanderLaan/stash/commit/a7c3114fba4f3f118fd5f9eaa46ca7dfa5aaaf06))

## [0.3.0](https://github.com/Brennan-VanderLaan/stash/compare/v0.2.0...v0.3.0) (2026-05-06)


### Features

* **deploy:** add HTTPS security headers at the caddy edge ([3427fc2](https://github.com/Brennan-VanderLaan/stash/commit/3427fc2fc677b309f89b1ce89072d73736f7486b))
* **deploy:** explicit cookie + session hardening on oauth2-proxy ([1dfae8f](https://github.com/Brennan-VanderLaan/stash/commit/1dfae8fb1ffbabe82412b905fb2cc6333b959c98))
* **deploy:** isolate watchtower from /var/run/docker.sock via tecnativa proxy ([a7acae6](https://github.com/Brennan-VanderLaan/stash/commit/a7acae69c46fd124109fad87078cc7002d97780f))
* **deploy:** segment compose networks so stash is unreachable except via oauth2-proxy ([a7bc475](https://github.com/Brennan-VanderLaan/stash/commit/a7bc475b8ce41f075cbada2776b99b90b99cedc8))


### Bug Fixes

* harden upload pipeline against XSS, traversal, and decompression bombs ([a553547](https://github.com/Brennan-VanderLaan/stash/commit/a55354782b581c21ba193c69ae5c534918c6bbb0))
* make release-please manifest the single source of truth for version ([b5782a4](https://github.com/Brennan-VanderLaan/stash/commit/b5782a4accf3197c8f2e3df180fedf5d56a88c3c))
* sync VERSION on release-please bumps ([65113d3](https://github.com/Brennan-VanderLaan/stash/commit/65113d3984989ffbfbc71f5b4ff31f7a117f8e0f))

## [0.2.0](https://github.com/Brennan-VanderLaan/stash/compare/v0.1.0...v0.2.0) (2026-05-06)


### Features

* production docker-compose stack with caddy, oauth2-proxy, and watchtower ([b2ddc3d](https://github.com/Brennan-VanderLaan/stash/commit/b2ddc3d0c7203184291edfb61de991cddeef80a3))
* show running version and trigger updates from the maintenance page ([3db7072](https://github.com/Brennan-VanderLaan/stash/commit/3db707222b312a71dbac92bea1c76dfe20449188))

## Changelog

Release notes are generated by release-please from Conventional Commit messages on `main`.
This file will be populated automatically on the first release.
