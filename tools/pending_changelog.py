#!/usr/bin/env python3
"""Build a "pending release" preview for ``:dev`` image builds.

Walks conventional-commit history from the most recent ``v*.*.*``
tag to ``HEAD``, predicts the next semver bump, and renders a
markdown block to prepend to ``CHANGELOG.md`` so staging's
``/admin/maintenance`` page shows "if we cut a release right now,
this is what would ship."

Section grouping mirrors release-please's default Node release-type
config so the preview reads identically to the eventual Release PR
on ``dev → main``:

* ``feat``    → ### Features
* ``fix``     → ### Bug Fixes
* ``perf``    → ### Performance Improvements
* ``revert``  → ### Reverts

Other types (``chore``, ``ci``, ``docs``, ``style``, ``refactor``,
``test``, ``build``) are hidden by design — they're hidden in the
real Release PR too, so showing them on staging would be confusing
("why does my pending preview list this but the actual release
PR didn't?").

Output: a single JSON object on stdout with ``version`` and
``markdown`` keys.  Empty / missing pieces are handled gracefully
so a brand-new repo with no tags doesn't crash the build.

Usage from build.yml:

    python tools/pending_changelog.py > /tmp/pending.json
    echo "version=$(jq -r .version /tmp/pending.json)" >> $GITHUB_OUTPUT
    jq -r .markdown /tmp/pending.json > /tmp/pending.md
    cat /tmp/pending.md CHANGELOG.md > /tmp/CHANGELOG.combined
    mv /tmp/CHANGELOG.combined CHANGELOG.md

Not invoked on tag pushes — those ship the canonical
release-please-generated CHANGELOG.md unchanged.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Conventional-commit header: ``<type>(<scope>)?!?: <subject>``.
# Scope is optional, the trailing ``!`` marks a breaking change.
_CC_RE = re.compile(
    r"^(?P<type>\w+)(?:\((?P<scope>[^)]+)\))?(?P<breaking>!)?:\s*"
    r"(?P<subject>.+)$"
)

# Visible sections in render order — same shape and order as
# release-please-types-config-node so the preview mirrors the
# eventual Release PR's body.
_SECTIONS: list[tuple[str, str]] = [
    ("feat", "Features"),
    ("fix", "Bug Fixes"),
    ("perf", "Performance Improvements"),
    ("revert", "Reverts"),
]


# ── Pure logic (unit-tested) ────────────────────────────────────────


def parse_commit(subject: str, body: str = "") -> dict | None:
    """Parse a commit's subject line into a structured dict, or
    return ``None`` if the subject doesn't match the conventional
    commit shape.  Body is scanned for ``BREAKING CHANGE:`` so a
    footer-only breaking marker still bumps major."""
    m = _CC_RE.match(subject.strip())
    if not m:
        return None
    breaking = bool(m.group("breaking")) or "BREAKING CHANGE:" in body
    return {
        "type": m.group("type"),
        "scope": (m.group("scope") or "").strip(),
        "breaking": breaking,
        "subject": m.group("subject").strip(),
    }


def predict_bump(commits: list[dict]) -> str:
    """Return ``'major'`` / ``'minor'`` / ``'patch'`` based on the
    set of commits.  Empty list → ``'patch'`` (preview still wants
    a meaningful version even if only chore/docs landed)."""
    if any(c["breaking"] for c in commits):
        return "major"
    if any(c["type"] == "feat" for c in commits):
        return "minor"
    if any(c["type"] in ("fix", "perf") for c in commits):
        return "patch"
    return "patch"


def bump_version(current: str, kind: str) -> str:
    """``1.48.0`` + ``minor`` → ``1.49.0``.  Minor/major bumps
    reset the lower fields to 0 — same rules as release-please."""
    major, minor, patch = (int(x) for x in current.split("."))
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def render_section(commits: list[dict], pending_version: str,
                   short_sha: str, today: str) -> str:
    """Render the pending-release section as markdown.  Returns
    the section text (trailing blank line included so callers can
    concatenate raw)."""
    lines = [
        f"## [{pending_version}] — UNRELEASED "
        f"(built {today} from {short_sha})",
        "",
    ]
    rendered_any = False
    for type_key, label in _SECTIONS:
        bucket = [c for c in commits if c["type"] == type_key]
        if not bucket:
            continue
        rendered_any = True
        lines.append(f"### {label}")
        lines.append("")
        for c in bucket:
            scope = f"**{c['scope']}:** " if c["scope"] else ""
            lines.append(f"* {scope}{c['subject']}")
        lines.append("")
    if not rendered_any:
        lines.append("_No user-visible changes since the last release._")
        lines.append("")
    return "\n".join(lines) + "\n"


# ── git plumbing (light I/O wrappers) ──────────────────────────────


def read_current_version() -> str:
    """Current released version from release-please's manifest."""
    return json.loads(
        (ROOT / ".release-please-manifest.json").read_text()
    )["."]


def _git(*args: str) -> str:
    """Run a git command and decode stdout as UTF-8.  Explicit
    encoding kwarg matters on Windows — the default text-mode
    decode uses cp1252 and choke-dies on emoji / em-dashes in
    commit messages (Windows-only bug, but the script needs to
    work in local smoke tests too)."""
    r = subprocess.run(
        ["git", *args],
        cwd=ROOT, capture_output=True, check=True,
        encoding="utf-8", errors="replace",
    )
    return r.stdout


def last_release_tag() -> str | None:
    """Most recent ``v*.*.*`` tag, or ``None`` if there isn't one."""
    try:
        return _git("describe", "--tags", "--abbrev=0",
                    "--match=v*.*.*").strip() or None
    except subprocess.CalledProcessError:
        return None


def commits_since(tag: str | None) -> list[dict]:
    """Walk git log from ``<tag>..HEAD`` (or the full HEAD history
    when tag is None), filtering to conventional commits only."""
    rng = f"{tag}..HEAD" if tag else "HEAD"
    fmt = "%H%x1f%s%x1f%b%x1e"
    raw_log = _git("log", rng, f"--format={fmt}")
    out: list[dict] = []
    for raw in raw_log.split("\x1e"):
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split("\x1f", 2)
        if len(parts) < 2:
            continue
        subject = parts[1]
        body = parts[2] if len(parts) > 2 else ""
        parsed = parse_commit(subject, body)
        if parsed is not None:
            out.append(parsed)
    return out


# ── Entry point ────────────────────────────────────────────────────


def main() -> None:
    current = read_current_version()
    tag = last_release_tag()
    short_sha = os.environ.get("GITHUB_SHA", "")[:7] or "local"
    today = date.today().isoformat()

    commits = commits_since(tag) if tag else []
    bump = predict_bump(commits)
    next_version = bump_version(current, bump)
    pending_version = f"{next_version}-dev.{short_sha}"
    markdown = render_section(commits, pending_version, short_sha, today)
    json.dump({"version": pending_version, "markdown": markdown},
              sys.stdout)


if __name__ == "__main__":
    main()
