#!/usr/bin/env python
"""Fail CI when test-suppression pragmas are added.

The measure-and-hold coverage policy is only meaningful if the denominator cannot
be quietly trimmed. `# pragma: no cover` and bare `pytest.skip` both do that, so
adding either is treated as a change that needs to be deliberate and visible.

Counting is a ratchet, not a ban: the total may go down freely, and removing
suppressions is always welcome.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

NO_COVER = re.compile(r"#\s*pragma:\s*no\s+cover")
# `skip_unless(...)` is exempt: it declines to run only when an external tool is
# absent, so it cannot mask a failing test. Raw pytest.skip/xfail is counted.
SKIP = re.compile(r"(?<!skip_unless\()\bpytest\.(?:skip|xfail)\(")
SOURCE_ROOTS = ("src", "tests", "clients", "dashboard/src")

# This guard's own test module necessarily contains both patterns as fixtures, so
# counting it would make the guard fail on the commit that introduces it.
EXCLUDED = {"tests/test_ci_integrity.py"}


def _count_in_text(text: str) -> tuple[int, int]:
    """Count suppressions, ignoring occurrences inside the guard's own module."""
    return len(NO_COVER.findall(text)), len(SKIP.findall(text))


def count(ref: str | None) -> tuple[int, int] | None:
    """Return (no_cover, skip) totals at `ref`, or None when it cannot be read.

    Every root is scanned and the totals summed. Returning after the first
    existing root -- which an earlier version did -- silently under-counted, so
    a skip added under tests/ would never have been caught.
    """
    no_cover = skip = 0
    scanned = False
    for root in SOURCE_ROOTS:
        if ref is None:
            if not Path(root).exists():
                continue
            files = [p for p in Path(root).rglob("*") if p.is_file()]
        else:
            listing = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", ref, "--", root],
                capture_output=True,
                text=True,
                check=False,
            )
            if listing.returncode != 0:
                continue
            files = [Path(name) for name in listing.stdout.splitlines() if name]
        if not files:
            continue
        scanned = True
        for path in files:
            if path.suffix not in {".py", ".js", ".ts", ".tsx"}:
                continue
            if path.as_posix() in EXCLUDED:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            no_cover += len(NO_COVER.findall(text))
            skip += len(SKIP.findall(text))
    return (no_cover, skip) if scanned else None


def merge_base(ref: str = "origin/main") -> str | None:
    for candidate in (ref, "main", "master"):
        result = subprocess.run(
            ["git", "merge-base", "HEAD", candidate],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main")
    args = parser.parse_args()

    current = count(None)
    if current is None:
        print("could not count suppressions in the working tree", file=sys.stderr)
        return 1
    now_no_cover, now_skip = current

    base = merge_base(args.base)
    baseline = count(base) if base else None
    if baseline is None:
        print("no baseline available; treating this as the first ratchet")
        return 0
    was_no_cover, was_skip = baseline

    print(
        f"suppressions: no_cover {was_no_cover} -> {now_no_cover}, skips {was_skip} -> {now_skip}"
    )

    added = []
    if now_no_cover > was_no_cover:
        added.append(f"{now_no_cover - was_no_cover} new 'pragma: no cover'")
    if now_skip > was_skip:
        added.append(f"{now_skip - was_skip} new pytest.skip/xfail")

    if added:
        print(
            "test suppressions were ADDED: " + "; ".join(added) + "\n"
            "Removing them is always fine. Adding them needs a stated reason in the\n"
            "pull request, because each one hides code from the coverage floor.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
