# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Environment-based settings. No hardcoded paths, keys, or endpoints."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _get(key: str, default: str) -> str:
    return os.environ.get(key, default)


@dataclass(frozen=True)
class Settings:
    data_dir: str
    api_key: str
    embeddings_provider: str
    embeddings_endpoint: str
    embeddings_model: str
    embeddings_dims: int
    llm_endpoint: str
    llm_model: str

    @classmethod
    def load(cls) -> Settings:
        try:
            dims = int(_get("MEMORATUM_EMBEDDINGS_DIMS", "0") or 0)
        except ValueError:
            raise ValueError("MEMORATUM_EMBEDDINGS_DIMS must be an integer") from None
        return cls(
            data_dir=_get("MEMORATUM_DATA_DIR", os.path.join(os.getcwd(), ".memoratum-data")),
            api_key=_get("MEMORATUM_API_KEY", ""),
            embeddings_provider=_get("MEMORATUM_EMBEDDINGS_PROVIDER", "hash"),
            embeddings_endpoint=_get("MEMORATUM_EMBEDDINGS_ENDPOINT", ""),
            embeddings_model=_get("MEMORATUM_EMBEDDINGS_MODEL", ""),
            embeddings_dims=dims,
            llm_endpoint=_get("MEMORATUM_LLM_ENDPOINT", ""),
            llm_model=_get("MEMORATUM_LLM_MODEL", ""),
        )

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "memoratum.db")

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_key)
