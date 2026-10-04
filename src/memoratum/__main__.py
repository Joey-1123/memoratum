# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`python -m memoratum`: serve the API."""

from __future__ import annotations

import uvicorn

from memoratum.app import create_app
from memoratum.config import Settings


def main() -> None:
    settings = Settings.load()
    if not settings.auth_enabled:
        print(
            "WARNING: MEMORATUM_API_KEY unset — server is OPEN (all tags readable/writable). Set it for any shared host."
        )
    # MEMORATUM_HOST defaults to loopback so a bare-metal install is not
    # accidentally exposed. The container image sets 0.0.0.0, because binding
    # loopback inside a container makes a published port unreachable.
    uvicorn.run(create_app(settings), host=settings.host, port=6767)


if __name__ == "__main__":
    main()
