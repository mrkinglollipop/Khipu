"""A rejected key (or no plan on it) is definitive for every account Khipu
reads with a stored key (GLM, DeepSeek, Kimi): routing must not stay open on
the last good figures, which stay in the row as stale."""
from __future__ import annotations

import pytest

from khipu import routing_sources, usage

NOW = "2026-10-29T12:00:00Z"
GOOD = [{"name": "weekly", "used_pct": 10.0, "resets_at": "2026-11-02T00:00:00Z", "window_mins": 10080}]


def _row(harness: str, error: str) -> dict:
    return {"harness": harness, "ok": False, "error": error, "last_error": error, "windows": GOOD,
            "last_good_at": "2026-10-29T11:00:00Z", "probed_at": NOW, "last_attempt_at": NOW}


@pytest.mark.parametrize("harness", ["glm", "deepseek", "kimi"])
@pytest.mark.parametrize("error", [usage.E_BAD_KEY, usage.E_NO_PLAN])
def test_a_key_read_account_with_a_rejected_key_is_down(monkeypatch, harness, error):
    key = f"{harness}:main"
    monkeypatch.setattr(usage, "_load_cache", lambda: {key: _row(harness, error)})
    evidence, _ = routing_sources.usage_evidence(None)
    assert [(e["state"], e["reason"]) for e in evidence] == [("down", error)]


def test_a_failed_read_that_is_not_definitive_keeps_the_last_good_figures(monkeypatch):
    monkeypatch.setattr(usage, "_load_cache", lambda: {"glm:main": _row("glm", usage.E_TIMEOUT)})
    evidence, _ = routing_sources.usage_evidence(None)
    assert all(e["state"] != "down" for e in evidence)
