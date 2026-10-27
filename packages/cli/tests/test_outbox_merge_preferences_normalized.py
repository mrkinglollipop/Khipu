"""``merge-line-preferences`` compares a line the way the hub's writer would
have stored it (K6 scope cleanup, W5.1 topics moved to tags, preferences
split at other commas), on the line side only: those lines merge and archive
as ``preferences_contained_normalized`` with the whole original job kept;
a real difference stays held under ``needs_review``."""
from copy import deepcopy
import json

import pytest

from khipu import outbox, outbox_preferences
from khipu.capture import _legacy_landed_line, _legacy_projection_kind
from khipu.capture_identity import payload_sha256
from tests.test_outbox_landed_lines import HUB, _queue, hub  # noqa: F401 — fixture
from tests.test_outbox_legacy_projection import world  # noqa: F401 — fixture
from tests.test_outbox_merge_preferences import NEW, _direction, _episode, _handles_merge_sql, _line, _md5, _run

PATH_SCOPE = "/Users/someone/Code/Khipu/.claude/worktrees/outbox-prefs"
LONG_SCOPE = "a scope that runs well past the six word limit"


@pytest.fixture
def lines(hub):  # noqa: F811
    """One held line per case, each at its own hub episode."""
    _handles_merge_sql(hub)
    hub.episodes[1]["summary"] = hub.episodes[1]["raw"]["summary"] = "Synthetic episode 1"
    _episode(hub, 2)
    _episode(hub, 3, tags=["claude", "tmp"])
    _episode(hub, 4, preferences=["Prefer short answers", "plain words"])
    _episode(hub, 5)
    _episode(hub, 6, scope="khipu")
    _episode(hub, 7)
    _episode(hub, 8, scope="khipu")
    hub._existing_keys = {(e["ts"], _md5(e["summary"])) for e in hub.episodes.values()}
    hub.next_id = 100
    paths = {
        "scope": _queue(_line(2, scope=PATH_SCOPE, preferences=[*HUB["preferences"], NEW[0]])),
        "tags": _queue(_line(3, scope=LONG_SCOPE, topics=["Outbox", "claude", "tmp"])),
        "comma": _queue(_line(4, preferences=["Prefer short answers, plain words"])),
        "missing_topic": _queue(_line(5, scope=PATH_SCOPE, topics=["outbox", "graph"],
                                      preferences=[*HUB["preferences"], NEW[0]])),
        "other_scope": _queue(_line(6, scope="aegis", preferences=[*HUB["preferences"], NEW[0]])),
        "decisions": _queue(_line(7, scope=PATH_SCOPE, decisions=["Keep the hub", "a decision the hub lacks"],
                                  preferences=[*HUB["preferences"], NEW[0]])),
        "dropped_scope_vs_kept": _queue(_line(8, scope=PATH_SCOPE, preferences=[*HUB["preferences"], NEW[1]])),
    }
    return hub, paths


def _records():
    return {json.loads(p.read_text())["resolution"]["episode_id"]: json.loads(p.read_text())
            for p in outbox.resolved_dir().glob("*.json")}


def test_dry_run_classifies_the_writer_reshaped_lines_and_lists_real_differences(lines):
    cur, _paths = lines
    before = deepcopy(cur.episodes)
    rc, out = _run()
    assert rc == 0 and out["held_lines"] == 7, out
    assert out[outbox_preferences.NORMALIZED_KIND] == 3 and out["needs_review"] == 4, out
    assert out["needs_review_episode_ids"] == [5, 6, 7, 8], out
    assert (out["mergeable"], out["landed"], out["preferences_contained"]) == (0, 0, 0), out
    # Only the scope line needs text added; the topic and comma lines are held already.
    assert out["preferences_to_add"] == 1 and {s["episode_id"] for s in out["samples"]} == {2, 3, 4}
    assert cur.episodes == before


def test_the_drains_exact_check_is_unchanged(lines):
    cur, paths = lines
    for key in ("scope", "tags", "comma"):
        view = outbox_preferences._hub_view(json.loads(paths[key].read_text())["payload"])
        eid = {"scope": 2, "tags": 3, "comma": 4}[key]
        raw = cur.episodes[eid]["raw"]
        assert _legacy_projection_kind(raw, view) is None and not _legacy_landed_line(raw, view), key
    outbox.drain()
    assert all(p.exists() for p in paths.values())
    assert not list(outbox.resolved_dir().glob("*.json"))


def test_apply_merges_and_archives_normalized_lines_and_never_changes_scope_topics_or_tags(lines, tmp_path,
                                                                                           monkeypatch):
    cur, paths = lines
    before = deepcopy(cur.episodes)
    jobs_before = {k: json.loads(p.read_text()) for k, p in paths.items()}
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--apply", "--owner-direction")
    assert rc == 0 and out["episode_ids"] == [2] and out["preferences_added"] == 1, out
    assert out["lines_resolved"] == 3, out
    assert cur.episodes[2]["raw"]["preferences"] == [*HUB["preferences"], NEW[0]]
    for eid in range(1, 9):
        keep = {k: v for k, v in cur.episodes[eid]["raw"].items() if k != "preferences"}
        assert keep == {k: v for k, v in before[eid]["raw"].items() if k != "preferences"}, eid
        assert cur.episodes[eid]["raw"].get("scope") == before[eid]["raw"].get("scope")
    for eid in (3, 4, 5, 6, 7, 8):
        assert cur.episodes[eid] == before[eid], eid
    records = _records()
    assert sorted(records) == [2, 3, 4]
    for eid, key in ((2, "scope"), (3, "tags"), (4, "comma")):
        record = records[eid]
        outbox._validate_resolution(record)
        assert record["resolution"]["kind"] == "preferences_contained_normalized"
        assert record["queued_evidence"] == jobs_before[key] and not paths[key].exists()
    # The scope text the hub's writer dropped survives verbatim in the record.
    assert records[2]["queued_evidence"]["payload"]["scope"] == PATH_SCOPE
    assert records[3]["queued_evidence"]["payload"]["scope"] == LONG_SCOPE
    assert records[3]["queued_evidence"]["payload"]["topics"] == ["Outbox", "claude", "tmp"]
    # The real differences stay held, files unchanged.
    for key in ("missing_topic", "other_scope", "decisions", "dropped_scope_vs_kept"):
        assert json.loads(paths[key].read_text()) == jobs_before[key], key


def test_archive_validation_re_checks_the_normalized_match(lines, tmp_path, monkeypatch):
    _cur, _paths = lines
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--apply", "--owner-direction")
    assert rc == 0, out
    records = _records()
    # A row whose tags lack the line's moved topic proves nothing.
    bad = deepcopy(records[3])
    bad["payload"]["tags"] = ["claude"]
    bad["resolution"]["original_payload_sha256"] = payload_sha256(bad["payload"])
    with pytest.raises(ValueError):
        outbox._validate_resolution(bad)
    # A row that kept a different non-null scope proves nothing.
    bad = deepcopy(records[2])
    bad["payload"]["scope"] = "khipu"
    bad["resolution"]["original_payload_sha256"] = payload_sha256(bad["payload"])
    with pytest.raises(ValueError):
        outbox._validate_resolution(bad)
    # A line the row matches exactly must carry the drain's kind, not this one.
    exact = deepcopy(records[4])
    exact["queued_evidence"]["payload"]["preferences"] = exact["payload"]["preferences"]
    exact["resolution"]["queued_payload_sha256"] = payload_sha256(
        outbox_preferences._hub_view(exact["queued_evidence"]["payload"]))
    with pytest.raises(ValueError):
        outbox._validate_resolution(exact)


def test_undo_puts_normalized_lines_back_and_restores_the_row(lines, tmp_path, monkeypatch):
    cur, paths = lines
    before = deepcopy(cur.episodes)
    jobs_before = {k: p.read_bytes() for k, p in paths.items()}
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--apply", "--owner-direction")
    assert rc == 0, out
    rc, undone = _run("--undo", out["owner_action"], "--owner-direction")
    assert rc == 0 and undone["restored"] == 1 and undone["lines_unresolved"] == 3, undone
    assert cur.episodes == before
    assert {k: json.loads(p.read_text()) for k, p in paths.items()} == \
        {k: json.loads(v) for k, v in jobs_before.items()}
    assert not list(outbox.resolved_dir().glob("*.json"))


@pytest.mark.parametrize("line_changes, raw_changes, expected", [
    ({"scope": PATH_SCOPE}, {}, []),
    ({"scope": "  khipu  "}, {"scope": "khipu"}, []),
    ({"scope": "aegis"}, {"scope": "khipu"}, None),
    ({"scope": PATH_SCOPE}, {"scope": "khipu"}, None),
    ({"topics": ["outbox", "codex"]}, {"tags": ["codex"]}, []),
    ({"topics": ["outbox", "codex"]}, {}, None),
    ({"topics": ["outbox", "graph"]}, {"tags": ["graph"]}, None),
    ({"preferences": ["Prefer short answers, plain words"]},
     {"preferences": ["Prefer short answers", "plain words"]}, []),
    ({"preferences": ["Prefer short answers", "plain words"]},
     {"preferences": ["Prefer short answers, plain words"]}, []),
    ({"preferences": ["Prefer short answers, new text"]}, {}, ["Prefer short answers, new text"]),
    ({"decisions": ["Keep the hub", "an extra decision"]}, {}, None),
])
def test_normalized_missing_cases(line_changes, raw_changes, expected):
    raw = {**deepcopy(HUB), **raw_changes}
    view = outbox_preferences._hub_view(_line(1, summary=HUB["summary"], **line_changes))
    assert outbox_preferences.normalized_missing(raw, view) == expected


@pytest.mark.parametrize("line_prefs, hub_prefs", [
    # Pieces of two distinct hub preferences never stand in for a third.
    (["Never deploy Friday, unless approved"], ["Never deploy Friday", "Ship hotfixes, unless approved"]),
    # The comma join is ordered, the way decisions compare.
    (["b, a"], ["a, b"]),
])
def test_comma_joined_preferences_must_be_an_in_order_run(line_prefs, hub_prefs):
    raw = {**deepcopy(HUB), "preferences": hub_prefs}
    view = outbox_preferences._hub_view(_line(1, summary=HUB["summary"], preferences=line_prefs))
    assert outbox_preferences.normalized_missing(raw, view) == line_prefs


@pytest.fixture
def unordered(hub):  # noqa: F811
    _handles_merge_sql(hub)
    hub.episodes[1]["summary"] = hub.episodes[1]["raw"]["summary"] = "Synthetic episode 1"
    _episode(hub, 2, preferences=["Never deploy Friday", "Ship hotfixes, unless approved"])
    _episode(hub, 3, preferences=["a, b"])
    hub._existing_keys = {(e["ts"], _md5(e["summary"])) for e in hub.episodes.values()}
    hub.next_id = 100
    return hub, {2: _queue(_line(2, preferences=["Never deploy Friday, unless approved"])),
                 3: _queue(_line(3, preferences=["b, a"]))}


def test_a_line_the_ordered_rule_does_not_hold_is_appended_never_archived_as_held(unordered, tmp_path,
                                                                                    monkeypatch):
    cur, paths = unordered
    rc, out = _run()
    # Held only once the append puts the line's own text on the row.
    assert rc == 0 and out["preferences_to_add"] == 2 and out[outbox_preferences.NORMALIZED_KIND] == 0, out
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--apply", "--owner-direction")
    assert rc == 0 and out["episode_ids"] == [2, 3] and out["preferences_added"] == 2, out
    # Append-only: the hub keeps its own preferences and gains the line's.
    assert cur.episodes[2]["raw"]["preferences"] == ["Never deploy Friday", "Ship hotfixes, unless approved",
                                                     "Never deploy Friday, unless approved"]
    assert cur.episodes[3]["raw"]["preferences"] == ["a, b", "b, a"]
    for record in _records().values():
        outbox._validate_resolution(record)
        assert record["resolution"]["kind"] == "preferences_contained"


def test_a_stored_projection_validates_whatever_the_noise_list_and_scope_limit_become(lines, tmp_path,
                                                                                      monkeypatch):
    from khipu import hygiene, identity

    _direction(tmp_path, monkeypatch)
    rc, out = _run("--apply", "--owner-direction")
    assert rc == 0, out
    records = _records()
    # The dropped scopes and the moved noise topics are not in the projection.
    assert records[2]["resolution"]["normalized_projection"] == {"topics": ["outbox"]}
    assert records[3]["resolution"]["normalized_projection"] == {"topics": ["outbox"]}
    # Tomorrow's lists: no topic is noise, and any scope is a label.
    monkeypatch.setattr(hygiene, "is_noise_slug", lambda _s: False)
    monkeypatch.setattr(identity, "normalize_scope", lambda s: (s or "").strip() or None)
    for eid in (2, 3, 4):
        outbox._validate_resolution(records[eid])
    # A record from before the projection was stored falls back to today's lists.
    old = deepcopy(records[3])
    del old["resolution"]["normalized_projection"]
    with pytest.raises(ValueError):
        outbox._validate_resolution(old)
    # A stored projection still has to agree with the line and the row.
    for bad_projection in ({"topics": ["outbox", "graph"]}, {"topics": ["outbox", "claude"], "tags": []},
                           {"topics": ["outbox"], "scope": None, "people": []}, ["outbox"]):
        bad = deepcopy(records[3])
        bad["resolution"]["normalized_projection"] = bad_projection
        with pytest.raises(ValueError):
            outbox._validate_resolution(bad)
