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
        print("memoratum: MEMORATUM_API_KEY unset — auth disabled (local dev mode)")
    uvicorn.run(create_app(settings), host="127.0.0.1", port=6767)


if __name__ == "__main__":
    main()
