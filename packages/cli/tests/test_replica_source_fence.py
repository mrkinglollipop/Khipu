# ruff: noqa: F811 - pytest fixtures imported from test_provenance_forget
"""The replica's one source fence (design section 6; second Astra review
2026-10-23, findings 1 and 2): every import into the replica goes through
``hub_snapshot._replica_write_gate`` and its ``_SourceFence``.

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

from khipu import capture, decisions, hub_snapshot as hs, tombstones
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.test_provenance_forget import env, pg, PROJECT, _capture, _payload  # noqa: F401
from tests.test_provenance_replica import hub_forget, replica, _replica, _replica_readers  # noqa: F401

PACKAGE = Path(hs.__file__).resolve().parent
SOURCE = (PACKAGE / "hub_snapshot.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)

# Every function in hub_snapshot.py that writes a replica memory table, and
# why it is safe. "fenced" means it is only ever reached inside a
# _replica_write_gate (checked below through the call graph).
MEMORY_WRITERS = {
    "_apply_fact_stream": "fenced: the fact stream (sync, pull)",
    "_write_decision_rows": "fenced: decision rows, each through _admit_fact",
    "_write_commitment_rows": "fenced: the pull's and the rebuild's commitments copy; the dump's builder",
    "_write_deliverable_rows": "fenced: an episode patch's deliverables",
    "_write_fact_deliverables": "fenced: deliverable rows, each through _admit_fact",
    "_upsert_episode_unfenced": "fenced: an episode patch (pull, capture, outbox replay)",
    "_write_pulled_tables": "fenced: the pull's commitments, their vectors, topics and fact stream",
    "upsert_embeddings": "fenced: vectors, by their episode's identities or their fact's wording",
    "_load_facts": "fenced: the rebuild's whole fact state",
    "_drop_fact_state": "fenced: the drop when the refusal set changes (only removes)",
    "_drop_mismatched_commitment_vectors": "fenced: removes vectors whose wording is not shown",
    "_insert_vector_row": "fenced: a vector row a fenced writer already checked",
    "_scrub_forgotten": "fenced: the fence's whole-replica check",
    "_insert_decisions": "dump builder: a fresh file the dump checks (_fence_dumped_facts) and publishes",
    "_insert_deliverables": "dump builder",
    "_insert_episodes": "dump builder",
    "_insert_memory_embeddings": "dump builder",
    "_apply_pending_erasures": "erasure: a forget, which only removes",
    "_erase_episode_commitments": "erasure",
    "forget_episode_in_snapshot": "erasure",
    "purge_topic_in_snapshot": "erasure: a topic purge",
}
DUMP_BUILDERS = {name for name, how in MEMORY_WRITERS.items() if how.startswith("dump builder")}

# Every public way rows reach the replica, and the function in it that
# opens the fenced write.
IMPORT_PATHS = {
    "sync_fact_changes": "_apply_fact_stream_locked",
    "rebuild_facts": "rebuild_facts",
    "apply_decision_changes": "apply_decision_changes",
    "sync_decision_changes": "apply_decision_changes",
    "upsert_episode": "_upsert_episode_unfenced",
    "upsert_embeddings": "upsert_embeddings",
    "pull_changes": "_write_pulled_tables",
    "refresh": "_refresh_locked",
    "publish_imported_snapshot": "publish_imported_snapshot",
    "scrub_forgotten_snapshot": "scrub_forgotten_snapshot",
}
# Every function that opens _replica_write_gate.
GATE_OPENERS = {"_apply_fact_stream_locked", "rebuild_facts", "apply_decision_changes",
                "_upsert_episode_unfenced", "upsert_embeddings", "_write_pulled_tables",
                "_refresh_locked", "publish_imported_snapshot", "scrub_forgotten_snapshot"}

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


def test_every_import_path_opens_the_one_gate():
    fns = _functions()
    assert {name for name, fn in fns.items() if _opens_gate(fn)} == GATE_OPENERS
    for entry, opener in IMPORT_PATHS.items():
        assert opener in GATE_OPENERS
        # The entry reaches its opener through hub_snapshot's own calls.
        seen, todo = set(), [entry]
        while todo:
            name = todo.pop()
            if name in seen or name not in fns:
                continue
            seen.add(name)
            todo.extend(_called(fns[name]))
        assert opener in seen, f"{entry} does not reach {opener}"


def test_every_fenced_writer_is_only_reached_inside_the_gate():
    """A fenced writer is called only by a gate opener or by another fenced
    writer (never from outside the mechanism), the dump builders only by the
    dump, and the fact-table row writers always with the fence."""
    fenced = {n for n, how in MEMORY_WRITERS.items() if how.startswith("fenced")}
    fns = _functions()
    # The fence's own methods (drop, scrub) and the dump's in-place check run
    # inside the gate that made the fence.
    inside = {"drop_facts", "scrub", "_fence_dumped_facts", "_write_commitment_vectors"}
    for name in fenced - GATE_OPENERS - {"_scrub_forgotten", "_drop_fact_state"}:
        for caller in _callers(name):
            assert caller in GATE_OPENERS | fenced | inside | {"upsert_episode", "_insert_commitments"}, \
                (name, caller)
    for name in DUMP_BUILDERS | {"_insert_commitments"}:
        assert _callers(name) == {"_refresh_locked"}, name
    assert _callers("_upsert_episode_unfenced") == {"upsert_episode"}
    assert _callers("_scrub_forgotten") == {"scrub"}
    # The backup export drops the facts of its own staged copy (a refused
    # generation's rows never reach an archive), never the replica's.
    assert _callers("_drop_fact_state") == {"drop_facts", "export_replica_copy"}
    assert _callers("_fence_dumped_facts") == {"_refresh_locked"}
    assert _callers("_write_commitment_vectors") <= fenced
    # Production calls of the row writers pass the fence.
    for writer, at in (("_write_decision_rows", 2), ("_write_commitment_rows", 3), ("_apply_fact_stream", 2)):
        for caller in _callers(writer):
            if caller == "_insert_commitments":
                continue
            for call in ast.walk(fns[caller]):
                if isinstance(call, ast.Call) and getattr(call.func, "id", None) == writer:
                    assert len(call.args) > at or any(k.arg == "fence" for k in call.keywords), (writer, caller)
    # Episodes and their vectors are checked by every identity they carry;
    # every fact import by the wait rule (owner ruling 2026-10-24).
    for name in ("_upsert_episode_unfenced", "upsert_embeddings"):
        assert "check" in _called(fns[name]), f"{name} does not check its episodes"
    for name in ("apply_decision_changes", "_upsert_episode_unfenced", "_write_pulled_tables",
                 "_apply_fact_stream", "_load_facts", "_fence_dumped_facts"):
        assert "facts_confirmed" in _called(fns[name]), f"{name} skips the wait rule"


def test_every_anchor_read_carries_every_identity():
    """Each episode import reads its episodes' identities from the hub in
    its own snapshot, merged captures and source receipts included. Every
    fact read carries the wait rule's hub read (``read_refusal_wait``),
    which since the owner decision of 2026-10-24 compares ids only: the
    registry's confirmed hub ids against the hub's tombstones, never an
    identity."""
    fns = _functions()
    assert {"_read_receipts", "read_anchor_identities", "read_refusal_wait"} <= _called(fns["_pull_apply"])
    for reader in ("_read_fact_rows", "_read_all_facts", "_dump_fact_inputs", "sync_decision_changes"):
        assert "read_refusal_wait" in _called(fns[reader]), reader
    for module in ("decisions.py", "embed.py"):
        assert "read_refusal_wait" in (PACKAGE / module).read_text(encoding="utf-8"), module
    assert "_episode_refused" not in fns
    assert "_read_receipts" in _called(fns["read_anchor_identities"])
    assert "_anchor_identities" in _called(fns["read_anchor_identities"])
    assert "_anchor_identities" in _called(fns["_scrub_forgotten"])
    assert "_anchor_identities" in _called(fns["_upsert_episode_unfenced"])
    body = ast.get_source_segment(SOURCE, fns["_anchor_identities"])
    assert "merged_from" in body and "receipts" in body
    wait = ast.get_source_segment(SOURCE, fns["read_refusal_wait"])
    assert "confirmed_ids" in wait and "unconfirmed" in wait and "deleted_at IS NOT NULL" in wait
    for identity_read in ("episode_sources", "merged_from", "khipu_natural_source_key", "_anchor_identities",
                          "refuses"):
        assert identity_read not in wait, identity_read


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
    opened: list[str] = []
    real = hs._replica_write_gate

    @contextmanager
    def spy(*args, **kwargs):
        opened.append("gate")
        with real(*args, **kwargs) as fence:
            yield fence

    monkeypatch.setattr(hs, "_replica_write_gate", spy)
    return opened


def _vector_row(eid):
    return {"profile": "fence", "kind": "episode", "ref": str(eid), "chunk_idx": 0, "chunk_text": MARKER,
            "content_hash": "x", "embedding": [1.0, 0.0], "built_at": "2026-10-23T12:00:00+00:00"}


IMPORTS = {
    "sync_fact_changes": lambda pg, eid: hs.sync_fact_changes(),
    "pull_changes": lambda pg, eid: hs.pull_changes(include_ledger=False),
    "refresh": lambda pg, eid: hs.refresh(),
}


def _rebuild_facts(pg, eid):
    return hs.rebuild_facts()


def _decision_mirror(pg, eid):
    with pg.connect() as conn, conn.cursor() as cur:
        for (fid,) in cur.execute("SELECT id FROM decisions").fetchall():
            decisions._mirror_to_snapshot(cur, fid)
        conn.rollback()
    return {"ok": True}


def _episode_patch(pg, eid):
    from khipu import deliverables

    with pg.connect() as conn, conn.cursor() as cur:
        row = cur.execute("SELECT to_jsonb(e) FROM episodes e WHERE id = %s", (eid,)).fetchone()[0]
        rows = deliverables.deliverables_for_episode(cur, eid)
        receipts = hs._read_receipts(cur, [eid]).get(eid, [])
        conn.rollback()
    return hs.upsert_episode(row, [_vector_row(eid)], deliverable_rows=rows, receipts=receipts)


def _vectors(pg, eid):
    with pg.connect() as conn, conn.cursor() as cur:
        anchors = hs.read_anchor_identities(cur, [eid])
        conn.rollback()
    return hs.upsert_embeddings([_vector_row(eid)], anchors=anchors)


IMPORTS.update({"rebuild_facts": _rebuild_facts, "apply_decision_changes": _decision_mirror,
                "upsert_episode": _episode_patch, "upsert_embeddings": _vectors})


@pytest.mark.parametrize("path", sorted(IMPORTS))
def test_every_import_path_fences_a_forgotten_merged_source(pg, replica, gate_spy, path):
    """A live public episode gains a merged capture on the hub whose source
    this Mac forgets while the merge is open, before the replica's last
    fenced write (so no registry change is left to trigger a scrub). Each
    import path alone must refuse its words."""
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
    IMPORTS[path](pg, eid)
    assert gate_spy, f"{path} did not go through _replica_write_gate"
    assert _leaked() == [], path


@pytest.mark.parametrize("path", ["publish_imported_snapshot", "scrub_forgotten_snapshot"])
def test_imported_and_scrubbed_replicas_fence_a_merged_source(pg, replica, gate_spy, path, tmp_path):
    """A replica copy made before the source was forgotten holds its words;
    the backup import and the scrub refuse them by the merged identity."""
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
    assert gate_spy
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

def test_every_fact_writer_checks_each_row():
    """Each writer of commitment, decision or deliverable rows passes every
    row through the one anchor check (``_admit_fact``), under the wait rule
    its caller applied. The per-row contributor admission was cut by the
    owner ruling of 2026-10-24: no contributor state is read or kept."""
    fns = _functions()
    for writer in ("_write_commitment_rows", "_write_decision_rows", "_write_fact_deliverables"):
        assert "_admit_fact" in _called(fns[writer]), writer
    for writer in ("_apply_fact_stream", "_write_deliverable_rows", "_load_facts"):
        assert "_write_fact_deliverables" in _called(fns[writer]), writer
    assert {"_dump_fact_inputs", "_fence_dumped_facts"} <= _called(fns["_refresh_locked"])
    fns_and_classes = {n.name for n in ast.walk(TREE) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    for retired in ("_fence_fact_wording", "_store_fact_sources", "_facts_sourced_by", "_ScrubGate",
                    "_index_identities", "apply_fact_rows", "read_fact_sources", "_fact_source_sql",
                    "fact_source_episodes", "_commitment_anchors", "_dumped_identities", "supports"):
        assert retired not in fns_and_classes, retired


SHARED_DECISION = "Ship the shared release checklist"
SHARED_TITLE = "Shared release checklist"
B_WORDS = "Review the deployment checklist before the release goes out"
A_WORDS = "Review the deployment checklist before release"


def _patch_with_confirm(pg, eid):
    from khipu import deliverables

    with pg.connect() as conn, conn.cursor() as cur:
        # As embed_on_capture reads them: the xmin before the rows.
        fact_xmin = hs._hub_snapshot_xmin(cur)
        row = cur.execute("SELECT to_jsonb(e) FROM episodes e WHERE id = %s", (eid,)).fetchone()[0]
        confirm = hs.read_refusal_wait(cur)
        rows = deliverables.deliverables_for_episode(cur, eid)
        receipts = hs._read_receipts(cur, [eid]).get(eid, [])
        conn.rollback()
    return hs.upsert_episode(row, [], deliverable_rows=rows, receipts=receipts, fact_xmin=fact_xmin,
                             confirm=confirm)


CONTRIBUTOR_IMPORTS = {
    "sync_fact_changes": (lambda pg, a: hs.sync_fact_changes(), {"decision", "deliverable"}),
    "pull_changes": (lambda pg, a: hs.pull_changes(include_ledger=False),
                     {"commitment", "decision", "deliverable"}),
    "refresh": (lambda pg, a: hs.refresh(), {"commitment", "decision", "deliverable"}),
    "rebuild_facts": (_rebuild_facts, {"commitment", "decision", "deliverable"}),
    "apply_decision_changes": (_decision_mirror, {"decision"}),
    "upsert_episode": (_patch_with_confirm, {"deliverable"}),
}


@pytest.mark.parametrize("path", sorted(CONTRIBUTOR_IMPORTS))
def test_every_import_path_keeps_and_fences_every_contributor(pg, replica, gate_spy, path):
    """Two separate live episodes share each fact: B restates A's
    commitment (its latest_text) and repeats A's decision and deliverable
    word for word. This Mac has forgotten B's source; the hub is unchanged.
    Under the wait rule (owner ruling 2026-10-24) each import path alone
    writes no fact row while the hub still holds B live; once the hub's
    forget re-projects the facts, the same path shows A's words and never
    B's."""
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
    out = run(pg, a)
    assert out.get("ok", True), out
    assert gate_spy, f"{path} did not go through _replica_write_gate"
    def tables():
        return json.dumps([_replica("SELECT * FROM commitments"), _replica("SELECT * FROM decisions"),
                           _replica("SELECT * FROM deliverables")])

    # Waiting: no fact row at all, B's words least of all.
    assert B_WORDS not in tables() and B_WORDS not in _replica_readers(), path
    assert SHARED_DECISION not in tables() and A_WORDS not in tables(), path
    # The hub catches up; the same path alone brings A's words back.
    hub_forget(pg, b)
    gate_spy.clear()
    out = run(pg, a)
    assert out.get("ok", True), out
    assert gate_spy, path
    assert B_WORDS not in tables() and B_WORDS not in _replica_readers(), path
    readers = _replica_readers()
    if "decision" in kinds:
        assert SHARED_DECISION in readers, path
    if "deliverable" in kinds:
        assert SHARED_TITLE in readers, path
    if "commitment" in kinds:
        assert A_WORDS in readers, path
