"""Fail CI when application code gains an external analytics/telemetry path."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (ROOT / "src", ROOT / "clients", ROOT / "dashboard" / "src")
EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".jsx"}
FORBIDDEN = re.compile(
    r"\b(posthog|sentry[_-]?sdk|mixpanel|amplitude|segment\.io|plausible|datadog)\b"
    r"|opentelemetry\.exporter|google-analytics|googletagmanager",
    re.IGNORECASE,
)


def main() -> int:
    violations: list[str] = []
    for source_root in SOURCE_ROOTS:
        if not source_root.exists():
            continue
        for path in source_root.rglob("*"):
            if path.suffix.lower() not in EXTENSIONS or not path.is_file():
                continue
            for number, line in enumerate(path.read_text(errors="replace").splitlines(), start=1):
                if FORBIDDEN.search(line):
                    violations.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    if violations:
        print("External telemetry/analytics code is not allowed:", file=sys.stderr)
        print("\n".join(violations), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
