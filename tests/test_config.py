"""Config loads from env (RED)."""

import os


def _get(key: str, default: str) -> str:
    return os.environ.get(key, default)


def test_data_dir_defaults_under_cwd() -> None:
    os.environ.pop("MEMORATUM_DATA_DIR", None)
    from memoratum.config import Settings

    s = Settings.load()
    assert s.data_dir.endswith(".memoratum-data")


def test_env_overrides() -> None:
    os.environ["MEMORATUM_DATA_DIR"] = "/tmp/x"
    os.environ["MEMORATUM_API_KEY"] = "k"
    os.environ["MEMORATUM_EMBEDDINGS_PROVIDER"] = "api"
    try:
        from memoratum.config import Settings

        s = Settings.load()
        assert s.data_dir == "/tmp/x"
        assert s.api_key == "k"
        assert s.embeddings_provider == "api"
    finally:
        for k in ("MEMORATUM_DATA_DIR", "MEMORATUM_API_KEY", "MEMORATUM_EMBEDDINGS_PROVIDER"):
            os.environ.pop(k, None)
