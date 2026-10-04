#!/usr/bin/env python
"""Fail CI when the coverage gate is weakened.

Constitution Principle III and the measure-and-hold policy in `pyproject.toml`
make `fail_under` a governed value: it may rise in the same pull request that adds
the tests, but it may not fall without a constitution amendment. Configuration must
not be the easy lever.

Compares against the merge-base with the default branch rather than `HEAD~1`, so a
multi-commit branch is judged once against what it actually forked from.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

FAIL_UNDER_RE = re.compile(r"^\s*fail_under\s*=\s*(\d+)", re.MULTILINE)


def current_floor(path: str = "pyproject.toml") -> int | None:
    try:
        with open(path, encoding="utf-8") as handle:
            match = FAIL_UNDER_RE.search(handle.read())
    except OSError:
        return None
    return int(match.group(1)) if match else None


def at_revision(revision: str, path: str = "pyproject.toml") -> int | None:
    try:
        content = subprocess.run(
            ["git", "show", f"{revision}:{path}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if content.returncode != 0:
        return None
    match = FAIL_UNDER_RE.search(content.stdout)
    return int(match.group(1)) if match else None


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
    parser.add_argument("--base", default="origin/main", help="ref to compare against")
    args = parser.parse_args()

    now = current_floor()
    if now is None:
        print("no fail_under found in pyproject.toml; the coverage gate is not enforced")
        return 1

    base = merge_base(args.base)
    if base is None:
        print("could not resolve a merge base; skipping the lowering check")
        return 0

    before = at_revision(base)
    if before is None:
        print(f"no baseline fail_under at {base[:10]}; treating this as the first gate")
        return 0

    if now < before:
        print(
            f"coverage floor was LOWERED: {before} -> {now}.\n"
            "Raising it is fine (do it in the PR that adds the tests). Lowering it"
            " requires a constitution amendment plus a documented reason.",
            file=sys.stderr,
        )
        return 1

    print(f"coverage floor not lowered ({before} -> {now})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
