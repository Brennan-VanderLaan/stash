# Changelog

## [1.25.1](https://github.com/Brennan-VanderLaan/stash/compare/v1.25.0...v1.25.1) (2026-05-15)


### Bug Fixes

* **theme:** visited-link rule outranked class colours; CTA text went invisible ([74cc163](https://github.com/Brennan-VanderLaan/stash/commit/74cc1633744bdc32e14c8c70c93953e2a9da2fb8))

## [1.25.0](https://github.com/Brennan-VanderLaan/stash/compare/v1.24.0...v1.25.0) (2026-05-15)


### Features

* **public:** public marketing landing at /, dashboard at /home, theme picker ([032d958](https://github.com/Brennan-VanderLaan/stash/commit/032d9588e242cad3d6458110cbac219c28053093))
* **security:** defense-in-depth response headers ([daadbcc](https://github.com/Brennan-VanderLaan/stash/commit/daadbcc7bd692a8964ed72f2405e243df76782de))


### Bug Fixes

* **about:** drop the "priority support" + business-day SLA promises ([a293a65](https://github.com/Brennan-VanderLaan/stash/commit/a293a65be1a2b3ffb8774114c743a382d19f698d))
* **security:** plug open redirects, run token-leak guard before path bypass ([feae5e7](https://github.com/Brennan-VanderLaan/stash/commit/feae5e7d73917cfb92b609a602d9b35d24084107))

## [1.24.0](https://github.com/Brennan-VanderLaan/stash/compare/v1.23.0...v1.24.0) (2026-05-15)


### Features

* **billing:** /about/transparency + Stripe automatic_tax on checkout ([9d0541d](https://github.com/Brennan-VanderLaan/stash/commit/9d0541d8eebf4af911911673d2f5d24aba7c6d90))
* **billing:** re-price to $4/mo + slash quotas + AI-art sub-budget ([ec68210](https://github.com/Brennan-VanderLaan/stash/commit/ec682109d48b0473618c3e57b5cb65415e88694a))
* **public:** static /about pages for Stripe KYC compliance ([69f38d6](https://github.com/Brennan-VanderLaan/stash/commit/69f38d6906bae8565b3413d1a35f0f0ebb747ebd))


### Bug Fixes

* **about:** cost ledger had wrong provider for bandwidth ([d21e405](https://github.com/Brennan-VanderLaan/stash/commit/d21e405a2dd917655eb48898c7bb69888dc08e73))
* **about:** rewrite terms to standard SaaS boilerplate ([419fcb5](https://github.com/Brennan-VanderLaan/stash/commit/419fcb58c87697a22de14a5d1ac74cc53816b97b))
* **admin:** compact hero moves to a right rail, hides on mobile ([01d04b4](https://github.com/Brennan-VanderLaan/stash/commit/01d04b478889a8c3118c57fe78f20283616cf14f))
* **floors:** make drawing tools sticky so you can rect-rect-rect ([6d721c8](https://github.com/Brennan-VanderLaan/stash/commit/6d721c8d488e1f6b575c94c7eb249cb4f0169bd8))
* **public:** bypass auth on /static so public /about pages render styled ([53559fc](https://github.com/Brennan-VanderLaan/stash/commit/53559fcb6899c4cf60a5204ec6f5b298f8a4cb6b))

## [1.23.0](https://github.com/Brennan-VanderLaan/stash/compare/v1.22.0...v1.23.0) (2026-05-15)


### Features

* **admin:** Kanban feedback queue with filter bar ([8ae2d6b](https://github.com/Brennan-VanderLaan/stash/commit/8ae2d6b88e22edd01cd049018525b3837688693b))


### Bug Fixes

* **floors:** unstick drag-draw, add eraser, allow blank-canvas editor ([dc916f1](https://github.com/Brennan-VanderLaan/stash/commit/dc916f143f7357f914e951d0a0966647cf91957c))
* **tours:** mobile-aware target selectors + visibility filter ([1efe0e2](https://github.com/Brennan-VanderLaan/stash/commit/1efe0e233bfa1c54c6f46c32c840036d46f56d8c))

## [1.22.0](https://github.com/Brennan-VanderLaan/stash/compare/v1.21.2...v1.22.0) (2026-05-15)


### Features

* **floors:** in-browser floorplan editor with Fabric.js ([595da75](https://github.com/Brennan-VanderLaan/stash/commit/595da7521b4ad42baa1d21964afc46e08a47c51b))


### Bug Fixes

* **tours:** kill spotlight flicker + scroll snap-back on tour end ([ae79c44](https://github.com/Brennan-VanderLaan/stash/commit/ae79c443e95d8efb8efe1a4481ac90b193282d35))

## [1.21.2](https://github.com/Brennan-VanderLaan/stash/compare/v1.21.1...v1.21.2) (2026-05-15)


### Bug Fixes

* **admin:** hero compresses to sticky KPI bar on scroll ([7155924](https://github.com/Brennan-VanderLaan/stash/commit/7155924d26e3d9b7e00fc2d0d53bd612041c5272))
* **tours:** Replay button navigates to tour's home page ([6be631d](https://github.com/Brennan-VanderLaan/stash/commit/6be631d45541c3f42817d2110bcef6183603a8bd))
* **tours:** scroll target into view before positioning, drop meta history ([a2b1a0e](https://github.com/Brennan-VanderLaan/stash/commit/a2b1a0ec3083281dd65d66b72176f69691460e78))

## [1.21.1](https://github.com/Brennan-VanderLaan/stash/compare/v1.21.0...v1.21.1) (2026-05-15)


### Bug Fixes

* **admin:** drop sticky position from dashboard TOC ([e9696fc](https://github.com/Brennan-VanderLaan/stash/commit/e9696fcdaf81f1cab6b367dc39e2d70623815cc3))
* **boxes:** add visible "Edit box" CTA on the detail page ([e6a9a91](https://github.com/Brennan-VanderLaan/stash/commit/e6a9a91c8b73ffd5a56c0073515b99aea18b5c43))
* **labels:** unstack checkbox from regen-art button, bump tile width ([fd499d1](https://github.com/Brennan-VanderLaan/stash/commit/fd499d1a4ae362eb32220621f17984a6ea376b21))
* **locations:** surface "rename or replace floor" + add floors tour ([6da6b33](https://github.com/Brennan-VanderLaan/stash/commit/6da6b334b4afa2700c32bda37938c1a050ba4c54))
* **queue:** expand Customize by default for fresh ingest items ([56de28b](https://github.com/Brennan-VanderLaan/stash/commit/56de28bf18169b164720d001c9b4369defeb530f))

## [1.21.0](https://github.com/Brennan-VanderLaan/stash/compare/v1.20.0...v1.21.0) (2026-05-15)


### Features

* **audit:** Tinder-style swipe UI with resume + per-item endpoints ([742e928](https://github.com/Brennan-VanderLaan/stash/commit/742e928e017139d978b90348209931d9f895963e))
* **queue:** no-scroll-to-accept layout, prominent AI recommendation ([52d78e7](https://github.com/Brennan-VanderLaan/stash/commit/52d78e7d82033f7288eca905094ac00ebdda5f11))
* **tours:** first-run onboarding overlay with per-feature versioned flags ([1040a0d](https://github.com/Brennan-VanderLaan/stash/commit/1040a0d3c7889f8f7209d716b6d5553c5217db03))

## [1.20.0](https://github.com/Brennan-VanderLaan/stash/compare/v1.19.0...v1.20.0) (2026-05-15)


### Features

* **admin:** dashboard facelift (KPI hero + tenant cards + section TOC) ([f58f9ef](https://github.com/Brennan-VanderLaan/stash/commit/f58f9effa540aa9e09150ca3ac5f27e2b1afbbc3))
* **ingest:** hero facelift, fix copy, demote optional packing ([7b03552](https://github.com/Brennan-VanderLaan/stash/commit/7b035524a6b1271331ae7d9ea8e4a5e680ae2195))

## [1.19.0](https://github.com/Brennan-VanderLaan/stash/compare/v1.18.0...v1.19.0) (2026-05-15)


### Features

* **billing:** Stripe Checkout + webhook entitlement for Pro tier ([905a5a9](https://github.com/Brennan-VanderLaan/stash/commit/905a5a9e662a1a42d7246304b07ff6513e30fc42))

## [1.18.0](https://github.com/Brennan-VanderLaan/stash/compare/v1.17.0...v1.18.0) (2026-05-15)


### Features

* **admin:** group OAuth-issued API tokens by client ([702ff3a](https://github.com/Brennan-VanderLaan/stash/commit/702ff3aa4676ce107b77f03ab100ee48b7b756a2))
* **feedback:** export endpoint + operator-scoped MCP tools ([0a8d885](https://github.com/Brennan-VanderLaan/stash/commit/0a8d885c9225e6a372c7017e77dbcf76d65253b7))


### Bug Fixes

* **usage:** clarify backup vs Download-my-data with decision-tree copy ([f031296](https://github.com/Brennan-VanderLaan/stash/commit/f0312967c6813d4a8fb6889c4ebe53e2526c9a65))

## [1.17.0](https://github.com/Brennan-VanderLaan/stash/compare/v1.16.0...v1.17.0) (2026-05-15)


### Features

* **feedback:** in-app feedback widget + operator triage queue ([33acd2d](https://github.com/Brennan-VanderLaan/stash/commit/33acd2d65ce5485a4154e1effed4b2c8a5879915))
* **tags:** AI-suggested tags per item and per box ([44bf265](https://github.com/Brennan-VanderLaan/stash/commit/44bf2659f8103dcdb4c0829ea2ae49f3eb538958))
* **tags:** bulk-tag every item in a box ([cea2159](https://github.com/Brennan-VanderLaan/stash/commit/cea215905855b730bd1f54d49f8f880fa9c06c81))
* **usage:** facelift /usage into named sections + cost-transparency block ([da84a3a](https://github.com/Brennan-VanderLaan/stash/commit/da84a3a8c8474aad0ac7b87e88f1566295117b4f))
* **usage:** GDPR Article 20 'Download my data' bundle ([4a0a9dc](https://github.com/Brennan-VanderLaan/stash/commit/4a0a9dc18ed40937811b7b92deac4d69067314c3))

## [1.16.0](https://github.com/Brennan-VanderLaan/stash/compare/v1.15.0...v1.16.0) (2026-05-15)


### Features

* **labels:** print N copies of each selected box (1–4) ([db4d2dc](https://github.com/Brennan-VanderLaan/stash/commit/db4d2dc70dcf0b429fdedcc93634aca79d374b58))


### Bug Fixes

* **labels:** larger, darker text for arm's-length legibility ([448135d](https://github.com/Brennan-VanderLaan/stash/commit/448135d1fa843c018148ac675aceab1e9e0e9126))
* **labels:** preserve selection across new-tab nav + fix 5164 layout ([1c09755](https://github.com/Brennan-VanderLaan/stash/commit/1c09755e52552d575a117ca8361ae44827d53d1a))

## [1.15.0](https://github.com/Brennan-VanderLaan/stash/compare/v1.14.0...v1.15.0) (2026-05-14)


### Features

* **labels:** group by location/room + persist selection across nav ([a9ff7e6](https://github.com/Brennan-VanderLaan/stash/commit/a9ff7e6a5530f5dbdecf9debd9f219d314db8f4b))


### Bug Fixes

* **labels:** keep names inside the cell + drop visible cell edge ([664cf77](https://github.com/Brennan-VanderLaan/stash/commit/664cf7705545a123071afe3dbcfab02627f725fe))

## [1.14.0](https://github.com/Brennan-VanderLaan/stash/compare/v1.13.0...v1.14.0) (2026-05-12)


### Features

* **admin:** tenant lifecycle + vendor cost + OAuth clients (phase 12 finish) ([72a0ca4](https://github.com/Brennan-VanderLaan/stash/commit/72a0ca483ffb4862b83ef7b647ed2ce22aed8858))

## [1.13.0](https://github.com/Brennan-VanderLaan/stash/compare/v1.12.0...v1.13.0) (2026-05-12)


### Features

* **usage,tenants:** monthly sparklines + tenant switcher ([b9cc9c7](https://github.com/Brennan-VanderLaan/stash/commit/b9cc9c78bb916209d0776266de5d567e79841710))
* **usage:** bandwidth metering + on-disk storage footprint ([5768291](https://github.com/Brennan-VanderLaan/stash/commit/5768291c214acbbd1018bb1a1a5339be198c5d4d))

## [1.12.0](https://github.com/Brennan-VanderLaan/stash/compare/v1.11.0...v1.12.0) (2026-05-12)


### Features

* **labels:** room-color tint toggle + brighter background art ([be875ce](https://github.com/Brennan-VanderLaan/stash/commit/be875ce6ef9976035fcb415bda1c89982bcb904c))


### Bug Fixes

* **mobile:** admin tables scroll horizontally, usage rows wrap ([5160e65](https://github.com/Brennan-VanderLaan/stash/commit/5160e6563b988d24b764d09057d8cf8bcdf56d75))

## [1.11.0](https://github.com/Brennan-VanderLaan/stash/compare/v1.10.2...v1.11.0) (2026-05-10)


### Features

* **errors:** sassy 4xx/5xx pages starring a Siberian cat + a tortoise ([693396e](https://github.com/Brennan-VanderLaan/stash/commit/693396e734e716af43aa39125d49b5d7e59e8250))


### Bug Fixes

* **ingest:** retry-only-on-failed, serialize workers, add scope picker ([dca2ba3](https://github.com/Brennan-VanderLaan/stash/commit/dca2ba324557c4f6af2279a205fbf0128fa42962))

## [1.10.2](https://github.com/Brennan-VanderLaan/stash/compare/v1.10.1...v1.10.2) (2026-05-08)


### Bug Fixes

* **ingest:** timeout Gemini calls, recover stuck jobs, surface UI escape ([206b1f0](https://github.com/Brennan-VanderLaan/stash/commit/206b1f042ed4c779197e8ac50406bf84fbba93ed))
* **queue:** bbox overlay sticks to the rendered photo ([05b7a36](https://github.com/Brennan-VanderLaan/stash/commit/05b7a361eb9a621fe09b7019f785093836709b99))

## [1.10.1](https://github.com/Brennan-VanderLaan/stash/compare/v1.10.0...v1.10.1) (2026-05-08)


### Bug Fixes

* **ingest:** worker logs every step, never wedges in processing ([6a62e67](https://github.com/Brennan-VanderLaan/stash/commit/6a62e67d445dc467be70644794ace5468a2984ec))

## [1.10.0](https://github.com/Brennan-VanderLaan/stash/compare/v1.9.0...v1.10.0) (2026-05-08)


### Features

* **ingest:** packing-session box picker pre-fills sort queue ([995ead0](https://github.com/Brennan-VanderLaan/stash/commit/995ead033f931bc0b64b3cea0665c4b928bece98))

## [1.9.0](https://github.com/Brennan-VanderLaan/stash/compare/v1.8.1...v1.9.0) (2026-05-08)


### Features

* **admin:** last-activity columns, token filters, recent-activity feed ([2b27320](https://github.com/Brennan-VanderLaan/stash/commit/2b27320e173bee1abdd49a45d802452cc35769b9))


### Bug Fixes

* **labels:** center art behind text, upright preview, wrapped portrait text ([417a9b8](https://github.com/Brennan-VanderLaan/stash/commit/417a9b8a9a9f2e241301791739d30da48ab4fb59))

## [1.8.1](https://github.com/Brennan-VanderLaan/stash/compare/v1.8.0...v1.8.1) (2026-05-08)


### Bug Fixes

* **labels:** portrait rendering + persistence + cleaner tile ([f0425f0](https://github.com/Brennan-VanderLaan/stash/commit/f0425f0007287871da75309cf7e9c1d5ebf24b12))

## [1.8.0](https://github.com/Brennan-VanderLaan/stash/compare/v1.7.2...v1.8.0) (2026-05-08)


### Features

* **labels:** pivot to Avery shipping-label sheets + mobile More tab ([9ea6625](https://github.com/Brennan-VanderLaan/stash/commit/9ea66250a421aa75e1705b66d7eabd3039350ffc))

## [1.7.2](https://github.com/Brennan-VanderLaan/stash/compare/v1.7.1...v1.7.2) (2026-05-08)


### Bug Fixes

* **deploy:** SameSite=lax so Google OAuth callback survives cross-site redirect ([0ee1c38](https://github.com/Brennan-VanderLaan/stash/commit/0ee1c38abda095b4b7b3c4726847c8355e40b735))

## [1.7.1](https://github.com/Brennan-VanderLaan/stash/compare/v1.7.0...v1.7.1) (2026-05-08)


### Bug Fixes

* **oauth:** close P0/P1 audit findings + path-suffixed discovery ([d1386be](https://github.com/Brennan-VanderLaan/stash/commit/d1386be204014ecd4e1e705bfcc4ba18beac1ed1))

## [1.7.0](https://github.com/Brennan-VanderLaan/stash/compare/v1.6.1...v1.7.0) (2026-05-08)


### Features

* **oauth:** OAuth 2.1 authorization server for MCP discovery ([382a35f](https://github.com/Brennan-VanderLaan/stash/commit/382a35fa0b2094e9b29ce7e220a74a0c962ab6a8))

## [1.6.1](https://github.com/Brennan-VanderLaan/stash/compare/v1.6.0...v1.6.1) (2026-05-08)


### Bug Fixes

* **api:** mint API token without putting plaintext in a URL ([d6b4d37](https://github.com/Brennan-VanderLaan/stash/commit/d6b4d37c4e6f585e907aad9bdf909de2d35a26e6))

## [1.6.0](https://github.com/Brennan-VanderLaan/stash/compare/v1.5.0...v1.6.0) (2026-05-08)


### Features

* **mcp:** built-in /mcp endpoint speaking spec rev 2025-11-25 ([45d4525](https://github.com/Brennan-VanderLaan/stash/commit/45d45253406d977ad3aaf3a3d0544a373f1e3461))

## [1.5.0](https://github.com/Brennan-VanderLaan/stash/compare/v1.4.0...v1.5.0) (2026-05-08)


### Features

* **api:** /usage card for minting + revoking API tokens ([d5cd9af](https://github.com/Brennan-VanderLaan/stash/commit/d5cd9af082400e9637b64d5e9f43935696f84524))
* **api:** auto-revoke leaked tokens + operator suspend/resume ([c7de9ec](https://github.com/Brennan-VanderLaan/stash/commit/c7de9ecbff2266bdac3ad9c5f3b0e8aa257ed421))
* **api:** bearer-auth /api/v1 router for MCP-style agents ([25270c5](https://github.com/Brennan-VanderLaan/stash/commit/25270c53769f2bea04c179936696aabc20854005))
* **audit:** backfill audit_log on box / item / location / floor / room mutations ([c962d38](https://github.com/Brennan-VanderLaan/stash/commit/c962d38a602244f3123914d89c5a86b19fe04f7c))
* **logging:** structured logger + request-id middleware + context ([3a1505a](https://github.com/Brennan-VanderLaan/stash/commit/3a1505ace90a325e4effb389a8eebc9689ab1c91))
* **quotas:** plan-default caps + soft warning + 429 enforcement ([d0a19f9](https://github.com/Brennan-VanderLaan/stash/commit/d0a19f92cf5ef4e8c5a3e093bde4cbfbfe1a0b35))
* **quotas:** tenant-creation throttle + operator override editor + bug fix ([46dbb91](https://github.com/Brennan-VanderLaan/stash/commit/46dbb9188a5434d17db1ca40c37e2a1c703c0cdd))
* **shares:** per-share file allow-list at access time ([57d837b](https://github.com/Brennan-VanderLaan/stash/commit/57d837b3778179c9431b9a79e61fb6d4f9865539))


### Bug Fixes

* **docker:** copy obs.py into the image ([d9dd5ab](https://github.com/Brennan-VanderLaan/stash/commit/d9dd5abc7b7094a00a45d3b411033b2f908afac5))
* **security:** healthz + headers + samesite + invite-matcher + token cap + shares-unique + auth coverage ([c073b2b](https://github.com/Brennan-VanderLaan/stash/commit/c073b2b4714483d3b6bbacf5f041dabeb2b666c3))

## [1.4.0](https://github.com/Brennan-VanderLaan/stash/compare/v1.3.0...v1.4.0) (2026-05-08)


### Features

* **backup:** manual /admin trigger to upload tenant backup to B2 ([8065633](https://github.com/Brennan-VanderLaan/stash/commit/806563308e5b17f5aa47d63664a27555aa74eeb1))
* **backup:** per-tenant backup at /usage/backup ([2d12f2f](https://github.com/Brennan-VanderLaan/stash/commit/2d12f2fa6f0c1708f36975860c02922262603848))
* **shares:** object shares with cascade + dedupe + soft-delete pause ([7dc5bd2](https://github.com/Brennan-VanderLaan/stash/commit/7dc5bd28790d1b5f2ab366f69fffc4d9562798b3))

## [1.3.0](https://github.com/Brennan-VanderLaan/stash/compare/v1.2.0...v1.3.0) (2026-05-08)


### Features

* **admin:** operator dashboard + create-tenant-and-invite bootstrap ([4350228](https://github.com/Brennan-VanderLaan/stash/commit/43502282e4431db358c721d2bed3f916baa763ad))

## [1.2.0](https://github.com/Brennan-VanderLaan/stash/compare/v1.1.1...v1.2.0) (2026-05-08)


### Features

* **invites:** link-share tenant invites without an email provider ([93a0faf](https://github.com/Brennan-VanderLaan/stash/commit/93a0faf8951db9ddf72e4201f890e4f693c3ad1b))
* **telemetry:** per-tenant usage_events + /usage meters ([bf11b7e](https://github.com/Brennan-VanderLaan/stash/commit/bf11b7ed426208aaf95528fe503df55c1f27528f))

## [1.1.1](https://github.com/Brennan-VanderLaan/stash/compare/v1.1.0...v1.1.1) (2026-05-08)


### Bug Fixes

* **docker:** copy dao/ into the image ([68cfff6](https://github.com/Brennan-VanderLaan/stash/commit/68cfff61ea2dbd96a2752819ca8038a0ad179574))

## [1.1.0](https://github.com/Brennan-VanderLaan/stash/compare/v1.0.1...v1.1.0) (2026-05-08)


### Features

* **boxes:** wire optimistic concurrency through the box-edit form ([ff41e4f](https://github.com/Brennan-VanderLaan/stash/commit/ff41e4f470180ef1b8dd035c64e18ea0c1074c35))
* **crypto:** log phase-2 filesystem migration so the operator can see it ran ([7496b3b](https://github.com/Brennan-VanderLaan/stash/commit/7496b3b28459b0ff682f8cf55fcd876ec5a6a1ac))
* **dao:** add locations / floors / rooms / tags / pending_items / ingest_jobs modules ([5c89797](https://github.com/Brennan-VanderLaan/stash/commit/5c897977f2c7594ff37e405dc5a586a92ae07008))
* **dao:** migrate box edit + move-to-room mutations ([b5d47f9](https://github.com/Brennan-VanderLaan/stash/commit/b5d47f94f4a44abda9bb2fec9b4dc774dfd2291a))
* **dao:** migrate create_box, add_item, ingest worker, kill dead helpers ([88df191](https://github.com/Brennan-VanderLaan/stash/commit/88df191ddf83ccb5d5b8af9de3be011230db5696))
* **dao:** migrate ingest + queue-delete routes onto the DAO ([d865d85](https://github.com/Brennan-VanderLaan/stash/commit/d865d8592da9dd5e643c0bd7d6e13bcd5de6f875))
* **dao:** migrate item + recrop + audit + box-preview routes ([ee19a3a](https://github.com/Brennan-VanderLaan/stash/commit/ee19a3a1081454dc52cc563863ff1b7694f7a070))
* **dao:** migrate location/floor/room mutation routes onto the DAO ([32f7303](https://github.com/Brennan-VanderLaan/stash/commit/32f7303d394b841494152241e4de8cbc697ee92d))
* **dao:** migrate search, tags, labels, box-art, box-delete ([8c6deea](https://github.com/Brennan-VanderLaan/stash/commit/8c6deea0f0a37f82edae866d659e092a0c6a502d))
* **dao:** migrate the high-traffic read paths off raw conn.execute ([cc9a4fd](https://github.com/Brennan-VanderLaan/stash/commit/cc9a4fd1b7f49657cab66a6594aa0ef8bf3b795b))
* **dao:** scaffold + boxes/items/tenants modules ([cc6fa88](https://github.com/Brennan-VanderLaan/stash/commit/cc6fa8875d4f9d9a02db4e138eaec5e0d74b60e7))


### Refactors

* **dao:** drop dead set_item_tags / get_item_tags helpers ([cf3c265](https://github.com/Brennan-VanderLaan/stash/commit/cf3c265e9766587e149dea059642d2af62e503f0))

## [1.0.1](https://github.com/Brennan-VanderLaan/stash/compare/v1.0.0...v1.0.1) (2026-05-08)


### Bug Fixes

* **crypto:** copy vault.py into the Docker image ([2f1850d](https://github.com/Brennan-VanderLaan/stash/commit/2f1850dce698ee7ce9cb4220b20d48d3b2b84437))
* **crypto:** document STASH_KEK in deploy/.env.example + thread through compose ([826f9f8](https://github.com/Brennan-VanderLaan/stash/commit/826f9f863f4385f634eaafcccc29044ea0c00ed0))

## [1.0.0](https://github.com/Brennan-VanderLaan/stash/compare/v0.20.2...v1.0.0) (2026-05-08)


### ⚠ BREAKING CHANGES

* STASH_ALLOWED_EMAILS and FULLY_PUBLIC are removed. The new gate is tenant_members; emails not on that table get a 403. Operators are configured via STASH_OPERATOR_EMAILS but get no automatic access to any tenant's data — they must be invited as a member, by design.

### Features

* phase 1 + 2 land — multi-tenancy foundation + encryption at rest ([e4a953c](https://github.com/Brennan-VanderLaan/stash/commit/e4a953cf083978f2a20b08629a6c15f564e9d75d))

## [0.20.2](https://github.com/Brennan-VanderLaan/stash/compare/v0.20.1...v0.20.2) (2026-05-07)


### Refactors

* Phase 1 ([39d4791](https://github.com/Brennan-VanderLaan/stash/commit/39d47918c4fb0a1abb63258d94a022a52a8a4dd2))

## [0.20.1](https://github.com/Brennan-VanderLaan/stash/compare/v0.20.0...v0.20.1) (2026-05-07)


### Bug Fixes

* **queue:** real-time updates that don't trample in-flight edits ([1cc437e](https://github.com/Brennan-VanderLaan/stash/commit/1cc437e3b3f6b8c03afe35293744a589e2df49af))

## [0.20.0](https://github.com/Brennan-VanderLaan/stash/compare/v0.19.0...v0.20.0) (2026-05-07)


### Features

* **index:** group boxes by location/room so the page isn't a wall of tiles ([bd8ad3e](https://github.com/Brennan-VanderLaan/stash/commit/bd8ad3eec2827fb14e77354eca007b5826c4ab7e))


### Bug Fixes

* cropper "Done" now reflects the user's adjustment instead of reverting ([2d3c983](https://github.com/Brennan-VanderLaan/stash/commit/2d3c983c91ec446286371dfe24dd6f69ca62af4f))
* locations index falls back to floor floorplans before reporting "no floorplan" ([5a53dd3](https://github.com/Brennan-VanderLaan/stash/commit/5a53dd38aa2218cc52c5c921f45d5497ec896990))

## [0.19.0](https://github.com/Brennan-VanderLaan/stash/compare/v0.18.1...v0.19.0) (2026-05-07)


### Features

* thread per-box colours through the index, room view, search, and floorplan preview ([299d290](https://github.com/Brennan-VanderLaan/stash/commit/299d29046d59a3e4be27225be7497bc95ee5d679))


### Bug Fixes

* render boxes index as a uniform grid instead of stub-card chaos ([4ab5579](https://github.com/Brennan-VanderLaan/stash/commit/4ab55796efe5bd2196508f6ce33bf4dc45e84c19))
* strip EXIF before sending to the vision model so AI bboxes line up ([4aece97](https://github.com/Brennan-VanderLaan/stash/commit/4aece971b0c40b3f3f14ec62fc376ea384431080))

## [0.18.1](https://github.com/Brennan-VanderLaan/stash/compare/v0.18.0...v0.18.1) (2026-05-07)


### Bug Fixes

* stop cropper.js double-rotating EXIF photos so saved crop matches drag ([7eb2e1c](https://github.com/Brennan-VanderLaan/stash/commit/7eb2e1c294000549f370e153532abeddf10fb638))

## [0.18.0](https://github.com/Brennan-VanderLaan/stash/compare/v0.17.2...v0.18.0) (2026-05-07)


### Features

* **maintenance:** show access state and revocation runbook ([05ae3ea](https://github.com/Brennan-VanderLaan/stash/commit/05ae3ea5800e0cff7edf02b5ba954a65baa2f65c))


### Bug Fixes

* pre-generate thumbnail on re-crop so stale thumbs don't survive ([05e9d78](https://github.com/Brennan-VanderLaan/stash/commit/05e9d78f329247089732d6c32769e1a0cec4629c))
* **security:** drop --email-domain wildcard so emails.txt actually gates ([249bdd3](https://github.com/Brennan-VanderLaan/stash/commit/249bdd338e2ff291befef478912f0f340e187cd6))
* **security:** enforce email allow-list at the app layer ([20de2a8](https://github.com/Brennan-VanderLaan/stash/commit/20de2a808079b08777a994314b29c48de2cd6693))
* stop floorplan snapping to the left at large viewports ([0790f48](https://github.com/Brennan-VanderLaan/stash/commit/0790f48f37460a186c82f93b2bc3ad346dc05aef))

## [0.17.2](https://github.com/Brennan-VanderLaan/stash/compare/v0.17.1...v0.17.2) (2026-05-07)


### Bug Fixes

* native image drag was eating pointer events on mosaic items ([39a8f91](https://github.com/Brennan-VanderLaan/stash/commit/39a8f9127b9457d8bf7e7386586c85d49fac9077))

## [0.17.1](https://github.com/Brennan-VanderLaan/stash/compare/v0.17.0...v0.17.1) (2026-05-07)


### Bug Fixes

* tiles fill the room, mosaic fills the tile, items actually draggable ([4b51ab5](https://github.com/Brennan-VanderLaan/stash/commit/4b51ab5400ee8a693aff26a534f8e99bc55442c9))

## [0.17.0](https://github.com/Brennan-VanderLaan/stash/compare/v0.16.0...v0.17.0) (2026-05-07)


### Features

* bigger floorplan tiles, item DnD between boxes, hover preview, drop-flash fix ([6fce701](https://github.com/Brennan-VanderLaan/stash/commit/6fce7017c220ff75f576425b8df62585a0b1fb92))

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
