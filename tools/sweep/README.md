# `sweep` — visual layout sweep tool

A small, **standalone** local dev tool: Playwright captures the
public surface across a viewport × route matrix, Gemini Flash
flags layout bugs, you annotate in a static gallery, and hand the
findings to an AI for a fix pass.

Self-contained.  No imports from this app — designed to graduate
out of this repo into a generic web-dev tool.

## What it produces

```
tmp/sweep/<UTC-timestamp>/
├── shots/
│   ├── root__iphone-se.png
│   ├── root__iphone-pro.png
│   ├── about-pricing__ipad.png
│   └── …                            (~45 shots for a default run)
├── manifest.json                    routes + viewports + AI findings
└── index.html                       gallery + annotation UI
```

Open the generated `index.html` directly in a browser (file://
works).  No server needed for the gallery itself.

## Install

```bash
# From the repo root, with your venv active:
pip install -r tools/sweep/requirements.txt
playwright install chromium
```

`playwright install` downloads the chromium binary (~150 MB) into
the playwright cache directory.  Microsoft's CDN serves it — not
npm.  Browsers cache across projects so this is a one-time cost
per machine.

For the AI review pass:

```bash
export GEMINI_API_KEY=...   # or set in your env
# (.env / direnv / Windows: $env:GEMINI_API_KEY = "...")
```

Without the key the tool still runs in capture-only mode — the
gallery shows up empty of AI findings.

## Run

The dev server must already be listening:

```bash
uvicorn app:app --reload --port 8000   # in another terminal
```

Then sweep:

```bash
python tools/sweep/sweep.py
```

That captures the default public-route matrix (`/` + 8 `/about/*`
pages) × 5 viewports = 45 shots, runs each through Gemini for a
layout-review pass, writes the bundle, and **serves the gallery
locally** at `http://127.0.0.1:<random-port>/index.html` — your
default browser opens automatically.  Press Ctrl+C in the terminal
to stop the server when you're done reviewing.

Why a local server rather than just opening the file: modern
browsers + VSCode's HTML preview + Windows file associations
all have at least one paper cut around `file://`.  A localhost
server bypasses every one of them.

If you'd rather skip the server (CI, headless box, scripting):

```bash
python tools/sweep/sweep.py --no-serve
# write-only mode — gallery path is printed and the tool exits
```

### Common flags

```bash
# Capture-only (skip AI):
python tools/sweep/sweep.py --no-ai

# Different model:
python tools/sweep/sweep.py --model gemini-1.5-flash

# Authed routes via the oauth2-proxy bypass header:
python tools/sweep/sweep.py \
    --routes /home,/usage,/admin \
    --header-email you@example.com

# A single viewport for fast iteration:
python tools/sweep/sweep.py --viewports iphone-se

# A preset of related viewports:
python tools/sweep/sweep.py --viewports phones
python tools/sweep/sweep.py --viewports desktops
python tools/sweep/sweep.py --viewports all      # ~18 viewports!

# Mix presets + individual nicknames:
python tools/sweep/sweep.py --viewports phones,desktop-2k

# Custom output directory:
python tools/sweep/sweep.py --out-dir /tmp/sweep-now

# Pin the gallery to a fixed port (handy if you bookmark the URL):
python tools/sweep/sweep.py --port 8765

# Don't auto-launch a browser tab:
python tools/sweep/sweep.py --no-open
```

## Annotation gallery

* Each shot renders with **AI findings auto-overlaid** as dashed
  coloured rectangles (red = high, yellow = medium, grey = low).
  Hover or scroll the finding list to read each.  Click an AI
  circle to dismiss it (so it doesn't pollute the export); click
  again to undismiss.
* **Click and drag** anywhere on a shot to draw your own
  rectangle.  A dialog asks for an optional note.  Click an
  existing user-circle to edit its note.
* Filter by viewport with the buttons in the header (or "all").
* **Copy report**: bundles every kept finding (yours + the AI
  ones you didn't dismiss) into a markdown report, with image
  paths and percentage coordinates, ready to paste into a chat
  with Claude / Cursor / whatever pair you're using.
* **Export JSON**: dumps the raw `annotations` blob for archival
  or to re-import on a different machine.  Pair with the
  `manifest.json` + `shots/` directory to reproduce the full
  review state elsewhere.
* Annotations persist in `localStorage` keyed by `run_id`, so a
  page reload mid-review doesn't lose work.

## Architecture (~one screenful)

```
sweep.py        capture (playwright) + ai_review (gemini)
                  └── writes shots/, manifest.json, copies gallery.html

gallery.html    static page (no build step, no server, file:// safe)
                  └── fetches manifest.json at load time
                  └── renders + persists user annotations to localStorage
                  └── "Copy report" → markdown for Claude / Cursor
```

`sweep.py` is ~330 lines.  `gallery.html` is ~500 lines including
the CSS.  Both are dependency-light by design.

## Tuning the AI review prompt

The prompt lives at the top of `sweep.py` under `_REVIEW_PROMPT`.
Edit it if the model is over-reporting (too many false positives
on perfectly-fine content) or under-reporting (missing real bugs).
Test changes with `--routes /about/pricing --viewports iphone-se`
for a fast one-shot iteration loop.

## Cost

Gemini 2.0 Flash bills per image at roughly $0.001 — a 45-shot
sweep costs less than a nickel.  Stash's existing transparency
block doesn't count this (it's a dev-side cost, not a per-tenant
cost), but you can check your billing dashboard for confirmation.
