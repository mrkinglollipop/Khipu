"""``khipu outbox merge-line-preferences``: preference text held only in
memory-file lines is appended to its hub episode on the owner's direction,
never removing or rewriting anything, and undone exactly."""
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json

import pytest

from khipu import cli, outbox, outbox_preferences, owner_direction, tombstones
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
    assert (out["mergeable"], out["landed"], out["needs_review"], out["forgotten"],
            out["preferences_contained"], out["no_hub_row"], out["column_shape"]) == (3, 1, 1, 1, 1, 1, 0), out
    # The reordered line's one missing preference is appended too, then it is contained.
    assert out["episodes"] == 4 and out["preferences_to_add"] == 2 + 1 + 1 + 1
    assert out["needs_review_episode_ids"] == [4] and out["preferences_contained_normalized"] == 0
    assert {s["episode_id"]: s["preferences_to_add"] for s in out["samples"]} == {1: 2, 2: 1, 6: 1, 7: 1}
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
    assert out["episodes_merged"] == 4 and out["preferences_added"] == 5 and out["episode_ids"] == [1, 2, 6, 7]
    assert out["lines_resolved"] == 1, out
    # Existing hub preferences stay first and unchanged; only the missing ones follow.
    assert _prefs(cur, 1) == ([*HUB["preferences"], *NEW], [*HUB["preferences"], *NEW])
    assert _prefs(cur, 2) == ([*HUB["preferences"], NEW[0]], [*HUB["preferences"], NEW[0]])
    assert _prefs(cur, 7) == ([NEW[1]], [NEW[1]])
    assert _prefs(cur, 6) == ([*HUB["preferences"], NEW[0]], [*HUB["preferences"], NEW[0]])
    for eid in (1, 2, 6, 7):
        rest = {k: v for k, v in cur.episodes[eid]["raw"].items() if k != "preferences"}
        assert rest == {k: v for k, v in before[eid]["raw"].items() if k != "preferences"}
    # Other differences, a landed line and a forgotten episode: untouched.
    for eid in (3, 4, 5):
        assert cur.episodes[eid] == before[eid], eid
    assert not [p for s, p in cur.calls if s.startswith("UPDATE") and p[-1] in (3, 4, 5)]
    # The reordered line, now contained, moved to resolved/ whole; every other
    # job file is unchanged.
    assert not paths["order"].exists()
    [record] = [json.loads(p.read_text()) for p in outbox.resolved_dir().glob("*.json")]
    outbox._validate_resolution(record)
    assert record["resolution"]["kind"] == "preferences_contained" and record["resolution"]["episode_id"] == 6
    assert record["queued_evidence"] == json.loads(jobs_before["order"])
    assert {k: p.read_bytes() for k, p in paths.items() if k != "order"} == \
        {k: v for k, v in jobs_before.items() if k != "order"}
    # The log holds each episode and the exact strings added, written ahead.
    line = next(x for x in owner_direction.actions() if x["id"] == out["owner_action"])
    assert line["action"] == "outbox.merge_preferences" and line["auth"] == "direction"
    assert line["state"] == "applied" and "no_undo" not in line
    added = {e["episode_id"]: e["added"] for e in owner_direction.preference_merges(line["id"])}
    assert added == {1: NEW, 2: [NEW[0]], 6: [NEW[0]], 7: [NEW[1]]}
    assert [r["episode_id"] for r in owner_direction.preference_merges(line["id"], "preference_resolutions")] == [6]


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
    assert paths["other"].exists() and not paths["order"].exists()


def test_a_second_apply_changes_nothing(backlog, tmp_path, monkeypatch):
    cur, _paths = backlog
    _direction(tmp_path, monkeypatch)
    rc, first = _run("--apply", "--owner-direction")
    assert rc == 0 and first["episodes_merged"] == 4, first
    after = deepcopy(cur.episodes)
    updates = len(_writes(cur))
    rc, second = _run("--apply", "--owner-direction")
    assert rc == 0 and second["episodes_merged"] == 0 and second["preferences_added"] == 0, second
    assert second["mergeable"] == 0 and second["landed"] == 4 and second["preferences_contained"] == 0
    assert cur.episodes == after and len(_writes(cur)) == updates
    assert owner_direction.preference_merges(second["owner_action"]) == []


def test_undo_restores_the_exact_prior_preferences(backlog, tmp_path, monkeypatch):
    cur, paths = backlog
    before = deepcopy(cur.episodes)
    order = json.loads(paths["order"].read_text())
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--apply", "--owner-direction")
    assert rc == 0, out
    rc, refused = _run("--undo", out["owner_action"])
    assert rc == 2 and refused["code"] == "owner_only", refused
    rc, undone = _run("--undo", out["owner_action"], "--owner-direction")
    assert rc == 0 and undone["ok"] and undone["restored"] == 4 and undone["left_alone"] == [], undone
    assert cur.episodes == before
    # The resolved job is back where it was held, and its record is gone.
    assert undone["lines_unresolved"] == 1 and json.loads(paths["order"].read_text()) == order
    assert not list(outbox.resolved_dir().glob("*.json"))
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
    assert rc == 0 and undone["restored"] == 3 and undone["left_alone"] == [1], undone
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
    assert rc == 0 and 1 not in out["episode_ids"] and out["episode_ids"] == [2, 6, 7], out
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


# ---- fix round 1 (reviews of 357e974d) ------------------------------------------------------


def _flaky_second_batch(monkeypatch):
    """BATCH=1 and the owner-actions log write fails on the second batch,
    after the first committed."""
    from khipu import outbox_preferences

    monkeypatch.setattr(outbox_preferences, "BATCH", 1)
    real = owner_direction.record_preference_merges
    calls = {"n": 0}

    def flaky(entries):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated log failure on batch 2")
        return real(entries)

    monkeypatch.setattr(owner_direction, "record_preference_merges", flaky)


def _second_line(first_path, payload):
    """Another held line for the same episode (same ts and summary), in its own file."""
    job = json.loads(first_path.read_text())
    job["payload"] = payload
    second = first_path.with_name(first_path.stem + "-second.json")
    second.write_text(json.dumps(job))
    return second


def test_an_episode_forgotten_by_id_is_never_merged(backlog, tmp_path, monkeypatch):
    """Forgotten on this Mac by id only (a backup's older raw), hub row live."""
    cur, _paths = backlog
    tombstones.publish_episode_ids({1})
    before = deepcopy(cur.episodes[1])
    rc, dry = _run()
    assert rc == 0 and dry["forgotten"] == 2 and 1 not in {s["episode_id"] for s in dry["samples"]}, dry
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--apply", "--owner-direction")
    assert rc == 0 and 1 not in out["episode_ids"], out
    assert cur.episodes[1] == before


def test_an_episode_forgotten_by_id_after_planning_is_skipped_under_the_lock(backlog, tmp_path, monkeypatch):
    cur, _paths = backlog
    planned = outbox_preferences._plan

    def plan_then_forget(c):
        out = planned(c)
        tombstones.publish_episode_ids({1})
        return out

    monkeypatch.setattr(outbox_preferences, "_plan", plan_then_forget)
    before = deepcopy(cur.episodes[1])
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--apply", "--owner-direction")
    assert rc == 0 and out["episode_ids"] == [2, 6, 7], out
    assert cur.episodes[1] == before


def test_undo_leaves_an_episode_forgotten_by_id_after_the_merge(backlog, tmp_path, monkeypatch):
    cur, _paths = backlog
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--apply", "--owner-direction")
    assert rc == 0, out
    tombstones.publish_episode_ids({1})
    after_merge = deepcopy(cur.episodes[1])
    rc, undone = _run("--undo", out["owner_action"], "--owner-direction")
    assert rc == 0 and undone["left_alone"] == [1] and undone["restored"] == 3, undone
    assert cur.episodes[1] == after_merge


def test_undo_leaves_an_episode_whose_line_source_was_forgotten_after_the_merge(backlog, tmp_path, monkeypatch):
    cur, paths = backlog
    view = outbox_preferences._hub_view(json.loads(paths["pending"].read_text())["payload"])
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--apply", "--owner-direction")
    assert rc == 0, out
    keys, hashes = tombstones._identity_values(view)
    tombstones.publish_identities(keys, hashes)
    after_merge = deepcopy(cur.episodes[1])
    rc, undone = _run("--undo", out["owner_action"], "--owner-direction")
    assert rc == 0 and undone["left_alone"] == [1], undone
    assert cur.episodes[1] == after_merge


def test_a_failed_apply_keeps_its_committed_batches_undoable(backlog, tmp_path, monkeypatch):
    cur, _paths = backlog
    before = deepcopy(cur.episodes)
    _flaky_second_batch(monkeypatch)
    _direction(tmp_path, monkeypatch)
    assert cli.main(["outbox", "merge-line-preferences", "--apply", "--owner-direction"]) == 1
    line = next(x for x in owner_direction.actions() if x["action"] == "outbox.merge_preferences")
    assert line["state"] == "unconfirmed", line
    assert _prefs(cur, 1)[0] == [*HUB["preferences"], *NEW]
    assert cur.episodes[2] == before[2]  # the failed batch rolled back
    rc, undone = _run("--undo", line["id"], "--owner-direction")
    assert rc == 0 and undone["restored"] == 1, undone
    assert cur.episodes == before
    assert owner_direction.line_state(line["id"]) == "undone"


def test_a_failed_apply_is_undoable_through_owner_direction_undo(backlog, tmp_path, monkeypatch):
    cur, _paths = backlog
    before = deepcopy(cur.episodes)
    _flaky_second_batch(monkeypatch)
    _direction(tmp_path, monkeypatch)
    assert cli.main(["outbox", "merge-line-preferences", "--apply", "--owner-direction"]) == 1
    line = next(x for x in owner_direction.actions() if x["action"] == "outbox.merge_preferences")
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli.main(["owner-direction", "undo", line["id"], "--owner-direction"])
    assert rc == 0, out.getvalue()
    assert cur.episodes == before


def test_two_lines_for_one_episode_with_disjoint_additions_both_count(backlog, tmp_path, monkeypatch):
    cur, paths = backlog
    first = paths["pending"]
    job = json.loads(first.read_text())
    job["payload"] = _line(1, preferences=[*HUB["preferences"], NEW[0]])
    first.write_text(json.dumps(job))
    second = _second_line(first, _line(1, preferences=[*HUB["preferences"], NEW[1]]))
    rc, dry = _run()
    assert rc == 0, dry
    sample = {s["episode_id"]: (s["lines"], s["preferences_to_add"]) for s in dry["samples"]}
    assert sample[1] == (2, 2), dry
    # Neither line matches the merged row exactly any more (each lacks the
    # other's text), so both are contained, beside the reordered line.
    assert (dry["preferences_to_add"], dry["mergeable"], dry["preferences_contained"]) == (5, 2, 3), dry
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--apply", "--owner-direction")
    assert rc == 0 and out["preferences_added"] == dry["preferences_to_add"], out
    assert out["lines_resolved"] == 3, out
    # Appended in job-file order ("…-second.json" sorts first).
    assert _prefs(cur, 1)[0] == [*HUB["preferences"], NEW[1], NEW[0]]
    kinds = [json.loads(p.read_text())["resolution"]["kind"] for p in outbox.resolved_dir().glob("*.json")]
    assert kinds == ["preferences_contained"] * 3, kinds
    assert not first.exists() and not second.exists()


def test_dry_run_does_not_count_a_row_apply_skips_for_its_column_shape(backlog, tmp_path, monkeypatch):
    cur, _paths = backlog
    cur.episodes[2]["preferences"] = {"not": "a list"}
    rc, dry = _run()
    assert rc == 0 and dry["column_shape"] == 1 and dry["mergeable"] == 2, dry
    assert 2 not in {s["episode_id"] for s in dry["samples"]}
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--apply", "--owner-direction")
    assert rc == 0 and 2 not in out["episode_ids"] and out["column_shape"] == 1, out


def test_a_preferences_contained_record_is_validated_and_never_a_drain_proof(backlog, tmp_path, monkeypatch):
    from khipu.capture import _legacy_projection_kind
    from khipu.capture_identity import payload_sha256

    cur, _paths = backlog
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--apply", "--owner-direction")
    assert rc == 0, out
    [path] = outbox.resolved_dir().glob("*.json")
    record = json.loads(path.read_text())
    outbox._validate_resolution(record)
    # A row that lacks one of the line's preferences proves nothing.
    bad = deepcopy(record)
    bad["payload"]["preferences"] = HUB["preferences"]
    bad["resolution"]["original_payload_sha256"] = payload_sha256(bad["payload"])
    with pytest.raises(ValueError):
        outbox._validate_resolution(bad)
    # A line the row matches exactly must carry the drain's kind, not this one.
    exact = deepcopy(record)
    exact["queued_evidence"]["payload"]["preferences"] = record["payload"]["preferences"]
    exact["resolution"]["queued_payload_sha256"] = payload_sha256(
        outbox_preferences._hub_view(exact["queued_evidence"]["payload"]))
    with pytest.raises(ValueError):
        outbox._validate_resolution(exact)
    # The drain's exact check is unchanged: this line never lands there.
    view = outbox_preferences._hub_view(record["queued_evidence"]["payload"])
    assert _legacy_projection_kind(record["payload"], view) is None


def test_forget_sweeps_a_preferences_contained_record(backlog, tmp_path, monkeypatch):
    from khipu import forget

    cur, _paths = backlog
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--apply", "--owner-direction")
    assert rc == 0, out
    with outbox._locked_outbox():
        entries = forget._outbox_entries()
    [hit] = [e for e in entries if e["path"].parent == outbox.resolved_dir()]
    assert "error" not in hit and len(hit["payloads"]) == 2, hit


def test_apply_and_undo_by_a_typed_yes_on_a_terminal(backlog, tmp_path, monkeypatch):
    from khipu import routing

    cur, _paths = backlog
    before = deepcopy(cur.episodes)
    monkeypatch.setattr(routing, "typed_yes", lambda prompt: True)
    rc, out = _run("--apply")
    assert rc == 0 and out["episode_ids"] == [1, 2, 6, 7], out
    line = next(x for x in owner_direction.actions() if x["action"] == "outbox.merge_preferences")
    assert line["auth"] == "terminal" and line["state"] == "applied", line
    rc, undone = _run("--undo", line["id"])
    assert rc == 0 and undone["restored"] == 4, undone
    assert cur.episodes == before


def test_apply_from_the_app_and_a_forged_from_app_is_refused(backlog, tmp_path, monkeypatch):
    from khipu import routing

    cur, _paths = backlog
    before = deepcopy(cur.episodes)
    monkeypatch.setattr(routing, "_caller", lambda: {"app": False, "chain": ["zsh"], "where": "zsh"})
    rc, out = _run("--apply", "--from-app")
    assert rc == 2 and out["code"] == "owner_only", out
    assert cur.episodes == before
    monkeypatch.setattr(routing, "_caller", lambda: {"app": True, "chain": [], "where": "Khipu"})
    rc, out = _run("--apply", "--from-app")
    assert rc == 0 and out["episode_ids"] == [1, 2, 6, 7], out
    line = next(x for x in owner_direction.actions() if x["action"] == "outbox.merge_preferences")
    assert line["auth"] == "app" and line["state"] == "applied", line


def test_undo_leaves_the_resolved_job_of_an_episode_forgotten_by_id(backlog, tmp_path, monkeypatch):
    cur, paths = backlog
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--apply", "--owner-direction")
    assert rc == 0 and not paths["order"].exists()
    [record] = list(outbox.resolved_dir().glob("*.json"))
    after6 = deepcopy(cur.episodes[6])
    tombstones.publish_episode_ids({6})
    rc, undone = _run("--undo", out["owner_action"], "--owner-direction")
    assert rc == 0, undone
    assert cur.episodes[6] == after6
    assert not paths["order"].exists() and record.exists()
    assert undone["lines_left_resolved"] == [{"record": record.stem, "job": paths["order"].name,
                                              "why": "forgotten"}]


def test_a_job_is_not_archived_when_its_episode_is_forgotten_by_id_before_the_move(backlog, tmp_path,
                                                                                   monkeypatch):
    _cur, paths = backlog
    real_archive = outbox.archive_resolved

    def forget_first(jp, snapshot, record):
        tombstones.publish_episode_ids({6})
        return real_archive(jp, snapshot, record)

    monkeypatch.setattr(outbox, "archive_resolved", forget_first)
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--apply", "--owner-direction")
    assert rc == 0 and out["lines_resolved"] == 0, out
    assert paths["order"].exists()
    assert not list(outbox.resolved_dir().glob("*.json"))


def _fail_first_update(cur, monkeypatch):
    real = cur.execute
    state = {"done": False}

    def execute(sql, params=None):
        if " ".join(sql.split()).startswith("UPDATE episodes") and not state["done"]:
            state["done"] = True
            raise RuntimeError("hub dropped at the first UPDATE, after its entries were recorded")
        return real(sql, params)

    monkeypatch.setattr(cur, "execute", execute)


def test_undo_of_a_batch_that_never_committed_keeps_a_later_writers_identical_text(backlog, tmp_path,
                                                                                   monkeypatch):
    cur, _paths = backlog
    monkeypatch.setattr(outbox_preferences, "BATCH", 1)
    _fail_first_update(cur, monkeypatch)
    _direction(tmp_path, monkeypatch)
    assert cli.main(["outbox", "merge-line-preferences", "--apply", "--owner-direction"]) != 0
    line = next(x for x in owner_direction.actions() if x["action"] == "outbox.merge_preferences")
    assert line["state"] == "unconfirmed" and owner_direction.preference_merges(line["id"])
    cur.episodes[1]["raw"]["preferences"] = [*HUB["preferences"], *NEW]
    cur.episodes[1]["preferences"] = [*HUB["preferences"], *NEW]
    rc, undone = _run("--undo", line["id"], "--owner-direction")
    assert rc == 0 and undone["restored"] == 0 and undone["already_restored"] == 0, undone
    assert undone["unproven"] == [1] and "--include-unproven" in undone["next"], undone
    assert _prefs(cur, 1) == ([*HUB["preferences"], *NEW], [*HUB["preferences"], *NEW])


class _Killed(BaseException):
    pass


def _crash_at_second_marker(monkeypatch):
    real = owner_direction.record_preference_commits
    calls = {"n": 0}

    def marker(ids):
        calls["n"] += 1
        if calls["n"] == 2:
            raise _Killed()
        return real(ids)

    monkeypatch.setattr(owner_direction, "record_preference_commits", marker)


def test_undo_names_a_batch_that_committed_before_its_marker_and_stays_open(backlog, tmp_path, monkeypatch):
    cur, _paths = backlog
    monkeypatch.setattr(outbox_preferences, "BATCH", 1)
    _crash_at_second_marker(monkeypatch)
    _direction(tmp_path, monkeypatch)
    with pytest.raises(_Killed):
        cli.main(["outbox", "merge-line-preferences", "--apply", "--owner-direction"])
    line = next(x for x in owner_direction.actions() if x["action"] == "outbox.merge_preferences")
    merged = _prefs(cur, 2)
    rc, undone = _run("--undo", line["id"], "--owner-direction")
    assert rc == 0 and undone["restored"] == 1 and undone["already_restored"] == 0, undone
    assert undone["unproven"] == [2] and "--include-unproven" in undone["next"], undone
    assert _prefs(cur, 2) == merged
    assert owner_direction.line_state(line["id"]) != "undone"
    rc, refused = _run("--undo", line["id"], "--include-unproven")
    assert rc == 2 and refused["code"] == "owner_only", refused
    rc, second = _run("--undo", line["id"], "--include-unproven", "--owner-direction")
    assert rc == 0 and second["unproven"] == [] and second["unproven_reverted"] == [2], second
    assert second["restored"] == 1, second
    assert _prefs(cur, 2) != merged
    assert owner_direction.line_state(line["id"]) == "undone"
    logged = [x for x in owner_direction.read_log() if x.get("kind") == "undo"]
    assert any((x.get("facts") or {}).get("include_unproven") for x in logged)


def test_a_normal_undo_has_nothing_unproven(backlog, tmp_path, monkeypatch):
    _cur, _paths = backlog
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--apply", "--owner-direction")
    rc, undone = _run("--undo", out["owner_action"], "--owner-direction")
    assert rc == 0 and undone["unproven"] == [] and "next" not in undone, undone
    assert owner_direction.line_state(out["owner_action"]) == "undone"
