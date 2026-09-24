# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Identity-provider seam for enterprise OIDC integrations.

The server does not parse or verify third-party identity tokens itself. An
embedding application supplies a verifier that validates issuer, audience,
signature, expiry, and claims, then returns a narrow local scope.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Identity:
    subject: str
    container_tag: str | None = None
    org_id: str | None = None
    scopes: frozenset[str] = frozenset()


class IdentityProvider(Protocol):
    def verify(self, token: str) -> Identity | None: ...


class StaticIdentityProvider:
    """Deterministic test/development provider; never enable in production."""

    def __init__(self, identities: dict[str, Identity]) -> None:
        self.identities = dict(identities)

    def verify(self, token: str) -> Identity | None:
        return self.identities.get(token)


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
