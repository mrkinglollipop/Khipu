"""``khipu outbox merge-line-preferences``: preference text held only in
memory-file lines is appended to its hub episode on the owner's direction,
never removing or rewriting anything, and undone exactly."""
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json

import pytest

from khipu import cli, outbox, owner_direction
from tests.test_outbox_landed_lines import HUB, LINE, _queue, _writes, hub  # noqa: F401 — fixture
from tests.test_outbox_legacy_projection import REASON, world  # noqa: F401 — fixture

NEW = ["Merge line-only preferences", "Never lose memory text"]


def _md5(text):
    return hashlib.md5(text.encode()).hexdigest()


def _handles_merge_sql(cur):
    """Teach the shared fake hub the merge command's three statement shapes."""
    base = cur.execute

    def rows(ids):
        return [(i, e.get("deleted_at"), e["raw"], e.get("preferences")) for i, e in sorted(cur.episodes.items())
                if i in ids]

    def execute(sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT DISTINCT ON (v.i) v.i, e.id"):
            cur.calls.append((s, params))
            out = []
            for i, (ts, md) in enumerate(zip(*params), start=1):
                hits = sorted(((e.get("deleted_at") is not None, -eid), eid) for eid, e in cur.episodes.items()
                              if e["ts"] == ts and _md5(e["summary"]) == md)
                if hits:
                    eid = hits[0][1]
                    out.append((i, *rows({eid})[0]))
            cur._result = out
            return
        if s.startswith("SELECT id, deleted_at, raw, preferences FROM episodes WHERE id = ANY"):
            cur.calls.append((s, params))
            assert s.endswith("ORDER BY id FOR UPDATE")
            cur._result = rows(set(params[0]))
            return
        if s.startswith("UPDATE episodes SET preferences = %s::jsonb, raw ="):
            cur.calls.append((s, params))
            e = cur.episodes[params[-1]]
            assert "AND deleted_at IS NULL" in s
            if e.get("deleted_at") is not None:
                return
            e["preferences"] = None if params[0] is None else json.loads(params[0])
            e["raw"] = dict(e["raw"])
            if "jsonb_set" in s:
                e["raw"]["preferences"] = json.loads(params[1])
            else:
                e["raw"].pop("preferences", None)
            return
        return base(sql, params)

    cur.execute = execute


def _episode(cur, eid, **raw_changes):
    raw = {**deepcopy(HUB), "summary": f"Synthetic episode {eid}", **raw_changes}
    raw = {k: v for k, v in raw.items() if v is not _ABSENT}
    cur.episodes[eid] = {**deepcopy(raw), "id": eid, "raw": raw, "deleted_at": None,
                         "preferences": list(raw.get("preferences") or [])}
    return eid


_ABSENT = object()


def _line(eid, **changes):
    return {**deepcopy(LINE), "summary": f"Synthetic episode {eid}", **changes}


def _bury(payload):
    """A held job, as the drain leaves one after its retries."""
    path = _queue(payload)
    job = json.loads(path.read_text())
    job.update(attempts=25, evidence_conflict=True, last_error="CaptureEvidenceConflict: held")
    outbox.dead_dir().mkdir(parents=True, exist_ok=True)
    dead = outbox.dead_dir() / path.name
    dead.write_text(json.dumps(job))
    path.unlink()
    return dead


def _run(*argv):
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli.main(["outbox", "merge-line-preferences", *argv])
    return rc, json.loads(out.getvalue())


def _direction(tmp_path, monkeypatch, session="s-merge-prefs"):
    from khipu import owner_turns

    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session)
    owner_turns.record({"session_id": session, "prompt": "merge the line preferences", "cwd": str(tmp_path)},
                       {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"})


@pytest.fixture
def backlog(hub):  # noqa: F811
    """One held line of each kind, each at its own hub episode."""
    _handles_merge_sql(hub)
    hub.episodes[1]["summary"] = hub.episodes[1]["raw"]["summary"] = "Synthetic episode 1"
    for eid in (2, 3, 4, 5, 6):
        _episode(hub, eid)
    _episode(hub, 7, preferences=_ABSENT)
    hub.episodes[5]["deleted_at"] = "2026-10-20T00:00:00Z"
    # The fake's insert bookkeeping was taken before these rows existed.
    hub._existing_keys = {(e["ts"], _md5(e["summary"])) for e in hub.episodes.values()}
    hub.next_id = 100
    paths = {
        "pending": _queue(_line(1, preferences=[*HUB["preferences"], *NEW])),
        "dead": _bury(_line(2, preferences=[*HUB["preferences"], NEW[0]])),
        "landed": _queue(_line(3)),
        "other": _queue(_line(4, preferences=[*HUB["preferences"], *NEW],
                              decisions=["Keep the hub", "a decision the hub lacks"])),
        "forgotten": _queue(_line(5, preferences=[*HUB["preferences"], *NEW])),
        "order": _queue(_line(6, preferences=[NEW[0], *HUB["preferences"]])),
        "no_key": _queue(_line(7, preferences=[NEW[1]])),
        "no_row": _queue(_line(99, preferences=[NEW[0]])),
    }
    return hub, paths


def _prefs(cur, eid):
    e = cur.episodes[eid]
    return e["raw"].get("preferences", _ABSENT), e["preferences"]


def test_dry_run_counts_each_kind_and_prints_no_preference_text(backlog):
    cur, _paths = backlog
    before = deepcopy(cur.episodes)
    rc, out = _run()
    assert rc == 0 and out["dry_run"] is True, out
    assert out["held_lines"] == 8
    assert (out["mergeable"], out["landed"], out["other"], out["forgotten"],
            out["preference_order"], out["no_hub_row"]) == (3, 1, 1, 1, 1, 1), out
    assert out["episodes"] == 3 and out["preferences_to_add"] == 2 + 1 + 1
    assert {s["episode_id"]: s["preferences_to_add"] for s in out["samples"]} == {1: 2, 2: 1, 7: 1}
    assert {s["where"] for s in out["samples"]} == {"outbox", "dead"}
    text = json.dumps(out)
    assert not any(p in text for p in NEW + HUB["preferences"])
    assert cur.episodes == before and not _writes(cur)


def test_apply_needs_owner_direction_and_changes_nothing_without_it(backlog, tmp_path, monkeypatch):
    cur, _paths = backlog
    before = deepcopy(cur.episodes)
    rc, out = _run("--apply")
    assert rc == 2 and out["code"] == "owner_only" and "Nothing was changed" in out["error"], out
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s-nobody-typed-here")
    rc, out = _run("--apply", "--owner-direction")
    assert rc == 2 and out["ok"] is False and "Nothing was changed" in out["error"], out
    assert cur.episodes == before and not _writes(cur)
    assert not [x for x in owner_direction.actions() if x["action"] == "outbox.merge_preferences"
                and x["state"] == "applied"]


def test_apply_appends_only_missing_preferences_and_leaves_everything_else(backlog, tmp_path, monkeypatch):
    cur, paths = backlog
    before = deepcopy(cur.episodes)
    jobs_before = {k: p.read_bytes() for k, p in paths.items()}
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--apply", "--owner-direction")
    assert rc == 0 and out["ok"] and out["owner_action"], out
    assert out["episodes_merged"] == 3 and out["preferences_added"] == 4 and out["episode_ids"] == [1, 2, 7]
    # Existing hub preferences stay first and unchanged; only the missing ones follow.
    assert _prefs(cur, 1) == ([*HUB["preferences"], *NEW], [*HUB["preferences"], *NEW])
    assert _prefs(cur, 2) == ([*HUB["preferences"], NEW[0]], [*HUB["preferences"], NEW[0]])
    assert _prefs(cur, 7) == ([NEW[1]], [NEW[1]])
    for eid in (1, 2, 7):
        rest = {k: v for k, v in cur.episodes[eid]["raw"].items() if k != "preferences"}
        assert rest == {k: v for k, v in before[eid]["raw"].items() if k != "preferences"}
    # Other differences, a preference order no append can fix, a landed line
    # and a forgotten episode: untouched.
    for eid in (3, 4, 5, 6):
        assert cur.episodes[eid] == before[eid], eid
    assert not [p for s, p in cur.calls if s.startswith("UPDATE") and p[-1] in (3, 4, 5, 6)]
    # Job files are never moved or deleted by the merge.
    assert {k: p.read_bytes() for k, p in paths.items()} == jobs_before
    # The log holds each episode and the exact strings added, written ahead.
    line = next(x for x in owner_direction.actions() if x["id"] == out["owner_action"])
    assert line["action"] == "outbox.merge_preferences" and line["auth"] == "direction"
    assert line["state"] == "applied" and "no_undo" not in line
    added = {e["episode_id"]: e["added"] for e in owner_direction.preference_merges(line["id"])}
    assert added == {1: NEW, 2: [NEW[0]], 7: [NEW[1]]}


def test_a_merged_line_lands_on_its_next_drain(backlog, tmp_path, monkeypatch):
    cur, paths = backlog
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--apply", "--owner-direction")
    assert rc == 0, out
    monkeypatch.setattr(outbox, "MAX_ATTEMPTS", 100)
    report = outbox.drain()
    assert not paths["pending"].exists() and paths["dead"].exists()
    records = [json.loads(p.read_text()) for p in outbox.resolved_dir().glob("*.json")]
    episodes = {r["resolution"]["episode_id"]: r["resolution"]["kind"] for r in records}
    assert episodes[1] == "legacy_landed_line" and episodes[7] == "legacy_landed_line", report
    for r in records:
        outbox._validate_resolution(r)
    # The held one resolves too once the owner moves it back.
    outbox.retry_dead()
    outbox.drain()
    records = [json.loads(p.read_text()) for p in outbox.resolved_dir().glob("*.json")]
    assert 2 in {r["resolution"]["episode_id"] for r in records}
    assert paths["other"].exists() and paths["order"].exists()


def test_a_second_apply_changes_nothing(backlog, tmp_path, monkeypatch):
    cur, _paths = backlog
    _direction(tmp_path, monkeypatch)
    rc, first = _run("--apply", "--owner-direction")
    assert rc == 0 and first["episodes_merged"] == 3, first
    after = deepcopy(cur.episodes)
    updates = len(_writes(cur))
    rc, second = _run("--apply", "--owner-direction")
    assert rc == 0 and second["episodes_merged"] == 0 and second["preferences_added"] == 0, second
    assert second["mergeable"] == 0 and second["landed"] == 4
    assert cur.episodes == after and len(_writes(cur)) == updates
    assert owner_direction.preference_merges(second["owner_action"]) == []


def test_undo_restores_the_exact_prior_preferences(backlog, tmp_path, monkeypatch):
    cur, _paths = backlog
    before = deepcopy(cur.episodes)
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--apply", "--owner-direction")
    assert rc == 0, out
    rc, refused = _run("--undo", out["owner_action"])
    assert rc == 2 and refused["code"] == "owner_only", refused
    rc, undone = _run("--undo", out["owner_action"], "--owner-direction")
    assert rc == 0 and undone["ok"] and undone["restored"] == 3 and undone["left_alone"] == [], undone
    assert cur.episodes == before
    assert "preferences" not in cur.episodes[7]["raw"]
    assert owner_direction.line_state(out["owner_action"]) == "undone"
    rc, again = _run("--undo", out["owner_action"], "--owner-direction")
    assert rc == 2 and "already undone" in again["error"], again


def test_undo_leaves_a_row_changed_since_the_merge(backlog, tmp_path, monkeypatch):
    cur, _paths = backlog
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--apply", "--owner-direction")
    assert rc == 0, out
    cur.episodes[1]["raw"]["preferences"].append("Written later by a capture")
    cur.episodes[1]["preferences"].append("Written later by a capture")
    changed = deepcopy(cur.episodes[1])
    rc, undone = _run("--undo", out["owner_action"], "--owner-direction")
    assert rc == 0 and undone["restored"] == 2 and undone["left_alone"] == [1], undone
    assert cur.episodes[1] == changed


def test_a_forgotten_episode_is_never_touched_even_if_forgotten_after_planning(backlog, tmp_path, monkeypatch):
    from khipu import outbox_preferences

    cur, _paths = backlog
    planned = outbox_preferences._plan

    def plan_then_forget(c):
        out = planned(c)
        cur.episodes[1]["deleted_at"] = "2026-10-27T00:00:00Z"
        return out

    monkeypatch.setattr(outbox_preferences, "_plan", plan_then_forget)
    before = deepcopy(cur.episodes[1])
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--apply", "--owner-direction")
    assert rc == 0 and 1 not in out["episode_ids"] and out["episode_ids"] == [2, 7], out
    assert {k: v for k, v in cur.episodes[1].items() if k != "deleted_at"} == \
        {k: v for k, v in before.items() if k != "deleted_at"}


def test_status_and_doctor_name_the_merge_while_such_lines_are_held(backlog):
    assert outbox.status()["line_conflicts"] == 1  # the held one; the queued ones are not tried yet
    for path in outbox.jobs():
        job = json.loads(path.read_text())
        job["evidence_conflict"] = True
        path.write_text(json.dumps(job))
    st = outbox.status()
    assert st["line_conflicts"] == 8 and "khipu outbox merge-line-preferences" in st["fix"], st
    # Everything held, nothing queued: doctor names the merge before retry-dead.
    for path in outbox.jobs():
        _bury(json.loads(path.read_text())["payload"])
        path.unlink(missing_ok=True)
    local = cli._doctor_local_checks(None)
    fix = local["outbox"]["fix"]
    assert local["outbox"]["pending"] == 0 and local["outbox"]["line_conflicts"] == 8, local["outbox"]
    assert fix.startswith("Run `khipu outbox merge-line-preferences`") and "retry-dead" in fix, fix
