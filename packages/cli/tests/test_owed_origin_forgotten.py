"""Correction snapshots must not retain or replay forgotten wording."""
import json

import pytest

from khipu import forget, owed_origin, owed_tidy, owner_direction, provenance
from tests.fact_seed import adopt, announce
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.test_owed_origin_correction import origin_db, seed, command, allow_app  # noqa: F401


SECRET = "Synthetic forgotten wording must never escape its episode"
VISIBLE = "Should the synthetic visible fixture use a queue?"


def item(conn, text):
    cid = seed(conn)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("UPDATE commitments SET text = %s WHERE id = %s", (text, cid))
        adopt(cur, "commitment", cid)
        cur.execute("UPDATE commitments SET origin_reviewed_at = NULL WHERE id = %s", (cid,))
    return cid


def legacy_request(conn, doc, *, state="pending"):
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("INSERT INTO commitment_origin_requests(id, preview, state) "
                    "VALUES ('origin-legacy', %s::jsonb, %s)", (json.dumps(doc), state))
    return "origin-legacy"


def forget_item(conn, cid, tmp_path):
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT opened_episode FROM commitments WHERE id = %s", (cid,))
        eid = cur.fetchone()[0]
        out = forget.forget_episode(cur, eid, memory_root=tmp_path / "memory")
        assert out["ok"] and out["soft_deleted"]


def stored(conn):
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT preview FROM commitment_origin_requests ORDER BY id")
        return cur.fetchall()


def test_pending_legacy_preview_rechecks_forget_and_rebuilds_counts(origin_db, tmp_path):
    hidden = item(origin_db, SECRET)
    visible = item(origin_db, VISIBLE)
    legacy_request(origin_db, command()[1])
    forget_item(origin_db, hidden, tmp_path)
    rc, out = command("--requests")
    assert rc == 0
    assert SECRET not in json.dumps(out), "Pending request returns forgotten commitment wording"
    request = out["requests"][0]
    assert [entry["id"] for entry in request["items"]] == [visible]
    assert request["sample"] == request["items"]
    assert request["counts"] == {"total": 1, "by_kind": {"question": 1},
                                  "by_project": {"example/widget": 1}}
    assert SECRET not in json.dumps(command()[1])
    assert SECRET not in json.dumps(command("--request")[1])


def test_cached_wording_cannot_hide_behind_a_live_commitment(origin_db):
    cid = item(origin_db, VISIBLE)
    doc = command()[1]
    doc["items"][0]["text"] = SECRET
    doc["items"][0]["extra"] = SECRET
    doc["sample"] = [{"text": SECRET}]
    doc["counts"]["by_project"][SECRET] = 1
    doc["extra"] = SECRET
    legacy_request(origin_db, doc)
    out = command("--requests")[1]
    assert SECRET not in json.dumps(out)
    assert out["requests"][0]["items"][0]["id"] == cid
    assert out["requests"][0]["items"][0]["text"] == VISIBLE


def test_reprojected_live_witness_does_not_replay_old_wording(origin_db, tmp_path):
    cid = item(origin_db, SECRET)
    legacy_request(origin_db, command()[1])
    with origin_db.transaction(), origin_db.cursor() as cur:
        announce(cur)
        cur.execute("INSERT INTO episodes(ts, session_id, parent_session_id, summary, raw) "
                    "VALUES(now(), 'synthetic-survivor', 'session-parent', 'Synthetic', '{}') RETURNING id")
        survivor = cur.fetchone()[0]
        provenance.record_claim(cur, "commitment", cid, "text", VISIBLE, episode_id=survivor)
        provenance.project(cur, "commitment", cid)
    forget_item(origin_db, cid, tmp_path)
    out = command("--requests")[1]
    assert SECRET not in json.dumps(out)
    assert out["requests"][0]["items"] == []
    assert out["requests"][0]["counts"]["total"] == 0


def test_request_storage_keeps_only_references(origin_db):
    item(origin_db, SECRET)
    rc, out = command("--request")
    assert rc == 0 and out["items"][0]["text"] == SECRET
    assert SECRET not in json.dumps(stored(origin_db)), "A request must not retain episode wording"
    assert command("--requests")[1]["requests"][0]["items"][0]["text"] == SECRET


@pytest.mark.parametrize("state", ["pending", "applied"])
def test_forget_scrubs_legacy_request_records(origin_db, tmp_path, state):
    cid = item(origin_db, SECRET)
    legacy_request(origin_db, command()[1], state=state)
    forget_item(origin_db, cid, tmp_path)
    assert SECRET not in json.dumps(stored(origin_db)), "Forget left wording in a legacy request record"


def test_forget_refreshes_pre_migration_request_table_cache(origin_db, tmp_path):
    from khipu import db

    cid = item(origin_db, SECRET)
    legacy_request(origin_db, command()[1])
    # A long-lived forget process queried the table before migration 0049.
    db._TABLE_COLUMNS_CACHE["commitment_origin_requests"] = set()
    forget_item(origin_db, cid, tmp_path)
    assert SECRET not in json.dumps(stored(origin_db)), "A stale absent-table cache skipped request scrubbing"


def test_unchecked_visibility_fails_closed_without_logging_payload(origin_db, monkeypatch, capsys):
    item(origin_db, SECRET)
    legacy_request(origin_db, command()[1])
    def unavailable(*args, **kwargs):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(owed_origin, "_rows", unavailable)
    rc, out = command("--requests")
    assert rc != 0 and out["requests"] == []
    assert out["code"] == "visibility_unavailable"
    captured = capsys.readouterr()
    assert SECRET not in json.dumps(out) + captured.out + captured.err


@pytest.mark.parametrize("items", [None, [None], [{"id": "bad", "text": SECRET}]])
def test_malformed_legacy_preview_drops_unverifiable_fields(origin_db, items):
    legacy_request(origin_db, {"items": items, "sample": [{"text": SECRET}], "extra": SECRET})
    rc, out = command("--requests")
    assert rc == 0 and SECRET not in json.dumps(out)
    assert out["requests"][0]["items"] == []
    assert out["requests"][0]["sample"] == []
    assert out["requests"][0]["counts"]["total"] == 0


def test_forgotten_confirmation_and_undo_do_not_record_wording(origin_db, tmp_path, monkeypatch, capsys):
    cid = item(origin_db, SECRET)
    pending = command("--request")[1]
    allow_app(monkeypatch)
    applied = command("--confirm", pending["request"], "--from-app")[1]
    assert applied["changed"] == 1
    forget_item(origin_db, cid, tmp_path)
    undone = owed_tidy.undo(applied["batches"][0], owner_gate="app")
    assert undone["restored"] == 0
    retried = command("--confirm", pending["request"], "--from-app")[1]
    with origin_db.transaction(), origin_db.cursor() as cur:
        cur.execute("SELECT prior FROM commitment_tidy_items")
        prior = cur.fetchall()
        cur.execute("SELECT undo_report FROM commitment_tidy_batches")
        undo_reports = cur.fetchall()
    captured = capsys.readouterr()
    records = [stored(origin_db), prior, undo_reports, owner_direction.actions(), undone, retried]
    assert SECRET not in json.dumps(records, default=str) + captured.out + captured.err
