"""Job-origin repair uses synthetic data and a disposable PostgreSQL only."""
import io
import json
from contextlib import redirect_stdout

import pytest

from khipu import cli, commitments, db, owner_act, routing
from tests.hermetic_pg import hub, pg_cluster, chain_dir  # noqa: F401
from tests.fact_seed import adopt, announce


@pytest.fixture
def origin_db(hub, monkeypatch, tmp_path):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KHIPU_USER_ALIASES", "owner")
    commitments.reset_user_patterns()
    conn = hub.connect()
    # Real ordering: protected ownership metadata exists before the historical
    # correction boundary. Seeds below model captures within that interval.
    hub.migrate(directory=chain_dir(tmp_path, "0035"), conn=conn)
    conn.commit()
    hub.migrate(conn=conn)
    conn.commit()
    db._TABLE_COLUMNS_CACHE.clear()
    monkeypatch.setattr(db, "connect", lambda **kw: hub.connect())
    monkeypatch.setattr(routing, "_caller", lambda: {"app": False, "chain": ["test"]})
    monkeypatch.setattr(routing, "typed_yes", lambda prompt: False)
    yield conn
    commitments.reset_user_patterns()


def seed(conn, *, parent="session-parent", project="example/widget", **over):
    with conn.transaction(), conn.cursor() as cur:
        announce(cur)
        cur.execute("INSERT INTO episodes (ts, session_id, parent_session_id, summary, raw) "
                    "VALUES (now(), md5(random()::text), %s, 'Synthetic capture', '{}'::jsonb) RETURNING id", (parent,))
        episode = cur.fetchone()[0]
        cur.execute("SELECT a.applied_at + (b.applied_at - a.applied_at) / 2 "
                    "FROM schema_migrations a, schema_migrations b "
                    "WHERE a.version = '0035_commitment_owner_reassigned' "
                    "AND b.version = '0049_commitment_origin_requests'")
        historical_opened = cur.fetchone()[0]
        cur.execute("INSERT INTO commitments (project, text, owner, kind, opened_episode, opened_at, "
                    "content_hash) VALUES (%s, 'Should the synthetic fixture use a queue?', 'user', "
                    "'question', %s, %s, %s) RETURNING id", (project, episode, historical_opened, str(episode)))
        cid = cur.fetchone()[0]
        adopt(cur, "commitment", cid)
        # Stand in for a historical row present before migration 0049's
        # edit-evidence trigger was installed.
        cur.execute("UPDATE commitments SET origin_reviewed_at = NULL WHERE id = %s", (cid,))
        for name, value in over.items():
            assert name in {"due_after", "owner_reassigned_at", "status", "latest_text"}
            cur.execute(f"UPDATE commitments SET {name} = %s WHERE id = %s", (value, cid))
            if name == "latest_text":
                adopt(cur, "commitment", cid)
    return cid


def allow_app(monkeypatch):
    monkeypatch.setattr(routing, "_caller", lambda: {"app": True, "chain": ["app"], "where": "/Applications"})


def command(*args):
    ns = cli.build_parser().parse_args(["owed", "correct-job-origin", *args])
    out = io.StringIO()
    with redirect_stdout(out):
        rc = ns.func(ns)
    return rc, json.loads(out.getvalue())


def test_preview_request_confirm_and_undo(origin_db, monkeypatch):
    cid = seed(origin_db)
    seed(origin_db, parent=None)
    rc, preview = command()
    assert rc == 0 and preview["counts"]["total"] == 1
    assert preview["counts"]["by_kind"] == {"question": 1}
    assert preview["counts"]["by_project"] == {"example/widget": 1}
    assert preview["sample"][0]["id"] == cid
    with origin_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM commitment_origin_requests")
        assert cur.fetchone()[0] == 0
    rc, request = command("--request", "--owner-direction")
    assert rc == 0 and request["state"] == "pending"
    request_id = request["request"]
    rc, refused = command("--confirm", request_id, "--owner-direction")
    assert rc != 0 and refused["code"] == "no_recorded_turn"
    rc, refused = command("--confirm", request_id, "--from-app")
    assert rc != 0
    with origin_db.cursor() as cur:
        cur.execute("SELECT owner, origin FROM commitments WHERE id = %s", (cid,))
        assert cur.fetchone() == ("user", "conversation")
    allow_app(monkeypatch)
    rc, applied = command("--confirm", request_id, "--from-app")
    assert rc == 0 and applied["changed"] == 1
    assert applied["state"] == "applied"
    from khipu import owed_tidy, owner_direction
    line = next(a for a in owner_direction.actions() if a["action"] == "owed.correct_job_origin.confirm")
    assert line["before"]["batches"] == applied["batches"]
    assert owed_tidy.undo(applied["batches"][0], owner_gate="terminal")["restored"] == 1
    assert owed_tidy.redo(applied["batches"][0], owner_gate="direction")["code"] == "owner_confirmation_required"
    with origin_db.cursor() as cur:
        cur.execute("SELECT owner, origin FROM commitments WHERE id = %s", (cid,))
        assert cur.fetchone() == ("user", "conversation")
    assert command("--confirm", request_id, "--from-app")[1]["changed"] == 0


def test_owner_act_reaches_request_path_without_app_flag(monkeypatch):
    seen = []
    class Stop(Exception):
        pass
    class Parser:
        def parse_args(self, args):
            seen.append(args)
            raise Stop
    monkeypatch.setattr(cli, "build_parser", lambda: Parser())
    with pytest.raises(Stop):
        owner_act.act("owed.correct_job_origin", {})
    assert seen == [["owed", "correct-job-origin", "--request", "--owner-direction"]]


@pytest.mark.parametrize("fields", [
    {"due_after": "2020-02-01"}, {"owner_reassigned_at": "2020-02-01"},
    {"status": "closed"}, {"latest_text": "Owner changed the synthetic wording"},
])
def test_hand_changes_are_never_candidates(origin_db, fields):
    seed(origin_db, **fields)
    assert command()[1]["counts"]["total"] == 0


def test_changes_after_preview_are_skipped_and_other_projects_apply_atomically(origin_db, monkeypatch):
    first = seed(origin_db)
    second = seed(origin_db, project="example/other")
    third = seed(origin_db, project=None)
    _, pending = command("--request")
    with origin_db.transaction(), origin_db.cursor() as cur:
        commitments.set_status(cur, first, "closed", by="app")
        commitments.set_status(cur, first, "open", by="app")
    allow_app(monkeypatch)
    _, applied = command("--confirm", pending["request"], "--from-app")
    assert applied["changed"] == 2 and applied["skipped"] == [first]
    assert len(applied["batches"]) == 2
    from khipu import owner_direction
    line = next(a for a in owner_direction.actions() if a["action"] == "owed.correct_job_origin.confirm")
    assert owner_direction.undo(line["id"], "cli")["restored"] == 2
    with origin_db.cursor() as cur:
        cur.execute("SELECT owner, origin FROM commitments WHERE id = ANY(%s)", ([first, second, third],))
        assert cur.fetchall() == [("user", "conversation")] * 3


def test_owner_touch_after_correction_prevents_undo(origin_db, monkeypatch):
    cid = seed(origin_db)
    _, pending = command("--request")
    allow_app(monkeypatch)
    _, applied = command("--confirm", pending["request"], "--from-app")
    with origin_db.transaction(), origin_db.cursor() as cur:
        # Same-value edits must still be retained as owner choices.
        cur.execute("UPDATE commitments SET due_after = NULL WHERE id = %s", (cid,))
    from khipu import owed_tidy
    result = owed_tidy.undo(applied["batches"][0], owner_gate="terminal")
    assert result["restored"] == 0 and result["left_alone"][0]["id"] == cid


def test_application_rolls_back_all_projects_on_failure(origin_db, monkeypatch):
    first = seed(origin_db)
    seed(origin_db, project="example/other")
    _, pending = command("--request")
    allow_app(monkeypatch)
    original = commitments.reassign
    calls = []
    def fail_second(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("Synthetic failure")
        return original(*args, **kwargs)
    monkeypatch.setattr(commitments, "reassign", fail_second)
    with pytest.raises(RuntimeError, match="Synthetic failure"):
        command("--confirm", pending["request"], "--from-app")
    with origin_db.cursor() as cur:
        cur.execute("SELECT owner, origin FROM commitments WHERE id = %s", (first,))
        assert cur.fetchone() == ("user", "conversation")
        cur.execute("SELECT state FROM commitment_origin_requests WHERE id = %s", (pending["request"],))
        assert cur.fetchone()[0] == "pending"
        cur.execute("SELECT count(*) FROM commitment_tidy_items")
        assert cur.fetchone()[0] == 0


def test_mcp_cannot_confirm_without_recorded_owner_prompt(origin_db):
    seed(origin_db)
    ok, request = owner_act.act("owed.correct_job_origin", {})
    assert ok and request["state"] == "pending"
    ok, refused = owner_act.act("owed.correct_job_origin.confirm", {"request": request["request"]})
    assert not ok and refused["code"] == "no_recorded_turn"


def test_ambiguous_witness_or_owner_claim_is_excluded(origin_db):
    from khipu import provenance
    cid = seed(origin_db)
    with origin_db.transaction(), origin_db.cursor() as cur:
        provenance.record_claim(cur, "commitment", cid, "text", "Synthetic owner endorsement", claimant="owner")
    assert command()[1]["counts"]["total"] == 0


def test_verified_app_can_confirm_the_stored_preview(origin_db, monkeypatch):
    seed(origin_db)
    _, pending = command("--request")
    monkeypatch.setattr(routing, "_caller", lambda: {"app": True, "chain": ["app"], "where": "/Applications"})
    rc, result = command("--confirm", pending["request"], "--from-app")
    assert rc == 0 and result["changed"] == 1
    from khipu import owner_direction
    assert owner_direction.actions()[0]["auth"] == "app"


def test_lineage_change_after_request_does_not_apply(origin_db, monkeypatch):
    cid = seed(origin_db)
    _, pending = command("--request")
    with origin_db.transaction(), origin_db.cursor() as cur:
        cur.execute("UPDATE episodes SET parent_session_id = NULL WHERE id = "
                    "(SELECT opened_episode FROM commitments WHERE id = %s)", (cid,))
    allow_app(monkeypatch)
    _, result = command("--confirm", pending["request"], "--from-app")
    assert result["changed"] == 0 and result["skipped"] == [cid]


def test_preview_enforces_a_read_only_transaction(origin_db, monkeypatch):
    from khipu import owed_origin
    def try_to_write(cur, project):
        cur.execute("INSERT INTO commitment_origin_requests (id, preview) VALUES ('invalid', '{}'::jsonb)")
    monkeypatch.setattr(owed_origin, "preview", try_to_write)
    import psycopg
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        command()


def test_conflicting_capture_witness_is_ambiguous(origin_db):
    from khipu import provenance
    cid = seed(origin_db)
    with origin_db.transaction(), origin_db.cursor() as cur:
        announce(cur)
        cur.execute("INSERT INTO episodes (ts, session_id, summary, raw) "
                    "VALUES (now(), 'session-conversation', 'Synthetic restatement', '{}'::jsonb) RETURNING id")
        witness = cur.fetchone()[0]
        provenance.record_claim(cur, "commitment", cid, "restatement", "Synthetic later capture", episode_id=witness)
    assert command()[1]["counts"]["total"] == 0
