"""Pytest configuration.

Makes ``tests/factories.py`` importable without a package install quirk and
keeps shared fixtures explicit.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


@pytest.fixture()
def consulting_scenario():
    from factories import make_consulting_scenario

    return make_consulting_scenario()


@pytest.fixture()
def contract_result():
    from factories import make_contract_result

    return make_contract_result()
