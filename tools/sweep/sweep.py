"""Visual sweep: Playwright captures + Gemini layout-review pass,
streamed live into a local gallery via Server-Sent Events.

Standalone tool — designed to graduate out of this repo into a
generic Playwright-pattern toolbelt for other web projects.  Does
not import anything from stash itself.

Run:
    python tools/sweep/sweep.py
        --base-url http://localhost:8000
        --viewports iphone-se,iphone-pro,ipad,laptop,desktop
        --routes /,/about,/about/pricing,...
        --header-email me@example.com    # optional, for authed pages

Output goes to ``tmp/sweep/<UTC-timestamp>/`` next to the project
root by default.  The gallery is served at
``http://127.0.0.1:<port>/`` and the browser opens automatically;
shots + AI findings stream in as they complete.  Ctrl+C in the
terminal stops the server.
"""
from __future__ import annotations

import argparse
import asyncio
import http.server
import json
import os
import queue as queuelib
import shutil
import socketserver
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── Defaults ────────────────────────────────────────────────────────


DEFAULT_VIEWPORTS: dict[str, tuple[int, int]] = {
    "iphone-se":  (375, 667),
    "iphone-pro": (393, 852),
    "ipad":       (768, 1024),
    "laptop":     (1366, 768),
    "desktop":    (1920, 1080),
}

DEFAULT_ROUTES: list[str] = [
    "/",
    "/about",
    "/about/pricing",
    "/about/transparency",
    "/about/refunds",
    "/about/privacy",
    "/about/terms",
    "/about/sub-processors",
    "/about/contact",
]

DEFAULT_MODEL = "gemini-2.0-flash"

DEFAULT_CAPTURE_CONCURRENCY = 32
DEFAULT_REVIEW_CONCURRENCY = 4


# ── AI prompt ───────────────────────────────────────────────────────


_REVIEW_PROMPT = """\
You are reviewing a web-page screenshot for LAYOUT BUGS only.

Viewport: {viewport_label} ({width}x{height} CSS px).
Route: {route}.

Look for these specific problems:
- Horizontal overflow (content clipped on the right edge)
- Vertical headers eating > 25% of the viewport
- Text overlapping other elements or running off the edge
- Buttons / links smaller than ~40px in either dimension on a mobile
  viewport (finger-tap targets)
- Content cut off at the bottom edge that should have wrapped
- Misaligned columns or wildly inconsistent gaps
- Illegible contrast between foreground and background
- Broken image placeholders (alt text or generic icon visible)
- A primary CTA pushed below the fold on a small viewport when it
  should be visible

DO NOT critique colors, typography preferences, or content choices.
Layout structural bugs only.

For each finding, output a JSON object with the bounding box of the
problem area as PERCENTAGES of the viewport (0-100), so the gallery
can overlay it accurately at any display scale:
    {{"x": 12.5, "y": 4.2, "w": 75.0, "h": 8.5,
      "severity": "high" | "medium" | "low",
      "issue": "one short sentence"}}

If you see no layout bugs, return an empty array."""


# ── Streaming bus (SSE) ─────────────────────────────────────────────


class StreamBus:
    """Tiny in-memory pub-sub for SSE events.  Producer is the
    async sweep loop (running in the main thread); consumers are
    the per-request handler threads spawned by
    ``ThreadingHTTPServer``.

    Maintains an event history so a late-arriving subscriber can
    replay everything since the run started — important when the
    browser autoload races with the first capture.  The history
    bounds at a few hundred entries; a 45-shot sweep emits ~91
    events (1 initial + 2 per shot + 1 done) which fits easily."""

    def __init__(self, max_history: int = 1000) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queuelib.Queue[str]] = []
        self._history: list[str] = []
        self._max_history = max_history
        self._closed = False

    def publish(self, event: dict[str, Any]) -> None:
        if self._closed:
            return
        data = json.dumps(event, separators=(",", ":"))
        with self._lock:
            self._history.append(data)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]
            dead: list[queuelib.Queue[str]] = []
            for sub in self._subscribers:
                try:
                    sub.put_nowait(data)
                except queuelib.Full:
                    dead.append(sub)
            for sub in dead:
                self._subscribers.remove(sub)

    def subscribe(self) -> tuple[queuelib.Queue[str], list[str]]:
        """Return a fresh queue + the full history snapshot.  The
        history is delivered eagerly so the client renders the
        current state on connect instead of waiting for the next
        live event."""
        q: queuelib.Queue[str] = queuelib.Queue(maxsize=1000)
        with self._lock:
            history = list(self._history)
            self._subscribers.append(q)
        return q, history

    def unsubscribe(self, q: queuelib.Queue[str]) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def close(self) -> None:
        self._closed = True


# Global bus — the request handler reads from it, the sweep coroutines
# write to it.  Module-global so the handler class (which is
# instantiated per-request by http.server) can reach it without
# constructor plumbing.
_BUS: StreamBus | None = None


# ── HTTP handler ────────────────────────────────────────────────────


class _GalleryHandler(http.server.SimpleHTTPRequestHandler):
    """Serves the gallery static files + a Server-Sent Events
    endpoint at ``/events`` that streams sweep progress to any
    connected gallery tab."""

    # Set in serve_gallery() before binding so the handler's
    # SimpleHTTPRequestHandler base knows where to read files.
    DIRECTORY: str = ""

    def __init__(self, *args, **kwargs) -> None:  # noqa: D401
        super().__init__(*args, directory=self.DIRECTORY, **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: ARG002
        # Static-server logs are noise; suppress.  SSE keepalives in
        # particular would spam stdout once per 25s per client.
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("/events", "/events.sse"):
            self._serve_sse()
            return
        super().do_GET()

    def _serve_sse(self) -> None:
        assert _BUS is not None
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        # CORS isn't needed (localhost only) but doesn't hurt and
        # makes the endpoint reusable if someone hosts the gallery
        # off the sweep server later.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        q, history = _BUS.subscribe()
        try:
            # Eagerly replay the history so the client starts fully
            # populated.  Frames are separated by the SSE delimiter
            # (\n\n) per the spec; ``data:`` lines hold the JSON.
            for data in history:
                self._send_event(data)
            # Live tail until the connection drops.  ``Queue.get``
            # with a timeout doubles as a keepalive: if no event for
            # 25s, send a comment-only chunk so proxies don't drop
            # the connection as idle.
            while True:
                try:
                    data = q.get(timeout=25.0)
                except queuelib.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self._send_event(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Client closed the tab; expected.
            pass
        finally:
            _BUS.unsubscribe(q)

    def _send_event(self, data: str) -> None:
        # Multi-line ``data:`` chunks are allowed but JSON.dumps with
        # the default separators returns one line — keep it simple.
        self.wfile.write(b"data: ")
        self.wfile.write(data.encode("utf-8"))
        self.wfile.write(b"\n\n")
        self.wfile.flush()


# ── Capture ─────────────────────────────────────────────────────────


@dataclass
class Shot:
    """One captured frame."""
    route: str
    viewport: str
    width: int
    height: int
    filename: str
    bytes_path: Path

    @property
    def slug(self) -> str:
        route_slug = self.route.strip("/").replace("/", "-") or "root"
        return f"{route_slug}__{self.viewport}"

    def to_payload(self) -> dict:
        return {
            "slug": self.slug,
            "route": self.route,
            "viewport": self.viewport,
            "width": self.width,
            "height": self.height,
            "image": f"shots/{self.filename}",
        }


async def capture_one(
    browser, route: str, viewport_name: str, w: int, h: int,
    base_url: str, header_email: str | None, out_dir: Path,
    sem: asyncio.Semaphore,
) -> Shot:
    """Capture one (route, viewport) pair to a PNG."""
    async with sem:
        ctx_kwargs: dict[str, Any] = {"viewport": {"width": w, "height": h}}
        if header_email:
            ctx_kwargs["extra_http_headers"] = {
                "X-Forwarded-Email": header_email,
            }
        ctx = await browser.new_context(**ctx_kwargs)
        page = await ctx.new_page()
        try:
            await page.goto(
                f"{base_url}{route}",
                wait_until="networkidle",
                timeout=15_000,
            )
            await page.wait_for_timeout(250)
            route_slug = route.strip("/").replace("/", "-") or "root"
            filename = f"{route_slug}__{viewport_name}.png"
            bytes_path = out_dir / "shots" / filename
            bytes_path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(bytes_path), full_page=True)
            return Shot(
                route=route, viewport=viewport_name, width=w, height=h,
                filename=filename, bytes_path=bytes_path,
            )
        finally:
            await ctx.close()


# ── AI review ───────────────────────────────────────────────────────


async def review_one(client, model: str, shot: Shot,
                     sem: asyncio.Semaphore) -> list[dict]:
    """Send one screenshot to Gemini, parse its JSON-array reply
    into a list of finding dicts.  Errors swallow to an empty list."""
    from google.genai import types as gtypes  # local import

    async with sem:
        prompt = _REVIEW_PROMPT.format(
            viewport_label=shot.viewport,
            width=shot.width,
            height=shot.height,
            route=shot.route,
        )
        try:
            img_bytes = shot.bytes_path.read_bytes()
            schema = {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "w": {"type": "number"},
                        "h": {"type": "number"},
                        "severity": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                        },
                        "issue": {"type": "string"},
                    },
                    "required": ["x", "y", "w", "h", "severity", "issue"],
                },
            }
            resp = await client.aio.models.generate_content(
                model=model,
                contents=[
                    gtypes.Part.from_bytes(
                        data=img_bytes, mime_type="image/png",
                    ),
                    prompt,
                ],
                config=gtypes.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=0.0,
                ),
            )
            text = resp.text or "[]"
            findings = json.loads(text)
            if not isinstance(findings, list):
                return []
            return findings
        except Exception as exc:  # noqa: BLE001
            print(f"  ! AI review failed for {shot.slug}: {exc}",
                  file=sys.stderr)
            return []


# ── Streaming pipeline ──────────────────────────────────────────────


async def sweep_pipeline(
    routes: list[str], viewports: dict[str, tuple[int, int]],
    base_url: str, header_email: str | None, out_dir: Path,
    capture_concurrency: int, review_concurrency: int,
    model: str, ai_enabled: bool,
) -> tuple[list[Shot], dict[str, list[dict]]]:
    """Pipelined capture + review.  Each shot, once captured,
    *immediately* starts its review while other shots are still
    being captured — no batch boundary between phases.  This gives
    a smoother live-fill of the gallery than running all captures
    first and all reviews second.

    Events are pushed onto the global ``StreamBus`` as work
    completes, so any connected gallery tab updates in real time."""
    from playwright.async_api import async_playwright  # local import

    capture_sem = asyncio.Semaphore(capture_concurrency)
    review_sem = asyncio.Semaphore(review_concurrency)

    gemini_client = None
    if ai_enabled:
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if api_key:
            from google import genai  # local import
            gemini_client = genai.Client(api_key=api_key)
        else:
            print("  ! GEMINI_API_KEY not set — skipping AI review pass.",
                  file=sys.stderr)

    all_shots: list[Shot] = []
    all_findings: dict[str, list[dict]] = {}

    async def process(route: str, vp_name: str, w: int, h: int,
                      browser) -> None:
        try:
            shot = await capture_one(
                browser, route, vp_name, w, h,
                base_url, header_email, out_dir, capture_sem,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  ! capture failed for {route} @ {vp_name}: {exc}",
                  file=sys.stderr)
            if _BUS is not None:
                _BUS.publish({
                    "type": "capture_error",
                    "route": route, "viewport": vp_name,
                    "error": str(exc),
                })
            return
        all_shots.append(shot)
        if _BUS is not None:
            _BUS.publish({"type": "shot", "shot": shot.to_payload()})
        if gemini_client is None:
            all_findings[shot.slug] = []
            return
        findings = await review_one(
            gemini_client, model, shot, review_sem,
        )
        all_findings[shot.slug] = findings
        if _BUS is not None:
            _BUS.publish({
                "type": "review",
                "slug": shot.slug,
                "findings": findings,
            })

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            tasks = [
                process(route, vp_name, w, h, browser)
                for route in routes
                for vp_name, (w, h) in viewports.items()
            ]
            await asyncio.gather(*tasks)
        finally:
            await browser.close()

    return all_shots, all_findings


# ── Manifest + gallery ──────────────────────────────────────────────


def write_manifest(
    out_dir: Path, run_id: str, base_url: str,
    shots: list[Shot], ai_findings: dict[str, list[dict]],
    *, status: str = "running",
) -> None:
    """Serialise the run to ``manifest.json``.  Written twice per run:
    once empty before the pipeline starts (so a tab reload mid-run
    still has a usable bootstrap), once with the final state after
    ``done`` (so reloads after the server stops still show
    everything)."""
    manifest = {
        "run_id": run_id,
        "base_url": base_url,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "shots": [
            {
                **s.to_payload(),
                "ai_findings": ai_findings.get(s.slug, []),
            }
            for s in shots
        ],
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8",
    )


def write_gallery(out_dir: Path) -> None:
    """Render the gallery template into the output dir with the
    manifest inlined.  See the rationale comment in the previous
    revision — same trick, plus SSE streams live updates on top."""
    src = Path(__file__).parent / "gallery.html"
    dst = out_dir / "index.html"
    template = src.read_text(encoding="utf-8")
    manifest_path = out_dir / "manifest.json"
    manifest_json = manifest_path.read_text(encoding="utf-8")
    safe_json = manifest_json.replace("</", "<\\/")
    rendered = template.replace(
        "<!-- MANIFEST_INLINE_MARKER -->",
        f'<script type="application/json" id="sweep-manifest">'
        f'{safe_json}</script>',
    )
    dst.write_text(rendered, encoding="utf-8")


# ── Local server ────────────────────────────────────────────────────


def start_server(
    out_dir: Path, port: int,
) -> tuple[http.server.ThreadingHTTPServer, int, threading.Thread]:
    """Bring up the streaming gallery server in a background thread
    and return immediately (so the sweep loop can start emitting
    events).  Returns (httpd, actual_port, thread) — caller closes
    the server when the user Ctrl+Cs."""
    _GalleryHandler.DIRECTORY = str(out_dir)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port),
                                            _GalleryHandler)
    httpd.daemon_threads = True
    actual_port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, actual_port, thread


# ── Entrypoint ──────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="sweep",
        description=(
            "Capture screenshots across a viewport × route matrix, "
            "ask Gemini to flag layout bugs, and stream the results "
            "into a local gallery for annotation."
        ),
    )
    p.add_argument(
        "--base-url", default=os.environ.get(
            "SWEEP_BASE_URL", "http://localhost:8000",
        ),
        help="Where the dev server is listening (default: %(default)s).",
    )
    p.add_argument(
        "--routes", default=",".join(DEFAULT_ROUTES),
        help="Comma-separated list of routes to capture.",
    )
    p.add_argument(
        "--viewports", default=",".join(DEFAULT_VIEWPORTS.keys()),
        help=(
            "Comma-separated viewport nicknames from the default "
            f"palette: {', '.join(DEFAULT_VIEWPORTS.keys())}."
        ),
    )
    p.add_argument(
        "--header-email", default="",
        help=(
            "X-Forwarded-Email value to send on every request.  "
            "Use to sweep authed routes via oauth2-proxy bypass; "
            "leave empty for the public surface."
        ),
    )
    p.add_argument(
        "--out-dir", default="",
        help=(
            "Output directory.  Default: "
            "``tmp/sweep/<UTC-timestamp>/`` next to the repo root."
        ),
    )
    p.add_argument(
        "--no-ai", action="store_true",
        help="Skip the Gemini review pass.  Capture-only mode.",
    )
    p.add_argument(
        "--model", default=DEFAULT_MODEL,
        help="Gemini model id (default: %(default)s).",
    )
    p.add_argument(
        "--no-serve", action="store_true",
        help=(
            "Skip the local HTTP server.  Run the sweep, write the "
            "files, exit.  Loses the live-streaming UX."
        ),
    )
    p.add_argument(
        "--no-open", action="store_true",
        help="Don't auto-open a browser when the server starts.",
    )
    p.add_argument(
        "--port", type=int, default=0,
        help=(
            "Local server port (default: 0 = pick a free port).  "
            "Set explicitly if you want a stable URL across runs."
        ),
    )
    p.add_argument(
        "--capture-concurrency", type=int, default=DEFAULT_CAPTURE_CONCURRENCY,
        help=(
            "Max parallel chromium contexts during capture "
            "(default: %(default)s)."
        ),
    )
    p.add_argument(
        "--review-concurrency", type=int, default=DEFAULT_REVIEW_CONCURRENCY,
        help=(
            "Max parallel Gemini requests during AI review "
            "(default: %(default)s).  Free-tier 15 RPM cap: keep ≤ 4."
        ),
    )
    return p.parse_args(argv)


def resolve_out_dir(explicit: str) -> Path:
    if explicit:
        return Path(explicit).resolve()
    repo_root = Path(__file__).resolve().parent.parent.parent
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    return repo_root / "tmp" / "sweep" / stamp


def resolve_viewports(names: str) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for raw in names.split(","):
        nick = raw.strip()
        if not nick:
            continue
        if nick not in DEFAULT_VIEWPORTS:
            print(f"  ! unknown viewport {nick!r}; known: "
                  f"{', '.join(DEFAULT_VIEWPORTS)}", file=sys.stderr)
            sys.exit(2)
        out[nick] = DEFAULT_VIEWPORTS[nick]
    if not out:
        print("  ! no viewports selected", file=sys.stderr)
        sys.exit(2)
    return out


async def run(args: argparse.Namespace) -> None:
    global _BUS
    out_dir = resolve_out_dir(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "shots").mkdir(exist_ok=True)
    run_id = out_dir.name
    routes = [r.strip() for r in args.routes.split(",") if r.strip()]
    viewports = resolve_viewports(args.viewports)
    n_shots = len(routes) * len(viewports)

    # Write an empty manifest + the gallery shell up front so the
    # server has something to serve the moment it binds.
    write_manifest(out_dir, run_id, args.base_url, [], {})
    write_gallery(out_dir)

    httpd = None
    if not args.no_serve:
        _BUS = StreamBus()
        httpd, port, _thread = start_server(out_dir, args.port)
        url = f"http://127.0.0.1:{port}/index.html"
        print(f"sweep · {run_id}")
        print(f"  base_url: {args.base_url}")
        print(f"  routes:   {len(routes)} × viewports: {len(viewports)} = "
              f"{n_shots} shots")
        print(f"  parallel: capture={args.capture_concurrency}, "
              f"review={args.review_concurrency}")
        print(f"  out_dir:  {out_dir}")
        print(f"\nserving gallery at {url}")
        print(f"  shots + AI reviews stream in live")
        print(f"  (Ctrl+C to stop)")
        if not args.no_open:
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass
        _BUS.publish({
            "type": "run_started",
            "run_id": run_id,
            "base_url": args.base_url,
            "total_shots": n_shots,
            "ai_enabled": not args.no_ai,
        })
    else:
        print(f"sweep · {run_id}")
        print(f"  parallel: capture={args.capture_concurrency}, "
              f"review={args.review_concurrency}")
        print(f"  --no-serve: writing only, gallery won't auto-open")

    t0 = time.monotonic()
    try:
        shots, ai_findings = await sweep_pipeline(
            routes=routes,
            viewports=viewports,
            base_url=args.base_url,
            header_email=args.header_email or None,
            out_dir=out_dir,
            capture_concurrency=args.capture_concurrency,
            review_concurrency=args.review_concurrency,
            model=args.model,
            ai_enabled=not args.no_ai,
        )
    except KeyboardInterrupt:
        print("\ninterrupted during sweep — gallery still serves "
              "whatever streamed in.")
        if _BUS is not None:
            _BUS.publish({"type": "done", "status": "interrupted"})
        if httpd is not None:
            await _block_until_ctrl_c(httpd)
        return

    elapsed = time.monotonic() - t0
    total_findings = sum(len(v) for v in ai_findings.values())
    print(f"\n✓ done in {elapsed:.1f}s — "
          f"{len(shots)} shots, {total_findings} AI findings")

    # Final manifest with everything captured.  A fresh page load
    # after this point reads the inline manifest and sees the
    # complete state without needing the SSE stream.
    write_manifest(
        out_dir, run_id, args.base_url, shots, ai_findings,
        status="complete",
    )
    write_gallery(out_dir)

    if _BUS is not None:
        _BUS.publish({
            "type": "done",
            "status": "complete",
            "elapsed_seconds": round(elapsed, 1),
            "total_shots": len(shots),
            "total_findings": total_findings,
        })

    if httpd is None:
        print(f"  gallery: {(out_dir / 'index.html')}")
        return

    await _block_until_ctrl_c(httpd)


async def _block_until_ctrl_c(httpd: http.server.ThreadingHTTPServer) -> None:
    """Idle the main coroutine after the sweep finishes so the
    server keeps running.  Ctrl+C drops out cleanly."""
    print("\n  (gallery still serving — Ctrl+C to stop)")
    try:
        # ``asyncio.Event().wait()`` blocks forever; interrupt
        # via the standard Python SIGINT path.
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        httpd.shutdown()
        if _BUS is not None:
            _BUS.close()
        print("stopped.")


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        # asyncio.run propagates Ctrl+C as KeyboardInterrupt out
        # of _block_until_ctrl_c sometimes; swallow at the top.
        pass


if __name__ == "__main__":
    main()
