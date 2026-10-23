"""Real writers, real migrations and disposable loopback PostgreSQL."""
import json

import pytest

from khipu import commitments, db, decisions, deliverables, forget
from tests.hermetic_pg import chain_dir, hub, pg_cluster  # noqa: F401

PROJECT = "audit/kept"
OWED = "I will review the retention policy before the production release"
DECISION = "Keep the public API stable for the next release"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for key, folder in (("KHIPU_DATA_DIR", "data"), ("KHIPU_CAPTURE_HOME", "capture"),
                        ("KHIPU_OUTBOX", "outbox")):
        monkeypatch.setenv(key, str(tmp_path / folder))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_TEST", "1")
    db._TABLE_COLUMNS_CACHE.clear()


def seed(hub, *, directory=None, second_text=OWED):
    conn = hub.connect()
    hub.migrate(conn=conn, directory=directory)
    db._TABLE_COLUMNS_CACHE.clear()
    cur = conn.cursor()
    ids, stats = [], []
    for n in (1, 2):
        payload = {
            "ts": f"2026-10-2{n}T09:00:00Z", "summary": f"Independent source {n}",
            "project": PROJECT, "harness": "codex", "session_id": f"codex:independent-{n}",
            "transcript_range": "0:100",
            "open_loops": [{"text": OWED if n == 1 else second_text, "owner": "user", "kind": "promise"}],
            "decisions": [DECISION],
            "decision_details": [{"text": DECISION, "by": "user", "rationale": f"rationale source {n}"}],
            "deliverables": [{"kind": "pr", "url": "https://example.invalid/acme/a/pull/42",
                              "title": f"deliverable source {n}"}],
        }
        cur.execute(
            "INSERT INTO episodes(ts, summary, project, harness, session_id, transcript_range, raw, decisions) "
            "VALUES (%s::timestamptz,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb) RETURNING id",
            (payload["ts"], payload["summary"], PROJECT, payload["harness"], payload["session_id"],
             payload["transcript_range"], json.dumps(payload), json.dumps(payload["decisions"])),
        )
        eid = cur.fetchone()[0]
        ids.append(eid)
        stats.append((commitments.open_from_episode(cur, payload, eid),
                      decisions.insert_decisions_from_episode(cur, payload, eid),
                      deliverables.insert_deliverables_from_episode(cur, payload, eid)))
    conn.commit()
    assert stats == [(1, 1, 1), (0, 0, 0)]
    return conn, cur, ids


def read(cur):
    return {
        "owed": commitments.list_owed(cur, project=PROJECT),
        "decisions": decisions.list_decisions(cur, project=PROJECT, status="standing"),
        "deliverables": deliverables.recent_deliverables(cur, project=PROJECT),
    }


def assert_kept(cur, eid):
    after = read(cur)
    assert {k: len(v) for k, v in after.items()} == {"owed": 1, "decisions": 1, "deliverables": 1}
    assert after["owed"][0]["opened_episode"] == eid
    assert after["decisions"][0]["episode_id"] == eid
    assert after["deliverables"][0]["episode_id"] == eid


def do_forget(cur, eid, tmp_path):
    return forget.forget_episode(cur, eid, memory_root=tmp_path / "memory")


def test_forget_preserves_kept_episode_shared_derivations(hub, tmp_path):
    conn, cur, ids = seed(hub)
    assert all(read(cur).values())
    result = do_forget(cur, ids[0], tmp_path)
    conn.commit()
    assert result["commitments_closed"] == result["decisions_retracted"] == result["deliverables_removed"] == 0
    assert_kept(cur, ids[1])
    cur.execute("SELECT rationale, evidence, session_id FROM decisions")
    rationale, evidence, session = cur.fetchone()
    assert rationale == "rationale source 2"
    assert evidence["episode_id"] == ids[1]
    assert session == "codex:independent-2"
    cur.execute("SELECT title FROM deliverables")
    assert cur.fetchone()[0] == "deliverable source 2"
    cur.execute("SELECT seen_count FROM commitments")
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT count(*) FROM fact_support WHERE episode_id = %s", (ids[0],))
    assert cur.fetchone()[0] == 0
    repeated = do_forget(cur, ids[0], tmp_path)
    assert not repeated["soft_deleted"]
    assert_kept(cur, ids[1])


def test_forget_alternate_then_last_source(hub, tmp_path):
    conn, cur, ids = seed(hub)
    do_forget(cur, ids[1], tmp_path)
    assert_kept(cur, ids[0])
    do_forget(cur, ids[1], tmp_path)
    assert_kept(cur, ids[0])
    final = do_forget(cur, ids[0], tmp_path)
    conn.commit()
    assert final["commitments_closed"] == final["decisions_retracted"] == final["deliverables_removed"] == 1
    assert read(cur) == {"owed": [], "decisions": [], "deliverables": []}
    cur.execute("SELECT count(*) FROM fact_support")
    assert cur.fetchone()[0] == 0


def test_forget_preserves_owner_edits_and_inactive_state(hub, tmp_path):
    _, cur, ids = seed(hub)
    cur.execute(
        "UPDATE commitments SET text = 'owner edited text', latest_text = 'owner latest words', "
        "owner = 'assistant', owner_reassigned_at = now(), status = 'closed', close_reason = 'owner close'"
    )
    cur.execute(
        "UPDATE decisions SET text = 'owner decision', rationale = 'owner rationale', "
        "retracted_at = now(), retract_reason = 'owner retract'"
    )
    cur.execute("UPDATE deliverables SET title = 'owner title'")
    do_forget(cur, ids[0], tmp_path)
    cur.execute("SELECT text,latest_text,owner,status,close_reason,opened_episode FROM commitments")
    assert cur.fetchone() == ("owner edited text", "owner latest words", "assistant", "closed", "owner close", ids[1])
    cur.execute("SELECT text,rationale,retract_reason,episode_id FROM decisions")
    assert cur.fetchone() == ("owner decision", "owner rationale", "owner retract", ids[1])
    cur.execute("SELECT title,episode_id FROM deliverables")
    assert cur.fetchone() == ("owner title", ids[1])
    do_forget(cur, ids[1], tmp_path)
    cur.execute("SELECT status,close_reason FROM commitments")
    assert cur.fetchone() == ("closed", "owner close")
    cur.execute("SELECT retract_reason FROM decisions")
    assert cur.fetchone()[0] == "owner retract"


def test_legacy_exact_raw_support_is_recovered(hub, tmp_path):
    conn, cur, ids = seed(hub, directory=chain_dir(tmp_path, "0044"))
    hub.migrate(conn=conn)
    db._TABLE_COLUMNS_CACHE.clear()
    do_forget(cur, ids[0], tmp_path)
    assert_kept(cur, ids[1])
    do_forget(cur, ids[1], tmp_path)
    assert read(cur) == {"owed": [], "decisions": [], "deliverables": []}


def test_paraphrase_support_replaces_forgotten_wording(hub, tmp_path):
    kept = "I will review the retention policy before the upcoming production release"
    _, cur, ids = seed(hub, second_text=kept)
    do_forget(cur, ids[0], tmp_path)
    assert_kept(cur, ids[1])
    cur.execute("SELECT text,latest_text,content_hash FROM commitments")
    assert cur.fetchone() == (kept, None, commitments.content_hash(PROJECT, kept))


def test_legacy_does_not_invent_paraphrase_support(hub, tmp_path):
    conn, cur, ids = seed(
        hub, directory=chain_dir(tmp_path, "0044"),
        second_text="I will review the retention policy before the upcoming production release",
    )
    hub.migrate(conn=conn)
    db._TABLE_COLUMNS_CACHE.clear()
    do_forget(cur, ids[0], tmp_path)
    assert read(cur)["owed"] == []
    # Proven exact decision and URL evidence are still retained.
    assert len(read(cur)["decisions"]) == len(read(cur)["deliverables"]) == 1


@pytest.mark.parametrize("row_lock", ["FOR UPDATE", "FOR NO KEY UPDATE"])
def test_legacy_recovery_refuses_busy_source_then_retries_without_loss(hub, tmp_path, row_lock):
    """A kept capture cannot deadlock forget or suffer partial fact removal."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from khipu import fact_support

    conn, cur, ids = seed(hub, directory=chain_dir(tmp_path, "0044"))
    hub.migrate(conn=conn)
    conn.commit()
    db._TABLE_COLUMNS_CACHE.clear()
    fact_support.lock(cur)
    holding_episode = Event()
    other = hub.connect()

    def capture_kept():
        with other.cursor() as capture:
            capture.execute("SET LOCAL statement_timeout = '5s'")
            capture.execute(f"SELECT raw FROM episodes WHERE id = %s {row_lock}", (ids[1],))
            payload = capture.fetchone()[0]
            holding_episode.set()
            # Waits on the support lock while holding the kept episode row.
            result = commitments.open_from_episode(capture, payload, ids[1])
        other.commit()
        return result

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(capture_kept)
        try:
            assert holding_episode.wait(5)
            cur.execute("SET LOCAL statement_timeout = '2s'")
            with pytest.raises(fact_support.FactSupportBusy, match="roll back and retry forget"):
                do_forget(cur, ids[0], tmp_path)
        finally:
            # Release the mutex even when a forbidden FK inversion times out.
            conn.rollback()
        assert future.result(timeout=10) == 0
    assert_kept(cur, ids[0])
    cur.execute("SELECT count(*) FROM episodes WHERE deleted_at IS NULL")
    assert cur.fetchone()[0] == 2
    from khipu.tombstones import forgotten_episode_ids

    assert ids[0] not in forgotten_episode_ids()
    do_forget(cur, ids[0], tmp_path)
    assert_kept(cur, ids[1])


def test_same_source_variants_are_removed_without_losing_kept_wording(hub, tmp_path):
    _, cur, ids = seed(hub)
    old_variant = "I will review the retention policy before the upcoming production release"
    cur.execute("SELECT raw FROM episodes WHERE id = %s", (ids[0],))
    payload = cur.fetchone()[0]
    payload["open_loops"][0]["text"] = old_variant
    assert commitments.open_from_episode(cur, payload, ids[0]) == 0
    do_forget(cur, ids[0], tmp_path)
    cur.execute("SELECT text,latest_text FROM commitments")
    assert cur.fetchone() == (OWED, None)
    assert_kept(cur, ids[1])


@pytest.mark.parametrize("owner_metadata", [False, True])
def test_forgotten_restatement_removes_only_its_added_pr_requirements(hub, tmp_path, monkeypatch, owner_metadata):
    from khipu import close_rules, config

    config.set_choice_setting("owed_pr_check_github", "off")
    monkeypatch.setattr(close_rules, "_project_repo", lambda project: "acme/widget")
    conn = hub.connect()
    hub.migrate(conn=conn)
    cur = conn.cursor()
    ids = []
    for n, text in enumerate((
        "Land PR #42 before the production release ships",
        "Land PR #42 and PR #43 before the production release ships",
        "Merge pull request #43 from audit/private-branch",
    ), 1):
        payload = {"ts": f"2026-10-2{n}T09:00:00Z", "summary": text, "project": PROJECT,
                   "session_id": f"codex:pr-source-{n}", "harness": "codex", "transcript_range": "0:100",
                   "open_loops": [{"text": text, "kind": "blocker", "owner": "assistant"}] if n < 3 else []}
        cur.execute(
            "INSERT INTO episodes(ts,summary,project,session_id,harness,transcript_range,raw) "
            "VALUES (%s::timestamptz,%s,%s,%s,%s,%s,%s::jsonb) RETURNING id",
            (payload["ts"], text, PROJECT, payload["session_id"], "codex", "0:100", json.dumps(payload)),
        )
        ids.append(cur.fetchone()[0])
        assert commitments.open_from_episode(cur, payload, ids[-1]) == (1 if n == 1 else 0)
    conn.commit()
    cur.execute("SELECT prs, pr_check FROM commitments")
    prs, check = cur.fetchone()
    assert prs == [42, 43]
    assert check["refs"] == ["acme/widget#42", "acme/widget#43"]
    if owner_metadata:
        check.update({"refs": check["refs"] + ["acme/widget#99"], "owner_note": "keep this",
                      "states": {"43": {"state": "merged", "source": "github"}}})
        cur.execute("UPDATE commitments SET prs = ARRAY[42,43,99], pr_check = %s::jsonb",
                    (json.dumps(check),))
    do_forget(cur, ids[1], tmp_path)
    cur.execute("SELECT prs,pr_check,latest_text FROM commitments")
    prs, check, latest = cur.fetchone()
    assert prs == ([42, 99] if owner_metadata else [42])
    assert check["refs"] == (["acme/widget#42", "acme/widget#99"] if owner_metadata else ["acme/widget#42"])
    if owner_metadata:
        assert check["owner_note"] == "keep this"
        assert check["states"] == {"43": {"state": "merged", "source": "github"}}
    assert latest is None
    report = close_rules.run(cur)
    assert report["closed_pr"] == report["github_asked"] == 0
    cur.execute("SELECT status,closed_episode FROM commitments")
    assert cur.fetchone() == ("open", None)

    if not owner_metadata:
        # The kept requirement can still close once its own live proof appears.
        cur.execute(
            "INSERT INTO episodes(ts,summary,project,raw) "
            "VALUES ('2026-10-24T09:00:00Z','Merge pull request #42 from audit/live',%s,'{}')",
            (PROJECT,),
        )
        assert close_rules.run(cur)["closed_pr"] == 1
