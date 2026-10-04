"""Shared pytest fixtures.

Deliberately NOT autouse: the existing suite calls ``helpers.use_tmp_data_dir()``
explicitly per test, and turning these on globally would change the data directory
under 79 already-passing tests.
"""

import pytest
from helpers import frozen_time, use_tmp_data_dir


@pytest.fixture
def temp_data_dir():
    """Point MEMORATUM_DATA_DIR at a fresh temp dir for one test."""
    directory = use_tmp_data_dir()
    yield directory


@pytest.fixture
def clock():
    """Deterministic epoch time for lease assertions; mutate the list to advance."""
    with frozen_time() as active:
        yield active
