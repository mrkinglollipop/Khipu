"""Astra's round-2 findings on the memory pipeline privacy branch: a delayed
"Looks settled" suggestion quoting a forgotten decision source, a repeated
request after a compacted history, a stale vectors-only topic update, and
Windows paths in agent views."""
import json
import sqlite3

from khipu import agent_view, forget, hub_snapshot as hs, provenance, session_capture as sc
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401 - fixtures
from tests.test_astra_1026_capture import row, write
from tests.test_astra_1026_forget import PRIVATE, _ids, _owed
from tests.test_provenance_forget import _capture, _rows, env, pg  # noqa: F401 - fixtures
from tests.test_replica_pull_integrity import replica  # noqa: F401 - fixture


def test_compaction_followed_by_fresh_repeated_request(tmp_path):
    p = tmp_path / "transcript.jsonl"
    instruction = "Review the release patch and fix every correctness issue before merging it."
    old_reply = row("assistant", "The initial review is complete. " + "a" * 100)
    old_reply["message"]["id"] = "msg_old"
    old_user = row("user", instruction)
    old_user["uuid"] = "old-user"
    write(p, [old_user, old_reply])
    stats = {}
    _, offset, _ = sc.read_window(p, 0, seen=(), stats=stats)
    compact = {"type": "user", "isCompactSummary": True,
               "message": {"role": "user", "content": "This session is being continued. " + "s" * 80}}
    fresh_user = row("user", instruction)
    fresh_user["uuid"] = "fresh-user"
    fresh_reply = row("assistant", "The new review found an additional defect. " + "b" * 100)
    fresh_reply["message"]["id"] = "msg_new"
    with p.open("a") as f:
        for entry in [compact, old_user, old_reply, fresh_user, fresh_reply]:
            f.write(json.dumps(entry) + "\n")
    messages, _, users = sc.read_window(p, offset, seen=stats["fresh"])
    assert users == 1 and any(instruction in t for role, t in messages if role == "user"), (users, messages)
    # The re-emitted copies are still dropped.
    assert sum(instruction in t for role, t in messages if role == "user") == 1, messages
    assert not any("initial review" in t for _, t in messages), messages


def test_old_topic_vectors_without_source_cannot_replace_newer_body(replica):
    assert hs.upsert_embeddings([], topic_rows=[{"slug": "probe", "body": "NEW"}], read_at=200)["ok"]
    vec = {"profile": "p", "kind": "topic", "ref": "probe", "chunk_idx": 0,
           "chunk_text": "OLD PRIVATE BODY", "content_hash": "old", "embedding": [0.1] * 4,
           "built_at": "2026-10-25T10:00:00Z"}
    out = hs.upsert_embeddings([vec], replace_refs=[("p", "topic", "probe")], read_at=100)
    assert out["ok"], out
    with sqlite3.connect(hs.snapshot_path()) as con:
        assert con.execute("SELECT body FROM topics WHERE slug='probe'").fetchone() == ("NEW",)
        old = con.execute("SELECT chunk_text FROM memory_embeddings WHERE kind='topic' AND ref='probe'").fetchall()
    assert not old, old
    # Vectors read after that topic row still land.
    vec = dict(vec, chunk_text="NEW", content_hash="new")
    assert hs.upsert_embeddings([vec], replace_refs=[("p", "topic", "probe")], read_at=300)["embeddings"] == 1


def _reanchored(pg):
    pg.memory.mkdir(exist_ok=True)
    victim = _capture(loop=None, decision=PRIVATE)
    kept = _capture(loop=None, decision=None)
    earlier = _capture(loop="Choose the launch codename", kind="question", owner="user")
    cid, _ = _ids(pg, earlier)
    _, did = _ids(pg, victim)
    with pg.connect() as conn, conn.cursor() as cur:
        provenance.record_claim(cur, "decision", did, "text", "Public surviving decision text", episode_id=kept)
        provenance.project(cur, "decision", did)
    out = forget.forget_everywhere(victim, memory_root=pg.memory, gateway=False)
    assert out.get("soft_deleted"), out
    assert _rows(pg, "SELECT episode_id, text FROM decisions WHERE id=%s", (did,)) == [
        (kept, "Public surviving decision text")]
    return victim, cid, did


def test_delayed_suggestion_quoting_reanchored_decision_is_hidden(pg):
    _, cid, did = _reanchored(pg)
    # The loose decision rule read the old row before forget and now finishes
    # its write, as older code wrote it (no source episode).
    with pg.connect() as conn:
        conn.execute("UPDATE commitments SET settle_suggestion=%s::jsonb WHERE id=%s",
                     (json.dumps({"decision_id": did, "text": PRIVATE, "score": 0.7}), cid))
    result = _owed(pg)
    assert PRIVATE not in result, result


def test_delayed_sourced_suggestion_of_forgotten_source_is_hidden(pg):
    victim, cid, did = _reanchored(pg)
    with pg.connect() as conn:
        conn.execute("UPDATE commitments SET settle_suggestion=%s::jsonb WHERE id=%s",
                     (json.dumps({"decision_id": did, "text": PRIVATE, "score": 0.7, "episode": victim}), cid))
    result = _owed(pg)
    assert PRIVATE not in result, result


def test_unknown_windows_path_is_scrubbed():
    table = agent_view._Table(None, [], None, {}, {}, [])
    for text in (r"Read C:\Users\Alice\Private\client-list.txt", r"Open \\fileserver\share\client-list.txt"):
        result = agent_view._scrub_text(text, table, [])
        assert "client-list.txt" not in result, result
    assert agent_view._scrub_text("a 3:1 ratio", table, []) == "a 3:1 ratio"
