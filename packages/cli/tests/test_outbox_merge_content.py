"""``merge-line-preferences --include-content``: a held line's topic items the
hub episode lacks are appended where the hub's writer would put them (topics
vs tags, aliases folded), append-only, in column and raw, undone exactly. A
line differing only by a kept hub scope is archived as ``scope_differs_kept``
with the hub scope untouched; decisions are never appended."""
from copy import deepcopy
import json
import re

import pytest

from khipu import hygiene, outbox, outbox_preferences
from khipu.capture_identity import payload_sha256
from tests.test_outbox_landed_lines import HUB, _queue, _writes, hub  # noqa: F401 — fixture
from tests.test_outbox_legacy_projection import world  # noqa: F401 — fixture
from tests.test_outbox_merge_preferences import (_ABSENT, NEW, _direction, _episode, _handles_merge_sql, _line,
                                                 _md5, _run)

ALIASES = {"graphs": "graph"}


def _classify(_cur, topics):
    """The writer's split, faked: noise to tags, aliases folded, ``boom``
    fails the existence check."""
    s = topics[0].strip().lower()
    if s == "boom":
        return [s], [], True
    if hygiene.is_noise_slug(s):
        return [], [s], False
    return [ALIASES.get(s, s)], [], False


def _handles_content_sql(cur):
    _handles_merge_sql(cur)
    base = cur.execute

    def rows(ids):
        return [(i, e.get("deleted_at"), e["raw"], e.get("preferences"), e.get("topics"), e.get("tags"))
                for i, e in sorted(cur.episodes.items()) if i in ids]

    def execute(sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT DISTINCT ON (v.i) v.i, e.id, e.deleted_at, e.raw, e.preferences, e.topics, e.tags"):
            cur.calls.append((s, params))
            out = []
            for i, (ts, md) in enumerate(zip(*params), start=1):
                hits = sorted(((e.get("deleted_at") is not None, -eid), eid) for eid, e in cur.episodes.items()
                              if e["ts"] == ts and _md5(e["summary"]) == md)
                if hits:
                    out.append((i, *rows({hits[0][1]})[0]))
            cur._result = out
            return
        if s.startswith("SELECT id, deleted_at, raw, preferences, topics, tags FROM episodes WHERE id = ANY"):
            cur.calls.append((s, params))
            assert s.endswith("ORDER BY id FOR UPDATE")
            cur._result = rows(set(params[0]))
            return
        m = re.match(r"UPDATE episodes SET (topics|tags) = %s::jsonb, raw = ", s)
        if m:
            cur.calls.append((s, params))
            field, e = m.group(1), cur.episodes[params[-1]]
            assert s.endswith("WHERE id = %s AND deleted_at IS NULL")
            if e.get("deleted_at") is not None:
                return
            e[field] = None if params[0] is None else json.loads(params[0])
            e["raw"] = dict(e["raw"])
            if "jsonb_set" in s:
                assert f"'{{{field}}}'" in s
                e["raw"][field] = json.loads(params[1])
            else:
                e["raw"].pop(field, None)
            return
        return base(sql, params)

    cur.execute = execute


@pytest.fixture
def held(hub, monkeypatch):  # noqa: F811
    monkeypatch.setattr(hygiene, "classify_topics", _classify)
    _handles_content_sql(hub)
    hub.episodes[1]["summary"] = hub.episodes[1]["raw"]["summary"] = "Synthetic episode 1"
    _episode(hub, 2)
    hub.episodes[2]["topics"] = []  # the column lost a topic raw keeps
    _episode(hub, 3, tags=_ABSENT)
    hub.episodes[3]["tags"] = None  # no tags column value either
    _episode(hub, 4, topics=["outbox", "graph"])
    _episode(hub, 5, tags=["release-train"])
    _episode(hub, 6)
    _episode(hub, 7, scope="khipu")
    _episode(hub, 8)
    hub.episodes[8]["deleted_at"] = "2026-10-20T00:00:00Z"
    _episode(hub, 9)
    hub._existing_keys = {(e["ts"], _md5(e["summary"])) for e in hub.episodes.values()}
    hub.next_id = 100
    paths = {
        "topic": _queue(_line(2, topics=["outbox", "graph", " Graph"], preferences=[*HUB["preferences"], NEW[0]])),
        "noise": _queue(_line(3, topics=["outbox", "tmp"])),
        "alias": _queue(_line(4, topics=["outbox", "graphs"])),
        "in_tags": _queue(_line(5, topics=["outbox", "release-train"])),
        "decisions": _queue(_line(6, topics=["outbox", "graph"],
                                  decisions=["Keep the hub", "a decision the hub lacks"])),
        "scope": _queue(_line(7, scope="aegis", topics=["outbox", "graph"])),
        "forgotten": _queue(_line(8, topics=["outbox", "graph"])),
        "unplaced": _queue(_line(9, topics=["outbox", "boom"])),
    }
    return hub, paths


def _records():
    return {json.loads(p.read_text())["resolution"]["episode_id"]: json.loads(p.read_text())
            for p in outbox.resolved_dir().glob("*.json")}


def test_dry_run_without_the_flag_is_unchanged_and_with_it_counts_content(held):
    cur, _paths = held
    before = deepcopy(cur.episodes)
    rc, out = _run()
    assert rc == 0 and outbox_preferences.CONTENT_KIND not in out and "topics_to_add" not in out, out
    assert out["needs_review"] == 7 and out["forgotten"] == 1, out
    rc, out = _run("--include-content")
    assert rc == 0 and out["dry_run"] is True, out
    # Once their topic is appended, the topic and noise lines hold under the
    # existing normalized rule and keep its kind; the alias and in-tags lines
    # need the content rule.
    assert (out[outbox_preferences.CONTENT_KIND], out[outbox_preferences.SCOPE_KIND],
            out[outbox_preferences.NORMALIZED_KIND], out["needs_review"], out["forgotten"]) == (2, 1, 2, 2, 1), out
    assert out["needs_review_episode_ids"] == [6, 9] and out["needs_review_decisions_episode_ids"] == [6], out
    assert out["scope_differs_kept_episode_ids"] == [7], out
    assert (out["preferences_to_add"], out["topics_to_add"], out["tags_to_add"]) == (1, 2, 1), out
    assert out["episodes"] == 3, out
    text = json.dumps(out)
    assert "graph" not in text and "release-train" not in text and NEW[0] not in text
    assert cur.episodes == before and not _writes(cur)


def test_apply_needs_the_owner_and_changes_nothing_without_it(held, monkeypatch):
    cur, paths = held
    before = deepcopy(cur.episodes)
    rc, out = _run("--include-content", "--apply")
    assert rc == 2 and out["code"] == "owner_only", out
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s-nobody-typed-here")
    rc, out = _run("--include-content", "--apply", "--owner-direction")
    assert rc == 2 and out["ok"] is False, out
    assert cur.episodes == before and not _writes(cur)
    assert all(p.exists() for p in paths.values())


def test_apply_appends_topics_where_the_writer_puts_them_and_archives_the_lines(held, tmp_path, monkeypatch):
    cur, paths = held
    before = deepcopy(cur.episodes)
    jobs_before = {k: json.loads(p.read_text()) for k, p in paths.items()}
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--include-content", "--apply", "--owner-direction")
    assert rc == 0 and out["episode_ids"] == [2, 3, 7], out
    assert (out["preferences_added"], out["topics_added"], out["tags_added"]) == (1, 2, 1), out
    assert out["lines_resolved"] == 5, out
    # Topics: appended once (deduped), raw and column; the column only gets what it lacks.
    assert cur.episodes[2]["raw"]["topics"] == ["outbox", "graph"] and cur.episodes[2]["topics"] == ["graph"]
    assert cur.episodes[2]["raw"]["preferences"] == [*HUB["preferences"], NEW[0]]
    # A noise slug goes to tags, never topics.
    assert cur.episodes[3]["raw"]["tags"] == ["tmp"] and cur.episodes[3]["tags"] == ["tmp"]
    assert cur.episodes[3]["raw"]["topics"] == HUB["topics"]
    # Held already (under its alias, or in tags): no write.
    for eid in (4, 5, 6, 8, 9):
        assert cur.episodes[eid] == before[eid], eid
    # The kept scope is never changed; decisions are never touched anywhere.
    assert cur.episodes[7]["raw"]["topics"] == ["outbox", "graph"]
    assert cur.episodes[7]["raw"]["scope"] == "khipu" and cur.episodes[7]["scope"] == "khipu"
    for eid in cur.episodes:
        assert cur.episodes[eid]["raw"].get("decisions") == before[eid]["raw"].get("decisions"), eid
        assert cur.episodes[eid].get("decisions") == before[eid].get("decisions"), eid
        keep = {k: v for k, v in cur.episodes[eid]["raw"].items() if k not in ("preferences", "topics", "tags")}
        assert keep == {k: v for k, v in before[eid]["raw"].items() if k not in ("preferences", "topics", "tags")}
    records = _records()
    assert sorted(records) == [2, 3, 4, 5, 7]
    for eid, key in ((2, "topic"), (3, "noise"), (4, "alias"), (5, "in_tags"), (7, "scope")):
        record = records[eid]
        outbox._validate_resolution(record)
        assert record["queued_evidence"] == jobs_before[key] and not paths[key].exists(), key
        assert record["payload"] == cur.episodes[eid]["raw"], key
        want = {2: outbox_preferences.NORMALIZED_KIND, 3: outbox_preferences.NORMALIZED_KIND,
                7: outbox_preferences.SCOPE_KIND}.get(eid, outbox_preferences.CONTENT_KIND)
        assert record["resolution"]["kind"] == want, key
    assert records[4]["resolution"]["topic_folds"] == {"graphs": "graph"}
    assert records[7]["queued_evidence"]["payload"]["scope"] == "aegis"
    for key in ("decisions", "forgotten", "unplaced"):
        assert json.loads(paths[key].read_text()) == jobs_before[key], key


def test_a_second_apply_changes_nothing(held, tmp_path, monkeypatch):
    cur, _paths = held
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--include-content", "--apply", "--owner-direction")
    assert rc == 0, out
    after = deepcopy(cur.episodes)
    rc, again = _run("--include-content", "--apply", "--owner-direction")
    assert rc == 0 and again["episodes_merged"] == 0 and again["lines_resolved"] == 0, again
    assert cur.episodes == after


def test_undo_restores_the_exact_prior_topics_tags_preferences_and_jobs(held, tmp_path, monkeypatch):
    cur, paths = held
    before = deepcopy(cur.episodes)
    jobs_before = {k: p.read_bytes() for k, p in paths.items()}
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--include-content", "--apply", "--owner-direction")
    assert rc == 0, out
    rc, undone = _run("--undo", out["owner_action"], "--owner-direction")
    assert rc == 0 and undone["restored"] == 3 and undone["lines_unresolved"] == 5, undone
    assert undone["left_alone"] == [] and undone["unproven"] == [], undone
    # Raw without a tags key gets none back; a column that lacked a topic lacks it again.
    assert "tags" not in cur.episodes[3]["raw"] and cur.episodes[2]["topics"] == []
    assert cur.episodes == before
    assert {k: p.read_bytes() for k, p in paths.items()} == jobs_before
    assert not list(outbox.resolved_dir().glob("*.json"))


def test_undo_leaves_a_row_whose_topics_changed_since(held, tmp_path, monkeypatch):
    cur, _paths = held
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--include-content", "--apply", "--owner-direction")
    assert rc == 0, out
    cur.episodes[2]["raw"] = dict(cur.episodes[2]["raw"], topics=["outbox", "graph", "later"])
    later = deepcopy(cur.episodes[2])
    rc, undone = _run("--undo", out["owner_action"], "--owner-direction")
    assert rc == 0 and undone["left_alone"] == [2] and undone["restored"] == 2, undone
    assert cur.episodes[2] == later


@pytest.mark.parametrize("eid, tamper", [
    (2, lambda r: r["payload"].update(topics=["outbox"])),
    (3, lambda r: r["payload"].update(tags=[])),
    (4, lambda r: r["resolution"].update(topic_folds={"graphs": "graph-nowhere"})),
    (4, lambda r: r["resolution"].update(topic_folds={})),
    (7, lambda r: r["payload"].update(scope="aegis")),
    (7, lambda r: r["payload"].update(scope=None)),
    (7, lambda r: r["resolution"].update(kind=outbox_preferences.CONTENT_KIND)),
])
def test_archive_validation_re_checks_the_content_proof(held, tmp_path, monkeypatch, eid, tamper):
    _cur, _paths = held
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--include-content", "--apply", "--owner-direction")
    assert rc == 0, out
    bad = deepcopy(_records()[eid])
    tamper(bad)
    bad["resolution"]["original_payload_sha256"] = payload_sha256(bad["payload"])
    with pytest.raises(ValueError):
        outbox._validate_resolution(bad)


def test_content_missing_never_appends_what_the_row_holds():
    raw = {**deepcopy(HUB), "topics": ["outbox", "graph"], "tags": ["claude"]}

    def place(s):
        return _classify(None, [s])[1] and ("tags", s) or ("topics", ALIASES.get(s, s))

    view = outbox_preferences._hub_view(_line(1, summary=HUB["summary"], topics=["Outbox", "graphs", "claude"]))
    assert outbox_preferences.content_missing(raw, view, place) == {"preferences": [], "topics": [], "tags": []}
    view = outbox_preferences._hub_view(_line(1, summary=HUB["summary"], topics=["new-one", "tmp"],
                                              tags=["claude", "own-tag"]))
    assert outbox_preferences.content_missing(raw, view, place) == \
        {"preferences": [], "topics": ["new-one"], "tags": ["tmp", "own-tag"]}
    # A decision the row lacks is never content: the line stays held.
    view = outbox_preferences._hub_view(_line(1, summary=HUB["summary"], decisions=["Something new"]))
    assert outbox_preferences.content_missing(raw, view, place) is None
