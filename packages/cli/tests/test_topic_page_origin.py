"""Topic pages no episode ever carried are owner-written files (like notes):
the forget fence lets them through. A slug with no topic row stays unproven,
and a page any episode carries follows that episode's forget state."""
from __future__ import annotations

import json
import os

import pytest

from khipu import hub_snapshot, provider_text, tombstones


class _Cur:
    def __init__(self, carriers, pages):
        self.carriers, self.pages, self.rows = carriers, pages, []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "JOIN episodes e ON e.topics" in s:
            self.rows = [(slug, eid) for slug, eids in self.carriers.items()
                         if slug in params[0] for eid in eids]
        elif s.startswith("SELECT slug FROM topics WHERE slug = ANY"):
            self.rows = [(slug,) for slug in params[0] if slug in self.pages]
        else:
            self.rows = []

    def fetchall(self):
        return list(self.rows)


@pytest.fixture
def forgotten(tmp_path, monkeypatch):
    reg = tmp_path / "forgotten_sources.json"
    reg.write_text(json.dumps({"version": 1, "source_keys": [], "payload_hashes": [],
                               "legacy_identities": [], "episode_ids": [42]}))
    monkeypatch.setattr(tombstones, "registry_path", lambda: reg)
    monkeypatch.setattr(hub_snapshot, "_pending_erasure_ids", lambda *a, **k: set())


def test_uncarried_page_passes_unknown_slug_and_forgotten_carrier_withhold(forgotten):
    cur = _Cur(carriers={"carried-by-forgotten": [42], "carried-live": [7]},
               pages={"owner-page", "carried-by-forgotten", "carried-live"})
    items = [("topic", "owner-page", "A"), ("topic", "no-such-topic", "B"),
             ("topic", "carried-by-forgotten", "C"), ("topic", "carried-live", "D")]
    assert provider_text.fence_chunks(cur, items) == ["A", None, None, "D"]
