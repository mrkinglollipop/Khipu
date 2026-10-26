# ruff: noqa: F811 - pytest fixtures imported from test_provenance_forget
"""The replica's episode fence and its one fact writer (design section 6.2;
second Astra review 2026-10-23, findings 1 and 2).

Every import of episodes and their vectors goes through
``hub_snapshot._replica_write_gate`` and its ``_SourceFence``; every fact
row (commitments, decisions, deliverables, commitment vectors) reaches the
replica only through the fact copy (``copy_facts``: its lock, load rule
and one publication transaction).

The structural half enumerates every function that writes the replica's
memory tables and every import entry point, and pins how each one is
fenced, so a new import path that skips the mechanism breaks the build.
The behavioural half runs every import path against one schedule: a
public episode gains a merged capture whose source this Mac has forgotten
(the source is in the durable registry, the episode is live on the hub),
and asserts the merged capture's words never reach the replica's tables
and that the path entered the mechanism.
"""
from __future__ import annotations

import ast
import json
import re
import shutil
from contextlib import contextmanager
from pathlib import Path

import pytest

from khipu import capture, hub_snapshot as hs, tombstones
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.test_provenance_forget import env, pg, PROJECT, _capture, _payload  # noqa: F401
from tests.test_provenance_replica import hub_forget, replica, _replica, _replica_readers  # noqa: F401

PACKAGE = Path(hs.__file__).resolve().parent
SOURCE = (PACKAGE / "hub_snapshot.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)

# Every function in hub_snapshot.py that writes a replica memory table, and
# why it is safe. "fenced" means it is only ever reached inside a
# _replica_write_gate; "copy" that it is only reached from the fact copy's
# publication (checked below through the call graph).
MEMORY_WRITERS = {
    "_write_copy": "copy: the publication of a copy that passed the load rule",
    "_write_decision_rows": "copy: decision rows",
    "_write_commitment_rows": "copy: commitment rows",
    "_write_fact_deliverables": "copy: deliverable rows",
    "_insert_vector_row": "fenced or copy: a vector row its caller checked (never a fact vector outside the copy)",
    "_drop_facts": "removes: a restored replica's fact rows",
    "_upsert_episode_unfenced": "fenced: an episode patch (pull, capture, outbox replay)",
    "_write_pulled_tables": "fenced: the pull's topics and project aliases",
    "upsert_embeddings": "fenced: vectors, by their episode's identities; never a fact vector",
    "_scrub_forgotten": "fenced: the fence's whole-replica check",
    "_insert_episodes": "dump builder",
    "_insert_memory_embeddings": "dump builder: never a fact vector",
    "_apply_pending_erasures": "erasure: a forget, which only removes",
    "_erase_episode_commitments": "erasure",
    "forget_episode_in_snapshot": "erasure",
    "purge_topic_in_snapshot": "erasure: a topic purge",
}
DUMP_BUILDERS = {name for name, how in MEMORY_WRITERS.items() if how.startswith("dump builder")}
COPY_WRITERS = {name for name, how in MEMORY_WRITERS.items() if how.startswith("copy")}

# Every public way episodes reach the replica, and the function in it that
# opens the fenced write.
IMPORT_PATHS = {
    "upsert_episode": "_upsert_episode_unfenced",
    "upsert_embeddings": "upsert_embeddings",
    "pull_changes": "_write_pulled_tables",
    "refresh": "_refresh_locked",
    "publish_imported_snapshot": "publish_imported_snapshot",
    "scrub_forgotten_snapshot": "scrub_forgotten_snapshot",
}
# Every function that opens _replica_write_gate.
GATE_OPENERS = {"_upsert_episode_unfenced", "upsert_embeddings", "_write_pulled_tables",
                "_refresh_locked", "publish_imported_snapshot", "scrub_forgotten_snapshot"}
# Every public way facts reach the replica: each one runs the copy.
FACT_PATHS = {"pull_changes", "refresh", "sync_decision_changes", "scrub_forgotten_snapshot", "copy_facts"}

_WRITE = re.compile(r"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+(?:main\.)?"
                    r"(episodes|decisions|deliverables|commitments|memory_embeddings)\b", re.I)


def _text(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    return None


def _functions():
    return {fn.name: fn for fn in ast.walk(TREE) if isinstance(fn, ast.FunctionDef)}


def _called(fn) -> set[str]:
    return {c.func.attr if isinstance(c.func, ast.Attribute) else getattr(c.func, "id", None)
            for c in ast.walk(fn) if isinstance(c, ast.Call)}


def _callers(name: str) -> set[str]:
    return {f for f, fn in _functions().items() if name in _called(fn) and f != name}


def _opens_gate(fn) -> bool:
    return any(isinstance(n, ast.withitem) and isinstance(n.context_expr, ast.Call)
               and getattr(n.context_expr.func, "id", None) == "_replica_write_gate"
               for n in ast.walk(fn))


def test_every_replica_memory_writer_is_enumerated():
    found = {name for name, fn in _functions().items()
             if any((t := _text(n)) and "%s" not in t and _WRITE.search(t) for n in ast.walk(fn))}
    assert found == set(MEMORY_WRITERS), "a replica memory writer is not listed with how it is fenced"


def _reaches(entry: str) -> set[str]:
    fns, seen, todo = _functions(), set(), [entry]
    while todo:
        name = todo.pop()
        if name in seen or name not in fns:
            continue
        seen.add(name)
        todo.extend(_called(fns[name]))
    return seen


def test_every_import_path_opens_the_one_gate():
    fns = _functions()
    assert {name for name, fn in fns.items() if _opens_gate(fn)} == GATE_OPENERS
    for entry, opener in IMPORT_PATHS.items():
        assert opener in GATE_OPENERS
        assert opener in _reaches(entry), f"{entry} does not reach {opener}"
    for entry in FACT_PATHS:
        assert "copy_facts" in _reaches(entry), f"{entry} does not run the fact copy"


def test_fact_rows_have_one_writer_the_copys_publication():
    """Fact rows are written only by ``_write_copy``, reached only from
    ``_publish_copy`` inside the admitted publication, which checks the
    registry fingerprint, the invalidation revision and the destination
    first; the copy runs under ``fact-copy.lock``. Episode writers never
    write a fact row; the dump writes none."""
    fns = _functions()
    for name in COPY_WRITERS - {"_write_copy"}:
        assert _callers(name) == {"_write_copy"}, (name, _callers(name))
    assert _callers("_write_copy") == {"_publish_copy"}
    assert _callers("_publish_copy") == {"_copy_once"}
    assert _callers("_copy_once") == {"copy_facts"}
    copy = ast.get_source_segment(SOURCE, fns["copy_facts"])
    assert "_fact_copy_lock" in copy
    publish = ast.get_source_segment(SOURCE, fns["_publish_copy"])
    for check in ("_refusal_digest", "_revision", "_hub_destination", "missing", "unmet"):
        assert check in publish, check
    once = ast.get_source_segment(SOURCE, fns["_copy_once"])
    assert once.count("_admitted(") >= 3  # the endpoint clear, the incarnation clear, the publication
    for name in DUMP_BUILDERS:
        assert _callers(name) == {"_refresh_locked"}, name
    assert _callers("_upsert_episode_unfenced") == {"upsert_episode"}
    assert _callers("_scrub_forgotten") == {"scrub"}
    # A backup export copies only kept rows (``_copy_kept_rows``); it never drops facts in place.
    assert _callers("_drop_facts") == {"publish_imported_snapshot"}
    assert _callers("_copy_kept_rows") == {"export_replica_copy"}
    for name in ("_upsert_episode_unfenced", "upsert_embeddings"):
        assert "check" in _called(fns[name]), f"{name} does not check its episodes"
    for name in ("_upsert_episode_unfenced", "_write_pulled_tables", "_refresh_locked", "upsert_embeddings"):
        assert not COPY_WRITERS & _reaches(name), name
    for kind in ("_insert_memory_embeddings", "upsert_embeddings", "_pull_apply"):
        assert "_FACT_VECTOR_KINDS" in ast.get_source_segment(SOURCE, fns[kind]), kind


def test_every_anchor_read_carries_every_identity():
    """Each episode import reads its episodes' identities from the hub in
    its own snapshot, merged captures and source receipts included. The fact
    copy reads no identity: the hub matched every refusal (design 6.1), and
    the copy checks only that each registry string is a hub refusal and
    each bare id tombstoned or absent there."""
    fns = _functions()
    assert {"_read_receipts", "read_anchor_identities"} <= _called(fns["_pull_apply"])
    assert "_episode_refused" not in fns
    assert "_read_receipts" in _called(fns["read_anchor_identities"])
    assert "_anchor_identities" in _called(fns["read_anchor_identities"])
    assert "_anchor_identities" in _called(fns["_scrub_forgotten"])
    assert "_anchor_identities" in _called(fns["_upsert_episode_unfenced"])
    body = ast.get_source_segment(SOURCE, fns["_anchor_identities"])
    assert "merged_from" in body and "receipts" in body
    copy = ast.get_source_segment(SOURCE, fns["_copy_read"]) + hs._COPY_CHECK_SQL
    assert "forgotten_sources" in copy and "deleted_at IS NULL" in copy and "fact_copy_ready" in copy
    for identity_read in ("episode_sources", "merged_from", "_anchor_identities", "refuses", "episode_identities"):
        assert identity_read not in copy, identity_read


# ---- behaviour: every path, one schedule ------------------------------------------

MARKER = "Rotate PRIVATE-MERGED-FENCE-WORDS before the release"


def _private():
    return _payload(loop=MARKER, decision=MARKER, title=MARKER,
                    summary="A private merged capture", session_id="codex:fence-private")


def _merge(pg, eid, private, *, before_commit=lambda: None):
    """A real capture merge; ``before_commit`` runs while it is still open
    (the merge's own registry check passed before the source was forgotten)."""
    with pg.connect() as conn, conn.cursor() as cur:
        assert capture._merge_into_episode(cur, eid, private, matched_via="review", score=1.0)
        before_commit()
        conn.commit()


def _leaked() -> list[str]:
    tables = {"decisions": "text", "deliverables": "title", "commitments": "text",
              "memory_embeddings": "chunk_text"}
    found = [t for t, col in tables.items()
             if any(MARKER in str(v) for (v,) in _replica(f"SELECT {col} FROM {t}"))]
    if MARKER in _replica_readers():
        found.append("readers")
    return found


@pytest.fixture
def gate_spy(monkeypatch):
    """Records each entry into the episode gate ("gate") and the copy lock
    ("copy")."""
    opened: list[str] = []
    real_gate, real_copy = hs._replica_write_gate, hs._fact_copy_lock

    @contextmanager
    def gate(*args, **kwargs):
        opened.append("gate")
        with real_gate(*args, **kwargs) as fence:
            yield fence

    @contextmanager
    def copy(*args, **kwargs):
        opened.append("copy")
        with real_copy(*args, **kwargs):
            yield

    monkeypatch.setattr(hs, "_replica_write_gate", gate)
    monkeypatch.setattr(hs, "_fact_copy_lock", copy)
    return opened


def _vector_row(eid):
    return {"profile": "fence", "kind": "episode", "ref": str(eid), "chunk_idx": 0, "chunk_text": MARKER,
            "content_hash": "x", "embedding": [1.0, 0.0], "built_at": "2026-10-23T12:00:00+00:00"}


def _episode_patch(pg, eid):
    with pg.connect() as conn, conn.cursor() as cur:
        row = cur.execute("SELECT to_jsonb(e) FROM episodes e WHERE id = %s", (eid,)).fetchone()[0]
        receipts = hs._read_receipts(cur, [eid]).get(eid, [])
        conn.rollback()
    return hs.upsert_episode(row, [_vector_row(eid)], receipts=receipts)


def _vectors(pg, eid):
    with pg.connect() as conn, conn.cursor() as cur:
        anchors = hs.read_anchor_identities(cur, [eid])
        conn.rollback()
    return hs.upsert_embeddings([_vector_row(eid)], anchors=anchors)


# Each path and the mechanism it must enter.
IMPORTS = {
    "copy_facts": (lambda pg, eid: hs.copy_facts(), {"copy"}),
    "sync_decision_changes": (lambda pg, eid: hs.sync_decision_changes(), {"copy"}),
    "pull_changes": (lambda pg, eid: hs.pull_changes(include_ledger=False), {"gate", "copy"}),
    "refresh": (lambda pg, eid: hs.refresh(), {"gate", "copy"}),
    "upsert_episode": (_episode_patch, {"gate"}),
    "upsert_embeddings": (_vectors, {"gate"}),
}


@pytest.mark.parametrize("path", sorted(IMPORTS))
def test_every_import_path_fences_a_forgotten_merged_source(pg, replica, gate_spy, path):
    """A live public episode gains a merged capture on the hub whose source
    this Mac forgets while the merge is open, before the replica's last
    write (so no registry change is left to trigger a scrub). Each import
    path alone must refuse its words: the episode by the fence, the facts
    because the hub lacks this Mac's refusal (the copy lists it)."""
    replica(pg)
    eid = _capture(loop=None)
    private = _private()

    def forget_then_dump():
        tombstones.publish_payloads([private])
        assert hs.refresh()["ok"]

    _merge(pg, eid, private, before_commit=forget_then_dump)
    with pg.connect() as conn:
        assert conn.execute("SELECT count(*) FROM decisions").fetchone() == (1,)
    gate_spy.clear()
    run, entered = IMPORTS[path]
    run(pg, eid)
    assert entered <= set(gate_spy), f"{path} did not go through {entered - set(gate_spy)}"
    assert _leaked() == [], path


@pytest.mark.parametrize("path", ["publish_imported_snapshot", "scrub_forgotten_snapshot"])
def test_imported_and_scrubbed_replicas_fence_a_merged_source(pg, replica, gate_spy, path, tmp_path):
    """A replica copy made before the source was forgotten holds its words;
    the backup import drops every fact row of the copy it restores, and the
    scrub refuses the episode by the merged identity."""
    replica(pg)
    eid = _capture(loop=None)
    private = _private()
    _merge(pg, eid, private)
    assert hs.refresh()["ok"]
    assert "decisions" in _leaked()
    staged = hs.staging_path()
    shutil.copyfile(hs.snapshot_path(), staged)
    tombstones.publish_payloads([private])
    gate_spy.clear()
    if path == "publish_imported_snapshot":
        out = hs.publish_imported_snapshot(staged)
    else:
        staged.unlink()
        out = hs.scrub_forgotten_snapshot()
    assert out["ok"], out
    assert "gate" in gate_spy
    assert _leaked() == []


def test_identities_cover_merged_payloads_and_receipts():
    """Unit: the identity set names the merged capture by payload and by
    receipt hash, and a source receipt by its key and hash."""
    from khipu.tombstones import identity_strings

    merged = {"ts": "2026-10-23T10:00:00Z", "summary": "merged", "harness": "codex",
              "session_id": "codex:m", "transcript_range": "0:1"}
    row = {"id": 1, "raw": json.dumps({"ts": "2026-10-23T09:00:00Z", "summary": "own",
                                       "merged_from": [{"source_id": "abc", "payload": merged}]})}
    ids = hs._anchor_identities(row, [("key-1", "sha-1")])
    assert identity_strings(merged, redact=False) <= ids
    assert {"h:abc", "k:key-1", "h:sha-1"} <= ids


# ---- contributors: every path, multi-episode facts (third Astra review #1) --------

def test_the_retired_replica_mechanisms_are_gone():
    """The rebuild rule, the wait rule and the incremental re-fence are
    removed (design 6.6, "Removed"), with every table they kept."""
    fns_and_classes = {n.name for n in ast.walk(TREE) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    for retired in ("_fence_fact_wording", "_store_fact_sources", "_facts_sourced_by", "_ScrubGate",
                    "_index_identities", "apply_fact_rows", "read_fact_sources", "_fact_source_sql",
                    "fact_source_episodes", "_commitment_anchors", "_dumped_identities", "supports",
                    "sync_fact_changes", "rebuild_facts", "read_refusal_wait", "_admit_fact",
                    "_fence_dumped_facts", "_apply_fact_stream", "_read_fact_stream", "_load_facts",
                    "apply_decision_changes", "_write_deliverable_rows", "_patch_covers_stream"):
        assert retired not in fns_and_classes, retired
    for name in ("facts_confirmed", "drop_facts", "wait_report"):
        assert not hasattr(hs._SourceFence, name), name
    for table in ("replica_anchor_sources", "replica_fact_sources", "replica_source_index",
                  "replica_fence_applied"):
        assert table in hs._RETIRED_FACT_TABLES
    from khipu import forget, provenance

    for name in ("confirm_from_hub", "pending_forgets", "send_pending"):
        assert not hasattr(forget, name), name
    for name in ("record_confirmations", "_union_confirmations"):
        assert not hasattr(tombstones, name), name
    assert "fact_changes_floor" not in ast.get_source_segment(
        (PACKAGE / "provenance.py").read_text(encoding="utf-8"),
        next(n for n in ast.walk(ast.parse((PACKAGE / "provenance.py").read_text(encoding="utf-8")))
             if isinstance(n, ast.FunctionDef) and n.name == "prune_changes"))
    assert provenance.prune_changes


SHARED_DECISION = "Ship the shared release checklist"
SHARED_TITLE = "Shared release checklist"
B_WORDS = "Review the deployment checklist before the release goes out"
A_WORDS = "Review the deployment checklist before release"

CONTRIBUTOR_IMPORTS = {
    "copy_facts": (lambda pg, a: hs.copy_facts(), {"commitment", "decision", "deliverable"}),
    "sync_decision_changes": (lambda pg, a: hs.sync_decision_changes(), {"commitment", "decision", "deliverable"}),
    "pull_changes": (lambda pg, a: hs.pull_changes(include_ledger=False),
                     {"commitment", "decision", "deliverable"}),
    "refresh": (lambda pg, a: hs.refresh(), {"commitment", "decision", "deliverable"}),
}


@pytest.mark.parametrize("path", sorted(CONTRIBUTOR_IMPORTS))
def test_every_import_path_keeps_and_fences_every_contributor(pg, replica, gate_spy, path):
    """Two separate live episodes share each fact: B restates A's
    commitment (its latest_text) and repeats A's decision and deliverable
    word for word. This Mac has forgotten B's source; the hub is unchanged.
    Each fact path alone publishes no copy while the hub still holds B live
    (the hub lacks this Mac's refusal); once the hub's forget re-projects
    the facts, the same path shows A's words and never B's."""
    replica(pg)
    assert hs.refresh()["ok"]
    a = _capture(loop=A_WORDS, decision=SHARED_DECISION, title=SHARED_TITLE)
    bpayload = _payload(loop=B_WORDS, decision=SHARED_DECISION, title=SHARED_TITLE)
    b = capture.write_pg(dict(bpayload))["episode_id"]
    assert a != b
    with pg.connect() as conn:
        assert conn.execute("SELECT opened_episode, latest_text FROM commitments").fetchall() == [(a, B_WORDS)]
        claimants = {(k, e) for k, e in conn.execute(
            "SELECT kind, episode_id FROM fact_claims WHERE claimant = 'episode'").fetchall()}
    assert {("commitment", b), ("decision", b), ("deliverable", b)} <= claimants, claimants
    tombstones.publish_payloads([bpayload])
    gate_spy.clear()
    run, kinds = CONTRIBUTOR_IMPORTS[path]
    run(pg, a)
    assert "copy" in gate_spy, f"{path} did not run the fact copy"

    def tables():
        return json.dumps([_replica("SELECT * FROM commitments"), _replica("SELECT * FROM decisions"),
                           _replica("SELECT * FROM deliverables")])

    # Listed: no fact row of this copy, B's words least of all.
    assert B_WORDS not in tables() and B_WORDS not in _replica_readers(), path
    assert SHARED_DECISION not in tables() and A_WORDS not in tables(), path
    # The hub catches up; the same path alone brings A's words back.
    hub_forget(pg, b)
    gate_spy.clear()
    run(pg, a)
    assert "copy" in gate_spy, path
    assert B_WORDS not in tables() and B_WORDS not in _replica_readers(), path
    readers = _replica_readers()
    if "decision" in kinds:
        assert SHARED_DECISION in readers, path
    if "deliverable" in kinds:
        assert SHARED_TITLE in readers, path
    if "commitment" in kinds:
        assert A_WORDS in readers, path
