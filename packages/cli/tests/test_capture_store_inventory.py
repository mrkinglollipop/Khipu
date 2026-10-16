"""Every function that writes a file is in the capture store inventory.

Forget scans the stores khipu/capture_stores.py declares. A new place that
holds capture data stays out of that scan until someone lists it, so this
test lists every function in the package that writes a file (the same AST
walk as test_episode_write_gate_inventory.py, over file-writing calls). Each
must be a store's writer there, or appear in NOT_CAPTURE below with what it
writes. A new writer fails until it is placed, and the store list is mirrored
in docs/plans/2026-10-11-capture-identity.md ("Durable forget: one store
inventory").
"""
from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path

import psycopg
import pytest

from khipu import capture_stores, db, forget, outbox
from tests import test_forget_ownership as fo
from tests.test_forget_write_gate import P

PACKAGE = Path(__file__).resolve().parents[1] / "khipu"

_LOCKS = "a lock file; holds nothing"
_SETTINGS = "settings, credentials or install files; no capture text"
_COUNTERS = "counters, timings, ids or health state; no capture text"
_LEDGER = "the work ledger's own rows and outcomes, not captures"
_ROUTING = "the account routing record and its views, not captures"
_NOTES = "memory notes and their index, written by the owner and models, not capture sources"
_TOOLS = "a downloaded tool, a gate fixture or a scratch drill folder, not captures"
_REPLICA_META = "replica bookkeeping: meta, freshness, erasure and purge markers holding ids or hashes"
_REGISTRY = "the forgotten-source registry: identities and ids, never text"

NOT_CAPTURE = {
    **dict.fromkeys(("hub_snapshot._acquire_refresh_lock", "ledger._ledger_lock",
                     "memory_lock._take", "outbox._locked_outbox", "promote._lock",
                     "recall_prompt._session_lock", "routing._lock", "session_capture._beat_lock",
                     "session_capture._capture_now_lock", "session_capture._claim_lock",
                     "usage._open_lock", "discover._try_lock", "config.config_write_lock",
                     "accounts._declined_lock", "signin._lock"), _LOCKS),
    # Account discovery (9A): which login folders exist and when they were checked,
    # and a throwaway folder a status check runs in. Folder names and counts only.
    "discover._save_store": "account discovery results: folder names, CLI kinds and check times",
    "discover.run_once": "a temporary empty folder a CLI status check runs in, removed after",
    "accounts._save_declined": "the logins the owner chose not to be offered again: folder names and CLI kinds",
    # Signing in a new account (9D): the running attempt and the folders Khipu made.
    # A sign-in code and link are held in memory only, never written.
    "signin._write_json": "the sign-in attempt (id, folder, pid, start times, argv) and the login folders "
                          "Khipu made; no capture text",
    "signin.start": "a temporary empty folder the login process runs in, removed when the attempt ends",
    # The replica job (offline design A): run counters and the wake file; no capture text.
    "replica_heal._write_state": "the replica job's counters and health: times, error words, no capture text",
    "replica_heal.ensure_wake_file": "an empty file the replica job's plist watches",
    **dict.fromkeys(("config._write_config", "integrations._backup", "integrations._cursor_install",
                     "integrations._grok_bot_install", "integrations._write_json",
                     "integrations._write_toml", "integrations.set_gateway_token",
                     "join._import_kit", "join._restore_file", "join._write_dsn_file",
                     "join.write_kit_file", "join.write_private_file",
                     "join_pair._self_signed_cert_dir", "keychain.migrate_legacy_secrets",
                     "launchd_gen.install_job", "paths.set_data_dir", "setup._stage_store",
                     "sources.export_resolved", "sources.save_sources",
                     "promote._seed_config", "promote._copy_routing_record",
                     "promote._remember_live", "promote.flip", "promote.record",
                     "components_matrix.write_versions", "usage.sign_in"), _SETTINGS),
    **dict.fromkeys(("embed._budget_take", "embed._log_query_timing", "embed._save_plan_cache",
                     "embed.budget_reset_today", "embed_jobs.list_jobs", "embed_jobs.write",
                     "extract._record_usage", "gateway._record_liveness",
                     "graph_backup._remember_integrity_ok", "hook_background._slot",
                     "jobs._nightly_log", "jobs._offsite_if_due", "jobs._record_nightly_step",
                     "jobs._run_script", "jobs._write_job_state", "migrate._save_lock_notes",
                     "probe._persist_result", "probe._write_state", "promote._recall_once",
                     "promote._run",
                     "recall_prompt._dedup_transaction", "recall_prompt._detach_child",
                     "recall_prompt._log", "recall_prompt._prompt_routing_child",
                     "recall_prompt._query_embed_cache_connect",
                     "recall_prompt._write_session_state", "recall_rule._routing_child",
                     "session_capture._log_subagentstop_shape_once", "session_capture._write_beat",
                     "synth_cli.register", "t3._lookup_health",
                     "t3._thread_linked_capture_evidence", "t3.thread_for_session",
                     "topic_hits._write_offset", "usage._converse", "usage._save_cache",
                     "components_matrix.refresh_matrix_cache"), _COUNTERS),
    **dict.fromkeys(("query_log._rotate_if_needed", "query_log.log_query"),
                    "search queries as typed into search, or their hash; not capture sources"),
    **dict.fromkeys(("ledger._migrate_outcomes", "ledger._prune_outcomes", "ledger._record_outcome",
                     "ledger._record_rejected", "ledger._refuse_job", "ledger._replica_pending",
                     "ledger._set_aside", "ledger.enqueue"), _LEDGER),
    # The one way a ledger or work answer leaves Khipu; only ``khipu ledger ... --out``
    # names a file, and it gets the ledger board or ledger rows.
    "agent_view.emit": "a ledger board or ledger rows, to a file the owner names with --out; not captures",
    **dict.fromkeys(("routing._append_locked", "routing._fold_turn", "routing._id_salt",
                     "routing._save_view", "routing_replay._routing_quiet", "routing_replay._seed",
                     "routing_replay.replay"), _ROUTING),
    **dict.fromkeys(("memory_md.regen_memory_md", "notes._write_state", "organise._backup_index",
                     "organise._write_evidence", "organise.rewrite_index", "organise.split_note"),
                    _NOTES),
    **dict.fromkeys(("components_graphify._download", "components_graphify._ensure_empty_sources",
                     "components_graphify._extract_tarball", "components_graphify.install_graphify",
                     "components_graphify.upgrade_graphify", "graph_backup.scratch_drill",
                     "promote.gate_capture", "integrations._probe_aegis_isolation",
                     "integrations._probe_aegis_refusal", "integrations._probe_extract",
                     "integrations._probe_hook", "integrations._probe_native_extract"), _TOOLS),
    **dict.fromkeys(("hub_snapshot._mark_purged_topic", "hub_snapshot._queue_erasure",
                     "hub_snapshot._write_last_known", "hub_snapshot._write_meta"), _REPLICA_META),
    **dict.fromkeys(("tombstones._atomic_write", "tombstones._locked_registry",
                     "tombstones.publish_identities"), _REGISTRY),
}

_NAMED = {"write_text", "write_bytes", "mkstemp", "mkdtemp", "NamedTemporaryFile",
          "TemporaryDirectory", "_atomic_write", "write_sensitive_bytes", "copy_sensitive",
          "sensitive_writer", "write_private_file"}
_SHUTIL = {"copy", "copy2", "copyfile", "copytree", "move"}
_OPEN_FLAGS = {"O_CREAT", "O_WRONLY", "O_RDWR", "O_APPEND"}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith(("KHIPU_", "ALZY_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "native"))
    monkeypatch.setenv("KHIPU_OUTBOX", str(tmp_path / "outbox"))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_TEST", "1")
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: pytest.fail("live PG forbidden"))
    monkeypatch.setattr(outbox, "_queued_origin", lambda: {"origin": None})
    db._TABLE_COLUMNS_CACHE.clear()


def _name(call: ast.Call) -> tuple[str | None, str | None]:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id, None
    if isinstance(f, ast.Attribute):
        return f.attr, f.value.id if isinstance(f.value, ast.Name) else None
    return None, None


def _writes_a_file(call: ast.Call) -> bool:
    name, base = _name(call)
    if name in _NAMED:
        return True
    if name in _SHUTIL:
        return base == "shutil"
    if name in ("replace", "rename"):
        # os.replace/os.rename, or Path.replace/rename (one argument; str.replace takes two).
        return base == "os" or (len(call.args) == 1 and not call.keywords)
    if name == "connect":
        texts = [n.value for n in ast.walk(call) if isinstance(n, ast.Constant)
                 and isinstance(n.value, str)]
        return base == "sqlite3" and not any("mode=ro" in t for t in texts)
    if name == "open" and base == "os":
        return any(isinstance(n, ast.Attribute) and n.attr in _OPEN_FLAGS for n in ast.walk(call))
    if name == "open":
        modes = [a for a in call.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        modes += [k.value for k in call.keywords
                  if k.arg == "mode" and isinstance(k.value, ast.Constant)]
        return any(any(ch in str(m.value) for ch in "wax+") for m in modes)
    return False


def _file_writers() -> set[str]:
    found = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        module = path.relative_to(PACKAGE).with_suffix("").as_posix().replace("/", ".")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions = [n for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _writes_a_file(node):
                best = None
                for fn in functions:
                    if fn.lineno <= node.lineno <= (fn.end_lineno or fn.lineno) and (
                            best is None or fn.lineno >= best.lineno):
                        best = fn
                found.add(f"{module}.{best.name if best else '<module>'}")
    return found


def _store_writers() -> set[str]:
    return set().union(*(s.writers for s in capture_stores.STORES))


def test_every_file_writer_is_a_store_writer_or_writes_no_capture_data():
    unplaced = sorted(_file_writers() - _store_writers() - set(NOT_CAPTURE))
    assert not unplaced, (
        f"{unplaced} write files but are in no capture store. If one can hold capture data "
        "(transcript, payload, summary or episode text), add it to a store in "
        "khipu/capture_stores.py and make forget scan that store; otherwise add it to "
        "NOT_CAPTURE here with what it writes.")


def test_inventory_lists_no_writer_that_no_longer_writes():
    writers = _file_writers()
    stale = sorted((_store_writers() | set(NOT_CAPTURE)) - writers)
    assert not stale, f"{stale} no longer write a file; remove them from the inventory"
    both = sorted(_store_writers() & set(NOT_CAPTURE))
    assert not both, f"{both} are both a store writer and NOT_CAPTURE"


def test_every_store_is_scanned_by_forget_or_says_why_not():
    for store in capture_stores.STORES:
        assert store.scanned or store.forget.startswith(("kept: ", "none: ")), store.name
    scanned = {s.forget for s in capture_stores.STORES if s.scanned}
    assert scanned == set(capture_stores.FORGET_SCANS)


def test_forget_reads_its_folders_from_the_inventory():
    assert "native_job_folders" in inspect.getsource(forget._native_job_remainder)
    assert "outbox_folders" in inspect.getsource(forget._outbox_entries)
    assert "STORES" in inspect.getsource(forget._forget_everywhere)


def test_a_store_without_a_forget_scan_keeps_the_forget_open(isolated, tmp_path, monkeypatch):
    extra = capture_stores.Store("new cache", "somewhere", "transcript text", "new cache scan",
                                 frozenset())
    monkeypatch.setattr(capture_stores, "STORES", (*capture_stores.STORES, extra))
    root = tmp_path / "memory"
    root.mkdir()
    fo.write_lines(root, [P])
    world = {"episodes": {1: dict(P, id=1, ts=fo.BASE, raw=dict(P), deleted=False)},
             "sources": [fo.ForgetOwnershipTest._source(None, 1, P)], "sources_enabled": True}
    monkeypatch.setattr(db, "connect", lambda *a, **kw: fo._Conn(fo._WorldCur(world)))

    result = forget.forget_everywhere(1, memory_root=root)

    assert not result["complete"]
    assert "store_inventory" in result["partial"]
    assert any(loc["place"] == "new cache" and loc["state"] == "not checked"
               for loc in result["locations"])
