"""Shared pytest fixtures for the khipu test suite.

``khipu.db.table_columns`` (fix 13 consolidation) keeps a per-process cache
keyed by table name so repeated schema checks (embed/drift/hub_snapshot)
share one information_schema round trip. That is correct in a real process
but poisons test isolation: two test methods that fake different schema
shapes for the same table (e.g. "pre-migration episodes" vs "post-migration
episodes") would otherwise see whichever one ran first. Clear it before
every test so each test's fake cursor is the sole source of truth.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_table_columns_cache():
    from khipu import db

    db._TABLE_COLUMNS_CACHE.clear()
    yield
    db._TABLE_COLUMNS_CACHE.clear()
