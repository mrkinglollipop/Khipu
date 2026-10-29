"""Pure routing wording regressions; run with --noconftest and a temporary HOME."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


NOW = datetime(2026, 10, 25, 14, tzinfo=timezone.utc)


@pytest.fixture
def routing_module(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("KHIPU_DATABASE_URL", raising=False)
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")

    def forbidden(*args, **kwargs):
        raise AssertionError("wording tests must not read a database or usage source")

    import psycopg
    monkeypatch.setattr(psycopg, "connect", forbidden)
    from khipu import routing, usage
    monkeypatch.setattr(usage, "probe_all", forbidden)
    monkeypatch.setattr(routing, "read_view", forbidden)
    monkeypatch.setattr(routing, "_agent_field", lambda value, fallback="", **kwargs: value or fallback)
    return routing


def event(kind, index, **fields):
    return {"type": kind, "id": f"wording-{index}", "seq": index, "_seq": index,
            "written_at": "2026-10-25T13:00:00Z", "written_by": {"kind": "owner"},
            "via": "app", **fields}


@pytest.mark.parametrize("kind,keys", [
    ("provider", "id, label"),
    ("account", "id, label, provider"),
    ("lane_map", "runner, lane, account"),
])
def test_malformed_definition_names_its_kind_and_required_keys(routing_module, kind, keys):
    args = SimpleNamespace(definition=kind, value=json.dumps({}))
    with pytest.raises(routing_module.RouteError) as exc:
        routing_module._route_intent("definition", args)
    assert str(exc.value) == f"invalid {kind} definition; required keys: {keys}"


@pytest.mark.parametrize("facts", [None, {}])
def test_owner_account_without_reader_remains_tracked_in_priority_and_changes(routing_module, facts):
    # Not Kimi: Khipu reads Kimi itself once its key is stored.
    r = routing_module
    account = {"id": "mistral:main", "label": "Mistral", "provider": "mistral"}
    rows = [event("definition_set", 1, definition="account", value=account),
            event("priority_set", 2, entries=[{"account": "mistral:main", "label": "Mistral"}])]
    view = r.fold(rows, now=NOW, cfg=r.settings({}), known=[])
    entry = next(a for a in view["accounts"] if a["key"] == "mistral:main")
    assert entry["configured"] is False  # A reader is still unavailable.
    assert view["priority"]["entries"][0]["in_settings"] is True
    account_line = r.account_line(entry, NOW, view=view)
    change_line = r.change_line("account:mistral:main", view, NOW)
    priority_line = r.priority_line(view["priority"], NOW, view=view, facts=facts)
    assert "no usage reader set up" in account_line
    assert "no usage reader set up" in change_line
    assert priority_line.count("no usage reader set up") == 1
    assert "no longer tracked" not in account_line + change_line + priority_line


@pytest.mark.parametrize("known", [[], [{"key": "codex|", "label": "Codex", "harness": "codex"}]])
def test_removed_account_retains_no_longer_tracked_wording(routing_module, known):
    r = routing_module
    account = {"id": "kimi:main", "label": "Kimi", "provider": "kimi"}
    rows = [event("definition_set", 1, definition="account", value=account),
            event("priority_set", 2, entries=[{"account": "kimi:main", "label": "Kimi"}]),
            event("definition_remove", 3, definition="account", value=account)]
    view = r.fold(rows, now=NOW, cfg=r.settings({}), known=known)
    assert all(a["key"] != "kimi:main" for a in view["accounts"])
    assert view["priority"]["entries"][0]["in_settings"] is False
    assert "(no longer tracked)" in r.priority_line(view["priority"], NOW, view=view)
    assert r.change_line("account:kimi:main", view, NOW, {
        "account:kimi:main": {"label": "Kimi"}}) == "Kimi: no longer tracked"
    # The other two wording paths require an explicit Stop tracking note.
    stopped = {"key": "kimi:main", "label": "Kimi", "state": r.UNKNOWN,
               "configured": False, "note": ["untracked", "2026-10-25T13:00:00Z"]}
    view["accounts"] = [stopped]
    assert r.account_line(stopped, NOW, view=view).endswith("; no longer tracked")
    assert r.change_line("account:kimi:main", view, NOW) == "Kimi: no longer tracked"
