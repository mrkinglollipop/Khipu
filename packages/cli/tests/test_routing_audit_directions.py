"""Offline regressions for the routing direction audit; fabricated owner rows only."""
import json

import pytest

from khipu import owner_turns, routing, routing_directions as d


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(owner_turns, "occurrence_state", lambda session: {"counts": {"sha": 2}})
    monkeypatch.setattr(routing, "accounts", lambda: [
        {"key": "claude|/synthetic/main", "label": "Claude"},
        {"key": "claude|/synthetic/second", "label": "Claude second"},
        {"key": "claude|/synthetic/glm", "label": "GLM", "id": "glm:primary"},
    ])
    monkeypatch.setattr(routing, "_entry_definitions", lambda: ({}, {}))


def row(**updates):
    return dict(id="rd-audit", session="synthetic", prompt_sha256="sha", occurrence=1,
                words="Use GLM only one job at a time.", status="open", at=1792756800,
                target={"entity": "GLM", "kind": "concurrency", "value": 1}, **updates)


def refresh(item, **updates):
    d._save({"directions": {item["id"]: item}})
    action = dict(id="action", session="synthetic", prompt_sha256="sha", occurrence=1,
                  state="applied", action="route.cap.set", target="GLM",
                  facts={"kind": "concurrency", "n": 1})
    action.update(updates)
    return d.refresh([action])


@pytest.mark.parametrize("account,entity,expected", [
    ("claude|/synthetic/second", "Claude", "open"),
    ("claude|/synthetic/second", "Claude second", "closed"),
    ("claude|/synthetic/glm", "GLM", "closed"),
    ("glm:primary", "GLM", "closed"),
    ("claude|/unrecognized/home", "Claude", "open"),
])
def test_full_account_identity_and_aliases(world, account, entity, expected):
    item = row()
    item["target"]["entity"] = entity
    assert refresh(item, target=account)[0]["status"] == expected


@pytest.mark.parametrize("days,expected", [(1, "open"), (7, "closed"), (None, "open")])
def test_weekly_spend_requires_weekly_period(world, days, expected):
    found, = d.extract("Keep GLM spend to $35 a week.")
    item = row()
    item["target"] = found["target"]
    assert refresh(item, facts={"kind": "spend", "usd": 35, "days": days})[0]["status"] == expected
    assert item["target"]["days"] == 7


@pytest.mark.parametrize("words", ["Keep GLM spend to $35.", "Keep GLM budget to $35 a month."])
def test_ambiguous_spend_period_stays_open_even_when_cited(world, words):
    found, = d.extract(words)
    item = row()
    item["target"] = found["target"]
    assert refresh(item, facts={"kind": "spend", "usd": 35, "days": 30,
                                "direction": item["id"]})[0]["status"] == "open"


def test_different_spend_scope_stays_open(world):
    item = row()
    item["target"] = d.extract("Keep GLM spend to $35 a week.")[0]["target"]
    assert refresh(item, facts={"kind": "spend", "usd": 35, "days": 7,
                                "purpose": "read"})[0]["status"] == "open"


def test_removal_and_unrelated_actions_cannot_fulfill_setting(world):
    for action in ("route.cap.remove", "route.rule.add"):
        assert refresh(row(), action=action)[0]["status"] == "open"


def test_citation_does_not_override_structured_value_or_identity(world):
    for target, n in (("GLM", 9), ("Claude", 1)):
        assert refresh(row(), target=target, facts={"kind": "concurrency", "n": n,
                       "direction": "rd-audit"})[0]["status"] == "open"
    item = row()
    item["target"] = {"entity": "GLM", "kind": None, "value": None}
    assert refresh(item, action="route.rule.add", facts={"direction": "rd-audit"})[0]["status"] == "closed"


@pytest.mark.parametrize("damage", ["occurrence", "prompt_sha256", "nan", "infinity", "range", "huge"])
def test_damaged_rows_diagnosed_skipped_and_preserved(world, damage):
    item = row()
    if damage in ("occurrence", "prompt_sha256"):
        del item[damage]
    else:
        item["at"] = {"nan": float("nan"), "infinity": float("inf"), "range": 10**20,
                      "huge": 10**1000}[damage]
    d._save({"directions": {item["id"]: item}})
    before = json.loads(d.store_path().read_text())
    assert d.malformed_rows() == [{"key": item["id"], "reason": "not a valid direction row"}]
    assert d.refresh([{"session": "synthetic", "prompt_sha256": "sha", "occurrence": 1}]) == []
    assert d.owed_items() == []
    # Malformed rows remain available for owner recovery, including nonstandard JSON NaN.
    assert json.dumps(json.loads(d.store_path().read_text()), sort_keys=True) == json.dumps(before, sort_keys=True)


def test_each_direction_uses_its_own_project(world, monkeypatch):
    first, second = row(), row()
    first.update(id="rd-first", project="project-a")
    second.update(id="rd-second", project="project-b", occurrence=2)
    d._save({"directions": {first["id"]: first, second["id"]: second}})
    assert d.pushed_slice_ids("project-a") == ["rd-first"]
    assert d.pushed_slice_ids("project-b") == ["rd-second"]
    assert d.pushed_slice_ids("project-a", job=True) == []
    first.pop("project")
    second.pop("project")
    second["prompt_sha256"] = "other-sha"
    monkeypatch.setattr(d, "_project_for", lambda session, sha: "project-a" if sha == "sha" else "project-b")
    d._save({"directions": {first["id"]: first, second["id"]: second}})
    assert d.pushed_slice_ids("project-a") == ["rd-first"]
    assert d.pushed_slice_ids("project-b") == ["rd-second"]
