#!/usr/bin/env python3
"""sync-env.py — non-destructively add missing keys from .env.example to .env.

When ``.env.example`` grows new env vars (a new feature lands, a guard
gets configurable, etc.) the operator's existing ``.env`` falls behind.
Re-copying the example over the top would wipe their actual values.
This script walks the example, finds every ``KEY=value`` assignment that
isn't already in ``.env``, and appends it — along with the comment block
that precedes it in the example — to the end of ``.env``.

* Existing values in ``.env`` are never touched.
* Re-running after a sync is a no-op (idempotent).
* Output is grouped under a dated header so you can spot the additions.

Usage::

    python3 deploy/sync-env.py
    python3 deploy/sync-env.py --example .env.example --env .env
    python3 deploy/sync-env.py --dry-run    # show what would change

Run on the EC2 host from the ``deploy/`` directory after a ``git pull``,
then ``$EDITOR .env`` to fill in any new placeholders the example added.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Match the conventional ENV_VAR_NAME=... assignment.  We don't try to
# parse the value side; we only need to know which keys exist.
_KEY_RE = re.compile(r"^([A-Z_][A-Z_0-9]*)=")


def _parse_existing_keys(path: Path) -> set[str]:
    """Return the set of KEY names already declared in ``path``."""
    if not path.exists():
        return set()
    keys: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        m = _KEY_RE.match(raw.strip())
        if m:
            keys.add(m.group(1))
    return keys


def _parse_example(path: Path) -> list[tuple[list[str], str, str]]:
    """Walk ``path`` once and return ``[(comments, key, line), ...]``.

    ``comments`` is the comment + blank-line block that precedes the
    ``KEY=value`` line in the example file.  We carry it across so the
    operator gets the context comment, not just a naked assignment, when
    the missing key gets appended to their ``.env``.
    """
    entries: list[tuple[list[str], str, str]] = []
    buffer: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            buffer.append(line)
            continue
        m = _KEY_RE.match(line)
        if m is not None:
            entries.append((list(buffer), m.group(1), line))
            buffer.clear()
        else:
            # A non-KEY=value, non-comment line resets the buffer — we
            # don't want to attach an unrelated comment block to the
            # next assignment.
            buffer.clear()
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--example", default=".env.example",
        help="Source file to diff against (default: .env.example).",
    )
    parser.add_argument(
        "--env", default=".env",
        help="Target file to update (default: .env).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print missing keys but don't modify the target file.",
    )
    args = parser.parse_args()

    example = Path(args.example)
    env = Path(args.env)

    if not example.exists():
        print(f"error: {example} not found", file=sys.stderr)
        return 2

    if not env.exists():
        if args.dry_run:
            print(f"{env} doesn't exist — would copy {example}.")
            return 0
        env.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"{env} didn't exist — copied from {example}.")
        return 0

    existing = _parse_existing_keys(env)
    entries = _parse_example(example)
    missing = [(c, k, l) for c, k, l in entries if k not in existing]

    if not missing:
        print(f"{env} is up to date — no missing keys.")
        return 0

    print(f"Found {len(missing)} key(s) in {example} missing from {env}:")
    for _, key, _ in missing:
        print(f"  {key}")

    if args.dry_run:
        print("\n(dry run — pass without --dry-run to apply.)")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_lines: list[str] = [
        "",
        f"# == Added by sync-env.py on {stamp} ==",
        "# Review each block + fill in any placeholders before restarting.",
    ]
    for comments, _key, assignment in missing:
        # Drop leading blank-only lines from the buffered comments so
        # appended blocks don't double-space themselves.
        while comments and not comments[0].strip():
            comments.pop(0)
        new_lines.append("")
        new_lines.extend(comments)
        new_lines.append(assignment)

    with env.open("a", encoding="utf-8") as f:
        f.write("\n".join(new_lines) + "\n")

    print(f"\nAppended {len(missing)} block(s) to {env}.")
    print(f"Open {env} to review the new entries.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
