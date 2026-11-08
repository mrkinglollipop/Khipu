"""Regression reproducer: main must hide forgotten decisions in conflicts too.

Only the repository's throwaway loopback PostgreSQL fixture is used.
"""

import pytest

from khipu import db


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_TEST", "1")
    db._TABLE_COLUMNS_CACHE.clear()


