"""``khipu outbox span-covered``: a dead job refused for not announcing its source
identities, whose range and evidence another episode already holds, retires with
a record and never reaches the hub. Every database here is a hermetic one."""
from __future__ import annotations

import ast
import hashlib
import io
import json
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest import mock

import pytest

from khipu import capture, cli, db, outbox, outbox_span_covered as span, owner_act, owner_direction, owner_turns
from khipu import storage_bounds, tombstones
from khipu.capture_identity import payload_sha256
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.test_capture_replay_duplicate import REFUSED, bury, counts, land, pg, records  # noqa: F401

SESSION = "codex:span"
TEXT = "the decision only the forgotten row ever held"
COVER = {
    "ts": "2026-11-01T04:00:00Z", "summary": "The whole release work, reviewed end to end",
    "session_id": SESSION, "harness": "codex", "event": "stop", "transcript_range": "0:500",
    "project": "acme/span", "topics": [], "decisions": [TEXT, "Another decision"], "people": ["Ada"],
    "open_loops": [{"text": "Review the checklist", "kind": "followup", "owner": "assistant"}],
}
JOB = {
    "ts": "2026-11-01T01:00:00Z", "summary": "A part of the release work, before the rest",
    "session_id": SESSION, "harness": "codex", "event": "stop", "transcript_range": "100:200",
    "project": "acme/span", "topics": ["Some-Label"], "decisions": [TEXT], "people": ["Ada"],
    "open_loops": [{"text": "Review the checklist", "kind": "followup", "owner": "assistant"}],
}


def refused(**changes):
    return {**deepcopy(JOB), **changes}


def dead(payload=None, **fields):
    bury(refused() if payload is None else payload, reason="CheckViolation", **{**REFUSED, **fields})
    return outbox.dead_jobs()


def forget_row(hub_, episode):
    with hub_.connect() as conn:
        conn.execute("UPDATE episodes SET deleted_at = now() WHERE id = %s", (episode,))
        conn.commit()


def forget_for_real(hub_, episode, *, elsewhere=False):
    """The hub half of a forget. ``elsewhere``: made on another Mac, so this
    Mac's own list never heard of it."""
    from khipu import forget

    with hub_.connect() as conn, conn.cursor() as cur:
        out = forget.forget_episode(cur, episode)
        assert out.get("ok") is not False, out
        conn.commit()
    if elsewhere:
        tombstones.registry_path().unlink(missing_ok=True)


def retire():
    return span.apply(record_resolutions=lambda jobs: None)


# ---- eligible -------------------------------------------------------------------------------

def test_an_eligible_job_retires_with_a_record_and_the_hub_never_moves(pg):
    episode = land(COVER)
    (path,) = dead()
    snapshot = path.read_bytes()
    before = counts(pg)
    report = retire()
    assert report["ok"] and report["retired"] == 1 and report["held"] == {}, report
    assert outbox.dead_jobs() == [] and outbox.jobs() == []
    assert counts(pg) == before
    (record,) = records()
    assert record["resolution"]["kind"] == capture.SPAN_KIND == "span_covered"
    assert record["resolution"]["episode_id"] == episode
    assert record["queued_evidence"] == json.loads(snapshot)
    assert record["queued_evidence"]["payload"]["summary"] == JOB["summary"]
    assert (outbox.resolved_dir() / (hashlib.sha256(snapshot).hexdigest() + ".json")).exists()
    outbox._validate_resolution(record)
    assert outbox.is_resolved_payload(refused())


def test_a_row_of_equal_range_covers_and_a_merged_capture_counts_only_where_folded(pg):
    with_people = {**COVER, "people": [],
                   "merged_from": [{"payload": {"people": ["Ada"], "closed_loops": [{"text": "x"}]}}]}
    assert capture._span_lacks(with_people, refused()) == []
    assert capture._span_lacks(with_people, refused(closed_loops=[{"text": "x"}])) == ["closed_loops"]
    assert capture._span_lacks({**COVER, "transcript_range": "100:200"}, refused()) == []


@pytest.mark.parametrize("change", [
    {"payload": {**COVER, "transcript_range": "0:150"}},
    {"payload": {**COVER, "harness": "cursor"}},
    {"payload": {**COVER, "decisions": ["Another decision"]}},
    {"queued_evidence": "other_error"},
])
def test_a_forged_record_is_refused_on_read(pg, change):
    raw = {**COVER}
    queued = {"payload": refused(), "attempts": 25, **REFUSED}
    view = capture._redact_capture_values(refused())
    record = {"payload": raw, "queued_evidence": queued,
              "resolution": {"kind": "span_covered", "episode_id": 1, "resolved_at": "2026-11-03T00:00:00Z",
                             "queued_payload_sha256": payload_sha256(view)}}
    record["resolution"]["original_payload_sha256"] = payload_sha256(raw)
    outbox._validate_resolution(record)
    if change.get("queued_evidence"):
        record["queued_evidence"] = {**queued, "last_error": "CheckViolation: something else"}
    else:
        record["payload"] = change["payload"]
        record["resolution"]["original_payload_sha256"] = payload_sha256(change["payload"])
    with pytest.raises(ValueError):
        outbox._validate_resolution(record)


# ---- each hold reason -----------------------------------------------------------------------

def assert_held(pg_, reason):
    paths = outbox.dead_jobs()
    snapshots = [p.read_bytes() for p in paths]
    before = counts(pg_)
    assert span.dry_run()["would_hold"] == {reason: 1}
    report = retire()
    assert report["retired"] == 0 and report["held"] == {reason: 1}, report
    assert [p.read_bytes() for p in outbox.dead_jobs()] == snapshots
    assert records() == [] and counts(pg_) == before


def test_no_covering_row_holds(pg):
    dead()
    assert_held(pg, span.NO_COVERING_ROW)


def test_a_row_in_another_harness_is_no_covering_row(pg):
    land({**COVER, "harness": "cursor"})
    dead()
    assert_held(pg, span.NO_COVERING_ROW)


def test_a_range_not_contained_holds(pg):
    land({**COVER, "transcript_range": "150:500"})
    dead()
    assert_held(pg, span.RANGE_NOT_CONTAINED)


@pytest.mark.parametrize("value", ["abc", "100-200", "100:200\n", "200:100", "100:", ":200", "١٠:٢٠"])
def test_a_range_that_is_not_two_numbers_holds(pg, value):
    land(COVER)
    dead(refused(transcript_range=value))
    assert_held(pg, span.RANGE_NOT_NUMERIC)


@pytest.mark.parametrize("field,value", [
    ("decisions", ["A decision no row holds"]),
    ("people", ["Grace"]),
    ("open_loops", [{"text": "Something else", "kind": "followup", "owner": "user"}]),
    ("closed_loops", [{"text": "Finished something"}]),
    ("preferences", ["Short subjects"]),
    ("a_field_added_later", "still evidence"),
])
def test_missing_evidence_holds(pg, field, value):
    land(COVER)
    dead(refused(**{field: value}))
    assert_held(pg, span.EVIDENCE_MISSING)


def test_evidence_spread_over_two_rows_holds(pg):
    land({**COVER, "open_loops": []})
    land({**COVER, "ts": "2026-11-01T05:00:00Z", "summary": "Another span", "people": [], "decisions": []})
    dead()
    assert_held(pg, span.EVIDENCE_MISSING)


def test_an_overlapping_tombstone_holds_even_beside_a_covering_row(pg):
    land(COVER)
    other = land({**COVER, "ts": "2026-11-01T05:00:00Z", "summary": "The part later forgotten",
                  "harness": "cursor", "transcript_range": "150:250", "decisions": [], "open_loops": []})
    forget_row(pg, other)
    dead()
    assert_held(pg, span.FORGOTTEN)


def test_an_adjacent_tombstone_does_not_hold(pg):
    land(COVER)
    other = land({**COVER, "ts": "2026-11-01T05:00:00Z", "summary": "The next window",
                  "transcript_range": "200:300", "decisions": [], "open_loops": []})
    forget_row(pg, other)
    dead()
    assert retire()["retired"] == 1


@pytest.mark.parametrize("value", ["garbage", None])
def test_a_tombstone_whose_range_cannot_be_read_overlaps(pg, value):
    land(COVER)
    other = land({**COVER, "ts": "2026-11-01T05:00:00Z", "summary": "A forgotten row", "decisions": [],
                  "open_loops": [], "transcript_range": "300:400"})
    with pg.connect() as conn:
        conn.execute("UPDATE episodes SET transcript_range = %s, raw = raw || %s::jsonb, deleted_at = now() "
                     "WHERE id = %s", (value, json.dumps({"transcript_range": value}), other))
        conn.commit()
    dead()
    assert_held(pg, span.FORGOTTEN)


@pytest.mark.parametrize("elsewhere", [False, True])
def test_a_forgotten_covering_row_holds(pg, elsewhere):
    forget_for_real(pg, land(COVER), elsewhere=elsewhere)
    dead()
    assert_held(pg, span.FORGOTTEN)


def test_a_covering_row_forgotten_on_this_mac_alone_holds(pg):
    tombstones.publish_episode_ids({land(COVER)})
    dead()
    assert_held(pg, span.FORGOTTEN)


def test_a_covering_row_whose_source_is_forgotten_holds(pg, monkeypatch):
    # The hub's carrier gate refuses this state at commit, so only a forget
    # racing the read can show it: the check is asked of the row's raw too.
    land(COVER)
    dead()
    monkeypatch.setattr(tombstones, "hub_source_forgotten",
                        lambda cur, payload, source=None: payload.get("summary") == COVER["summary"])
    assert_held(pg, span.FORGOTTEN)


def test_a_job_whose_own_source_is_forgotten_holds(pg):
    land(COVER)
    dead()
    tombstones.publish_payloads([refused()])
    assert span.dry_run()["would_hold"] == {span.FORGOTTEN: 1}
    assert retire()["retired"] == 0 and records() == []


@pytest.mark.parametrize("error", [
    "CheckViolation: khipu: some other refusal",
    "CaptureEvidenceConflict: legacy episode key conflicts",
    "UniqueViolation: khipu: an episode write must announce its source identities",
    None,
])
def test_a_different_error_holds(pg, error):
    land(COVER)
    dead(last_error=error)
    assert_held(pg, span.DIFFERENT_ERROR)


# ---- a failed check holds -------------------------------------------------------------------

def test_a_hub_that_cannot_be_read_holds_everything(pg, monkeypatch):
    land(COVER)
    dead()
    monkeypatch.setattr(db, "connect", mock.Mock(side_effect=OSError("no route to hub")))
    out = span.dry_run()
    assert not out["ok"] and out["would_retire"] == 0 and out["would_hold"] == {span.CHECK_FAILED: 1}
    report = retire()
    assert not report["ok"] and report["retired"] == 0 and report["held"] == {span.CHECK_FAILED: 1}
    assert len(outbox.dead_jobs()) == 1 and records() == []
    assert span.retryable() == {}


def test_a_forget_list_that_cannot_be_read_holds_everything(pg, monkeypatch):
    land(COVER)
    dead()
    tombstones.registry_path().parent.mkdir(parents=True, exist_ok=True)
    tombstones.registry_path().write_text("{not json")
    assert span.dry_run()["would_hold"] == {span.CHECK_FAILED: 1}
    assert retire()["retired"] == 0 and len(outbox.dead_jobs()) == 1 and records() == []


def test_a_single_failed_check_holds_only_that_job(pg, monkeypatch):
    land(COVER)
    dead()
    monkeypatch.setattr(capture, "_span_lacks", mock.Mock(side_effect=RuntimeError("boom")))
    assert retire()["held"] == {span.CHECK_FAILED: 1}
    assert len(outbox.dead_jobs()) == 1


def test_an_unreadable_job_is_held_and_counted(pg):
    land(COVER)
    outbox.dead_dir().mkdir(parents=True, exist_ok=True)
    (outbox.dead_dir() / "torn.json").write_text("{not json")
    assert retire()["held"] == {span.CHECK_FAILED: 1}
    assert (outbox.dead_dir() / "torn.json").exists()


# ---- dry run --------------------------------------------------------------------------------

def run(*argv):
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli.main(["outbox", "span-covered", *argv])
    return rc, out.getvalue()


def test_the_dry_run_changes_nothing_and_says_counts_in_plain_words(pg):
    land(COVER)
    dead()
    dead(refused(transcript_range="abc", summary="A second one with a range that is no range"))
    dead(refused(summary="A third one, failed otherwise"), last_error="CheckViolation: other")
    before_files = sorted((p.name, p.read_bytes()) for p in outbox.dead_jobs())
    before = counts(pg)
    rc, text = run()
    assert rc == 0
    assert sorted((p.name, p.read_bytes()) for p in outbox.dead_jobs()) == before_files
    assert records() == [] and counts(pg) == before
    assert "Dry run: nothing was changed." in text
    assert "3 dead jobs. Would retire 1" in text and "Would hold 2." in text
    assert "  1: the job's transcript range is not two numbers in order" in text
    assert "  1: the job failed with a different error" in text
    for secret in (TEXT, JOB["summary"], "Ada", "A second one", SESSION):
        assert secret not in text
    rc, text = run("--json")
    out = json.loads(text)
    assert rc == 0 and out["would_retire"] == 1 and out["would_hold"] == {
        span.RANGE_NOT_NUMERIC: 1, span.DIFFERENT_ERROR: 1}
    assert TEXT not in text


def test_apply_says_what_it_did_in_plain_words(pg, monkeypatch):
    land(COVER)
    dead()
    monkeypatch.setattr(owner_direction, "record_preference_resolutions", lambda jobs: None)
    with mock.patch("khipu.routing.typed_yes", return_value=True):
        rc, text = run("--apply")
    assert rc == 0, text
    assert "1 dead job. Retired 1" in text and "Nothing was written to the hub" in text
    assert TEXT not in text and JOB["summary"] not in text


# ---- a crash between the record and the unlink ------------------------------------------------

def test_a_crash_before_the_record_leaves_the_whole_job(pg, monkeypatch):
    land(COVER)
    (path,) = dead()
    snapshot = path.read_bytes()
    monkeypatch.setattr(outbox, "_atomic_write", mock.Mock(side_effect=KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        retire()
    assert path.read_bytes() == snapshot
    assert not list(outbox.resolved_dir().glob("*"))


def test_a_crash_after_the_record_leaves_both_whole_and_the_next_run_finishes(pg, monkeypatch):
    land(COVER)
    (path,) = dead()
    snapshot = path.read_bytes()
    real = Path.unlink

    def crash(self, *a, **kw):
        if self == path:
            raise KeyboardInterrupt()
        return real(self, *a, **kw)

    with monkeypatch.context() as crashing:
        crashing.setattr(Path, "unlink", crash)
        with pytest.raises(KeyboardInterrupt):
            retire()
    assert path.read_bytes() == snapshot
    (record,) = records()
    outbox._validate_resolution(record)
    assert record["queued_evidence"] == json.loads(snapshot)
    report = retire()
    assert report["retired"] == 1 and outbox.dead_jobs() == []
    (again,) = records()
    assert again["resolution"]["resolved_at"] == record["resolution"]["resolved_at"]


def test_the_record_is_written_before_the_job_goes(pg, monkeypatch):
    land(COVER)
    (path,) = dead()
    order = []
    real_write, real_unlink = outbox._atomic_write, Path.unlink

    def write(dst, job):
        order.append("record")
        return real_write(dst, job)

    def unlink(self, *a, **kw):
        if self == path:
            order.append("job")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(outbox, "_atomic_write", write)
    monkeypatch.setattr(Path, "unlink", unlink)
    retire()
    assert order == ["record", "job"]


# ---- the drain never runs it ----------------------------------------------------------------

def test_the_drain_never_runs_it(pg, monkeypatch):
    land(COVER)
    (path,) = dead()
    snapshot = path.read_bytes()
    before = counts(pg)
    for name in ("apply", "dry_run", "retryable"):
        monkeypatch.setattr(span, name, mock.Mock(side_effect=AssertionError(f"the drain called {name}")))
    outbox.drain()
    assert path.read_bytes() == snapshot and records() == [] and counts(pg) == before


def test_only_the_cli_and_an_explicit_retry_import_it():
    root = Path(cli.__file__).parent
    importers = set()
    for module in root.glob("*.py"):
        if module.stem == "outbox_span_covered":
            continue
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [f"{node.module}.{a.name}" for a in node.names]
            if any("outbox_span_covered" in n for n in names):
                importers.add(module.stem)
    assert importers == {"cli", "outbox", "owner_direction"}
    source = (root / "outbox.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    (drain,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "drain"]
    assert "outbox_span_covered" not in ast.get_source_segment(source, drain)


# ---- retry-dead, and text that was forgotten ----------------------------------------------------

def test_retry_dead_holds_the_refused_kind_and_warns_with_a_count(pg):
    land(COVER)
    dead()
    bury(refused(summary="Another kind of dead job"), reason="x", last_error="OperationalError: gone")
    before = counts(pg)
    out = outbox.retry_dead()
    assert out["moved"] == 1 and out["announce_refused_held"] == 1
    assert out["warning"].startswith("1 dead job refused") and "span-covered" in out["warning"]
    assert "duplicates" in out["warning"] and TEXT not in out["warning"]
    assert len(outbox.dead_jobs()) == 1 and len(outbox.jobs()) == 1
    assert counts(pg) == before


def test_the_explicit_flag_moves_only_what_cannot_be_retired_and_touches_nothing_forgotten(pg):
    land(COVER)
    dead()  # covered: retires, never retries
    dead(refused(summary="Carries more than the row", decisions=["A decision no row holds"]))
    dead(refused(summary="Spans further", transcript_range="400:900"))
    out = outbox.retry_dead(include_announce_refused=True)
    assert out["moved"] == 2 and out["announce_refused_held"] == 1, out
    assert len(outbox.dead_jobs()) == 1
    assert retire()["retired"] == 1


def test_a_failed_read_retries_no_refused_job(pg, monkeypatch):
    dead()
    monkeypatch.setattr(db, "connect", mock.Mock(side_effect=OSError("no route")))
    out = outbox.retry_dead(include_announce_refused=True)
    assert out["moved"] == 0 and out["announce_refused_held"] == 1
    assert outbox.jobs() == []


def hub_text(hub_, text):
    with hub_.connect() as conn:
        return conn.execute("SELECT count(*) FROM episodes WHERE deleted_at IS NULL AND raw::text LIKE %s",
                            (f"%{text}%",)).fetchone()[0]


@pytest.mark.parametrize("elsewhere", [False, True])
def test_a_forgotten_text_job_cannot_reach_the_hub(pg, elsewhere):
    forget_for_real(pg, land(COVER), elsewhere=elsewhere)
    dead()
    assert hub_text(pg, TEXT) == 0
    for attempt in (retire, outbox.retry_dead, lambda: outbox.retry_dead(include_announce_refused=True), outbox.drain):
        attempt()
        assert hub_text(pg, TEXT) == 0
    assert len(outbox.dead_jobs()) == 1 and outbox.jobs() == [] and records() == []


def test_forgets_sweep_reads_both_identities_in_the_record(pg):
    from khipu.capture_stores import outbox_payloads

    land(COVER)
    dead()
    retire()
    (record,) = records()
    assert JOB["summary"] == record["queued_evidence"]["payload"]["summary"]
    assert len(outbox_payloads(record)) == 2


# ---- doctor's and the command's words -----------------------------------------------------------

def test_doctor_names_the_dry_run_first_and_that_retry_may_land_duplicates(pg):
    dead()
    status = outbox.status()
    assert status["announce_refused"] == 1
    assert status["fix"].startswith("Run `khipu outbox span-covered` first") and "duplicates" in status["fix"]
    text = storage_bounds.warnings({"outbox_dead": 1053, "capture_held": 0})[0]
    assert text.index("khipu outbox span-covered") < text.index("khipu outbox retry-dead")
    assert "dry run" in text and "duplicates" in text


def test_doctor_keeps_the_dry_run_first_for_this_kind_and_retry_for_any_other(pg):
    dead()
    assert cli._doctor_local_checks(None)["outbox"]["fix"] == outbox.SPAN_FIRST
    for path in outbox.dead_jobs():
        path.unlink()
    bury(refused(), reason="x", last_error="OperationalError: gone")
    assert outbox.status()["announce_refused"] == 0 and "fix" not in outbox.status()
    assert cli._doctor_local_checks(None)["outbox"]["fix"] == "run `khipu outbox retry-dead`"


def test_the_retry_dead_help_names_the_held_kind():
    out = io.StringIO()
    with redirect_stdout(out), pytest.raises(SystemExit):
        cli.main(["outbox", "retry-dead", "--help"])
    text = " ".join(out.getvalue().split())
    assert "--include-announce-refused" in text
    parser = cli.build_parser()
    assert parser.parse_args(["outbox", "retry-dead"]).include_announce_refused is False
    out = io.StringIO()
    with redirect_stdout(out), pytest.raises(SystemExit):
        cli.main(["outbox", "--help"])
    text = " ".join(out.getvalue().split())
    assert "stay held" in text and "duplicates" in text and "span-covered" in text


# ---- the owner's own yes ------------------------------------------------------------------------

@pytest.fixture
def owned(pg, tmp_path, monkeypatch, khipu_app):
    session = "s-span-owner"
    for name in ("KHIPU_SESSION_ID", "CODEX_THREAD_ID", "KHIPU_JOB", "KHIPU_PARENT_SESSION",
                 "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_HOST_SESSION_ID", "KHIPU_HARNESS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session)
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    khipu_app.as_shell("/opt/harness/claude")
    owner_turns.write_settings({"enabled": True})
    land(COVER)
    dead()
    return session


def say(tmp_path, session, words="please go ahead with that"):
    return owner_turns.record({"session_id": session, "prompt": words, "cwd": str(tmp_path)},
                              {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"})


def test_apply_without_a_yes_changes_nothing(owned, monkeypatch):
    with mock.patch("khipu.routing.typed_yes", return_value=False):
        rc, text = run("--apply", "--json")
    out = json.loads(text)
    assert rc == 2 and out["code"] == "owner_only" and "Nothing was changed" in out["error"]
    assert len(outbox.dead_jobs()) == 1 and records() == []


def test_a_recent_owner_turn_alone_does_not_pass_the_direction_flag(owned, tmp_path):
    say(tmp_path, owned)
    rc, text = run("--apply", "--json", "--owner-direction")
    out = json.loads(text)
    assert rc == 2 and out["code"] == "confirm_needed", out
    assert len(outbox.dead_jobs()) == 1 and records() == []


def test_owner_act_holds_even_after_a_recent_turn_until_his_answer_is_proven(owned, tmp_path):
    say(tmp_path, owned)
    ok, out = owner_act.act("outbox.span_covered", {})
    assert ok and out["pending"] and out["applied"] is False, out
    assert "nothing is written to the hub" in out["ask"]
    assert len(outbox.dead_jobs()) == 1 and records() == []
    # The turn before the ask is no answer to it.
    ok, refused_out = owner_act.act("direction.confirm", {"request": out["request"]})
    assert not ok and refused_out["code"] == "not_confirmed", refused_out
    assert len(outbox.dead_jobs()) == 1 and records() == []
    say(tmp_path, owned, "yes, retire them")
    ok, done = owner_act.act("direction.confirm", {"request": out["request"]})
    assert ok and done["retired"] == 1 and done["owner_action"], done
    assert outbox.dead_jobs() == [] and len(records()) == 1
    line = next(x for x in owner_direction.actions() if x["id"] == done["owner_action"])
    assert line["action"] == "outbox.span_covered" and line["auth"] == "direction" and line["state"] == "applied"
    assert [e["kind"] for e in owner_direction.preference_merges(line["id"], "preference_resolutions")] == [
        capture.SPAN_KIND]
    # Undo puts the job back whole, and the record goes.
    say(tmp_path, owned, "undo that please")
    result = owner_direction.undo(done["owner_action"], "direction")
    assert result["restored"] == 1, result
    assert len(outbox.dead_jobs()) == 1 and records() == []


def test_a_dispatched_job_cannot_even_hold_it(owned, tmp_path, monkeypatch):
    say(tmp_path, owned)
    monkeypatch.setenv("KHIPU_JOB", "1")
    ok, out = owner_act.act("outbox.span_covered", {})
    assert not ok and out["code"] == "no_recorded_turn", out
    assert len(outbox.dead_jobs()) == 1
