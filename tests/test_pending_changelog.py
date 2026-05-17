"""Unit tests for tools/pending_changelog.py.

The script's pure functions — parse_commit, predict_bump,
bump_version, render_section — are tested here.  The git/IO
wrappers (last_release_tag, commits_since, read_current_version)
are thin enough that an integration test in build.yml itself
covers them.

Why the pure-logic split: the rendered preview shape is the part
operators look at; misclassifying a commit (feat reading as fix)
or rendering a busted markdown header is more likely to confuse a
human than a git plumbing bug, so that's where pinned tests pay
the most.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import pending_changelog as pc  # noqa: E402


# ── parse_commit ───────────────────────────────────────────────────


def test_parse_commit_plain_feat():
    r = pc.parse_commit("feat: add login")
    assert r == {
        "type": "feat", "scope": "", "breaking": False,
        "subject": "add login",
    }


def test_parse_commit_with_scope():
    r = pc.parse_commit("fix(billing): charge twice on retry")
    assert r["type"] == "fix"
    assert r["scope"] == "billing"
    assert r["breaking"] is False


def test_parse_commit_breaking_marker():
    """``feat!:`` flips breaking even with no BREAKING CHANGE footer."""
    r = pc.parse_commit("feat!: drop /api/v0")
    assert r["breaking"] is True


def test_parse_commit_breaking_footer():
    """A BREAKING CHANGE footer in the body flips breaking even
    without a ``!`` on the header line."""
    r = pc.parse_commit(
        "refactor: simplify token format",
        body="\nBREAKING CHANGE: tokens shorter, old ones rejected\n",
    )
    assert r["breaking"] is True


def test_parse_commit_rejects_non_conventional():
    """Free-form commit messages return None so the renderer
    silently drops them (no garbage section entries)."""
    assert pc.parse_commit("wip") is None
    assert pc.parse_commit("Merge pull request #42 from foo/bar") is None
    assert pc.parse_commit("") is None


def test_parse_commit_trims_subject_whitespace():
    r = pc.parse_commit("feat:    leading spaces  ")
    assert r["subject"] == "leading spaces"


# ── predict_bump ───────────────────────────────────────────────────


def _commit(type_, breaking=False, scope="", subject="x"):
    return {"type": type_, "scope": scope, "breaking": breaking,
            "subject": subject}


def test_predict_bump_breaking_wins():
    """One breaking among many feats still bumps major."""
    commits = [
        _commit("feat"), _commit("fix"),
        _commit("feat", breaking=True),
    ]
    assert pc.predict_bump(commits) == "major"


def test_predict_bump_feat_promotes_to_minor():
    commits = [_commit("fix"), _commit("feat"), _commit("chore")]
    assert pc.predict_bump(commits) == "minor"


def test_predict_bump_fix_or_perf_is_patch():
    assert pc.predict_bump([_commit("fix")]) == "patch"
    assert pc.predict_bump([_commit("perf")]) == "patch"


def test_predict_bump_only_chore_falls_back_to_patch():
    """When nothing bumpable landed, still return a patch — the
    preview wants a real version to render."""
    assert pc.predict_bump([_commit("chore"), _commit("docs")]) == "patch"


def test_predict_bump_empty_list_is_patch():
    assert pc.predict_bump([]) == "patch"


# ── bump_version ───────────────────────────────────────────────────


def test_bump_version_major_resets_lower():
    assert pc.bump_version("1.48.3", "major") == "2.0.0"


def test_bump_version_minor_resets_patch():
    assert pc.bump_version("1.48.3", "minor") == "1.49.0"


def test_bump_version_patch_increments_only_patch():
    assert pc.bump_version("1.48.3", "patch") == "1.48.4"


# ── render_section ─────────────────────────────────────────────────


def test_render_section_groups_by_type_with_scope_bolded():
    """The rendered section mirrors release-please's Node config:
    grouped headers, scope as bold prefix, types ordered Features
    → Bug Fixes → Performance Improvements → Reverts."""
    commits = [
        _commit("fix", scope="auth", subject="bearer leak"),
        _commit("feat", subject="dark mode"),
        _commit("feat", scope="ui", subject="new sidebar"),
        _commit("perf", subject="trim startup"),
    ]
    md = pc.render_section(commits, "1.49.0-dev.abc1234",
                           "abc1234", "2026-05-17")
    # Header line carries version, build date, sha.
    assert md.startswith(
        "## [1.49.0-dev.abc1234] — UNRELEASED "
        "(built 2026-05-17 from abc1234)"
    )
    # Section ordering: feat first, then fix, then perf.
    feat_idx = md.index("### Features")
    fix_idx = md.index("### Bug Fixes")
    perf_idx = md.index("### Performance Improvements")
    assert feat_idx < fix_idx < perf_idx
    # Scope is bolded; no-scope commits don't get a leading prefix.
    assert "* **ui:** new sidebar" in md
    assert "* dark mode" in md
    assert "* **auth:** bearer leak" in md
    assert "* trim startup" in md


def test_render_section_hides_chore_docs_etc():
    """Commits outside the visible types don't render — same
    behaviour as release-please's actual Release PR body."""
    commits = [
        _commit("chore", subject="bump deps"),
        _commit("docs", subject="readme tweaks"),
        _commit("ci", subject="cache pip"),
    ]
    md = pc.render_section(commits, "1.48.1-dev.xyz",
                           "xyz", "2026-05-17")
    # No headers rendered + the "no user-visible changes" fallback fires.
    assert "### Features" not in md
    assert "### Bug Fixes" not in md
    assert "_No user-visible changes since the last release._" in md


def test_render_section_empty_commits_renders_fallback():
    """No commits at all → fallback copy so the markdown isn't a
    bare header with nothing under it."""
    md = pc.render_section([], "1.48.1-dev.xyz",
                           "xyz", "2026-05-17")
    assert "_No user-visible changes since the last release._" in md


def test_render_section_skips_empty_buckets():
    """Mixed commits — perf-only with no feat/fix shouldn't render
    empty Features / Bug Fixes headers above the Performance one."""
    commits = [_commit("perf", subject="cache hits")]
    md = pc.render_section(commits, "1.48.1-dev.xyz",
                           "xyz", "2026-05-17")
    assert "### Features" not in md
    assert "### Bug Fixes" not in md
    assert "### Performance Improvements" in md
    assert "* cache hits" in md
