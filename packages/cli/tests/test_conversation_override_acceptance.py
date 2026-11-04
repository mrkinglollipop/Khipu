"""Owner acceptance: elapsed reviews and retained history cannot end or revive grants."""
from datetime import datetime, timedelta, timezone

from khipu import conversation_overrides as overrides, routing
from tests.test_owner_direction_e2e import world  # noqa: F401


NOW = datetime(2026, 11, 4, 14, tzinfo=timezone.utc)


def grant(now=NOW, **extra):
    return overrides.grant({"account": "codex|", "effect": "ahead_of_pace",
                            "subject": {"kind": "session", "id": "fixture", "mac": routing._local_mac()},
                            "request": "let this conversation run ahead indefinitely",
                            "indefinite": True, **extra}, now=now)


def test_indefinite_review_does_not_end_override(world, monkeypatch):
    row = grant()
    monkeypatch.setattr(overrides, "subject", lambda *a, **kw: (row["subject"], None))
    advice, refusal = overrides.advice("codex|", now=NOW + timedelta(days=8))
    assert advice is not None and refusal is None
    assert any(item.get("override") == row["id"] for item in overrides.walkthrough(now=NOW + timedelta(days=8))["items"])


def test_retention_keeps_live_indefinite(world):
    first = grant()
    grant(now=NOW + timedelta(days=40), subject={"kind": "session", "id": "other", "mac": routing._local_mac()})
    assert first["id"] in {r["id"] for r in overrides.list_overrides(now=NOW + timedelta(days=40))}


def test_retention_cannot_resurrect_ended_override(world):
    first = grant()
    overrides.end(first["id"], now=NOW + timedelta(hours=1))
    grant(now=NOW + timedelta(days=40), subject={"kind": "session", "id": "other", "mac": routing._local_mac()})
    assert first["id"] not in {r["id"] for r in overrides.list_overrides(now=NOW + timedelta(days=40))}
