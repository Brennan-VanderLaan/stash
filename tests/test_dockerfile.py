"""Guard against the recurring "forgot to COPY the new module"
regression.

Background: vault.py (phase 2), dao/ (phase 3), and obs.py (phase
16) each shipped functioning code that imported fine in tests but
broke the production container because the Dockerfile's COPY line
listed each top-level Python file by name and the new module
wasn't on that list.  Tests don't catch this because they import
from the source tree, not the built image.

This guard parses the Dockerfile, walks the repo's top-level
Python files + first-class package directories, and asserts each
one is referenced by at least one COPY directive.  When a future
phase adds a new top-level module it has to land in the COPY line
or this test fails.

The guard intentionally lives outside the Docker build (no docker
daemon needed in CI) so it costs ~0ms and runs locally on every
``pytest tests/``.
"""

from __future__ import annotations

import re
from pathlib import Path


# Top-level Python files that *don't* belong in the runtime image.
# Tests + their fixtures, build scripts, this guard itself.  Add
# new exclusions here with a one-line justification when a future
# helper script lands.
_EXEMPT_FILES = {
    "conftest.py",  # pytest top-level config; not a runtime module.
}

# Top-level directories that are part of the runtime image but
# explicitly captured by their own COPY directive (or otherwise
# don't carry runtime Python).  Exempting them here lets the
# walker focus on first-class Python packages.
_EXEMPT_DIRS = {
    "tests",     # test suite, not shipped.
    "docs",      # markdown only.
    "deploy",    # docker-compose + Caddyfile, not runtime image.
    "locale",    # gettext catalogs, optional + small.
    "scripts",   # one-off ops scripts.
    ".venv", "venv", ".git", ".github", ".pytest_cache",
    "__pycache__", "uploads",
}


def _read_dockerfile() -> str:
    return (
        Path(__file__).resolve().parent.parent / "Dockerfile"
    ).read_text(encoding="utf-8")


def _copy_directives(dockerfile_text: str) -> list[str]:
    """Return the right-hand side (sources) of every ``COPY``
    directive in the Dockerfile, lower-cased + stripped.  We only
    care about which paths the build pulls in, not where they
    land in the image."""
    copies: list[str] = []
    for line in dockerfile_text.splitlines():
        m = re.match(r"^\s*COPY\s+(.*)$", line, flags=re.IGNORECASE)
        if not m:
            continue
        # COPY <src...> <dest> — drop the trailing dest.
        parts = m.group(1).split()
        if len(parts) < 2:
            continue
        copies.extend(p.lower() for p in parts[:-1])
    return copies


def test_every_top_level_python_module_is_copied_into_image():
    """Each ``*.py`` in the repo root must be referenced by a
    Dockerfile COPY directive.  When the test fails, fix it by
    adding the new file to the COPY line in Dockerfile.

    The error message names the file so the next contributor
    doesn't have to read this docstring to figure out what
    happened."""
    repo = Path(__file__).resolve().parent.parent
    copies = _copy_directives(_read_dockerfile())

    missing: list[str] = []
    for py in repo.glob("*.py"):
        if py.name in _EXEMPT_FILES:
            continue
        if py.name.lower() not in copies:
            missing.append(py.name)
    assert not missing, (
        "Top-level Python files missing from the Dockerfile's COPY "
        "directives — production container will fail to start with "
        f"ModuleNotFoundError: {missing}.  Add them to the COPY "
        "line in Dockerfile (alongside app.py / vault.py / obs.py / "
        "etc.) and re-run."
    )


def test_first_class_packages_are_copied_into_image():
    """Same idea for top-level package directories that hold
    runtime code (dao/, templates/, static/).  The COPY entry
    is typically ``COPY <dir>/ ./<dir>/`` so we look for either
    ``<dir>`` or ``<dir>/`` in the source list."""
    repo = Path(__file__).resolve().parent.parent
    copies = {c.rstrip("/").lower() for c in _copy_directives(_read_dockerfile())}

    missing: list[str] = []
    for entry in repo.iterdir():
        if not entry.is_dir():
            continue
        if entry.name in _EXEMPT_DIRS:
            continue
        # Heuristic: a runtime package has at least one .py file.
        # Asset dirs (templates/, static/) get caught the same way
        # but are still required by the runtime — leave them in.
        has_py = any(p.suffix == ".py" for p in entry.rglob("*"))
        is_template_or_static = entry.name in {"templates", "static"}
        if not (has_py or is_template_or_static):
            continue
        if entry.name.lower() not in copies:
            missing.append(entry.name)
    assert not missing, (
        "Top-level runtime directories missing from the Dockerfile's "
        f"COPY directives: {missing}.  Add a ``COPY {missing[0]}/ "
        f"./{missing[0]}/`` line."
    )
