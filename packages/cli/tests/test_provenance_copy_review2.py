# ruff: noqa: F811 - pytest fixtures imported from test_provenance_forget and replica_copy
"""The second 7B fix round (astra-7b2 report, findings 1-5): each test is
the review's probe, ported, and failed on efa29302. Synthetic stores only."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
import zipfile

from khipu import forget, hub_snapshot as hs, paths, tombstones
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.replica_copy import point, replica, shown, stamp  # noqa: F401
from tests.test_provenance_copy import _decision, _new_hub
from tests.test_provenance_forget import env, pg  # noqa: F401

_READ_FACTS = '''import sys, sqlite3, json
from khipu import hub_snapshot as hs
assert 'khipu.db' not in sys.modules
with sqlite3.connect(hs.snapshot_path()) as c:
    rows = c.execute('SELECT text FROM decisions WHERE 1=1' + hs._fact_epoch_sql(c)).fetchall()
print(json.dumps({'db_loaded': 'khipu.db' in sys.modules, 'rows': rows}))
'''


def _reader(env: dict, code: str = _READ_FACTS) -> str:
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert '"db_loaded": false' in done.stdout
    return done.stdout


# ---- 1 (R2): a crashed export's scratch never enters a later backup ----------------

def test_crashed_export_scratch_never_enters_next_backup(pg, replica, tmp_path):
    replica(pg)
    marker = "ordinary prefix longer than twenty characters CRASH_EXPORT_FACT_ONLY_" + "private-wording-" * 200
    _decision(pg, marker)
    with pg.connect() as conn:
        conn.execute("UPDATE episodes SET summary = 'ordinary anchor'")
    assert hs.refresh()["facts"].get("copied")
    # The export dies with its copy committed and synced, just before the rename.
    code = ("import os, sys\nfrom pathlib import Path\nfrom khipu import hub_snapshot as hs\n"
            "hs._fsync_path = lambda path: os._exit(17)\n"
            "hs.export_replica_copy(Path(sys.argv[1]), Path(sys.argv[2]))\n")
    staged = hs.staging_path()
    done = subprocess.run([sys.executable, "-c", code, str(hs.snapshot_path()), str(staged)],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 17, done.stderr
    left = [p for p in paths.data_dir().iterdir() if hs.is_replica_temp(p.name) and p != staged]
    assert any(b"ordinary anchor" in p.read_bytes() for p in left), "the crash left its whole copy"
    assert not [p.name for p in left if b"CRASH_EXPORT_FACT_ONLY_" in p.read_bytes()], \
        "the crashed export's file holds fact bytes"
    # A scratch copy an earlier export left in the data folder (its exact
    # name: ``.<staged name>.facts-<pid>``), its process gone.
    legacy = paths.data_dir() / "..hub_snapshot.ab12_x9z.sqlite.facts-999999"
    legacy.write_bytes(b"CRASH_EXPORT_FACT_ONLY_ legacy scratch")
    out = paths.backup_local(dest=tmp_path / "next.zip")
    assert out["ok"], out
    with zipfile.ZipFile(out["archive"]) as z:
        leaks = [n for n in z.namelist() if b"CRASH_EXPORT_FACT_ONLY_" in z.read(n)]
    assert not leaks, "next backup archived the crashed export scratch with fact bytes"
    assert not legacy.exists()


# ---- 2 (R3): the driver-free reader resolves the endpoint; a busy save still hides --

def test_prompt_reader_checks_dsn_file(pg, replica, tmp_path):
    replica(pg)
    _decision(pg, "OLD_HUB_PROMPT_FACT")
    assert hs.refresh()["facts"].get("copied")
    endpoint = tmp_path / "other-dsn"
    endpoint.write_text(pg.dsn + "_replacement")
    assert "OLD_HUB_PROMPT_FACT" not in _reader({**os.environ, "KHIPU_DSN_FILE": str(endpoint)})
    endpoint.write_text(pg.dsn)
    assert "OLD_HUB_PROMPT_FACT" in _reader({**os.environ, "KHIPU_DSN_FILE": str(endpoint)})


def test_actual_prompt_helper_rejects_other_file_endpoint(pg, replica, tmp_path):
    from tests.test_provenance_forget import _capture

    replica(pg)
    _capture(loop=None, title="Endpoint secret report", path="docs/endpoint-secret-report.md")
    assert hs.refresh()["facts"].get("copied")
    endpoint = tmp_path / "prompt-dsn"
    endpoint.write_text(pg.dsn + "_replacement")
    code = ("import sys, json\nfrom khipu import recall_prompt\n"
            "line = recall_prompt._replica_deliverable_line('acme/widget', ['endpoint', 'secret', 'report'])\n"
            "print(json.dumps({'db_loaded': 'khipu.db' in sys.modules, 'line': line}))\n")
    out = _reader({**os.environ, "KHIPU_DSN_FILE": str(endpoint)}, code)
    assert "docs/endpoint-secret-report.md" not in out


def test_official_endpoint_save_with_busy_replica_hides_prompt_facts(pg, replica, monkeypatch):
    from khipu import join

    replica(pg)
    _decision(pg, "OLD_HUB_AFTER_OFFICIAL_SAVE")
    assert hs.refresh()["facts"].get("copied")
    # The effective endpoint stays the old hub's (an environment override),
    # so only the save's own invalidation can hide the copy.
    monkeypatch.setenv("KHIPU_DATABASE_URL", pg.dsn)
    with sqlite3.connect(hs.snapshot_path()) as locker:
        locker.execute("BEGIN IMMEDIATE")
        join._write_dsn_file(pg.dsn + "_replacement")
        locker.rollback()
    assert stamp() is not None, "the busy replica kept its stamp"
    assert "OLD_HUB_AFTER_OFFICIAL_SAVE" not in _reader(dict(os.environ))
    assert hs.facts_hidden()
    # A copy that began after the save certifies itself and lifts the marker.
    assert hs.copy_facts()["ok"]
    assert "OLD_HUB_AFTER_OFFICIAL_SAVE" in _reader(dict(os.environ))


# ---- 3 (R7/D2): the incarnation is handed over before readiness can block ----------

def test_new_incarnation_blocked_readiness_clears_old_stamp(pg, replica, pg_cluster, monkeypatch):
    replica(pg)
    _decision(pg, "OLD_INCARNATION_FACT")
    assert hs.refresh()["facts"].get("copied")
    old = stamp()
    other = _new_hub(pg_cluster)
    try:
        with other.connect() as conn:
            assert hs._hub_incarnation(conn.cursor()) != old["incarnation"]
        point(monkeypatch, other, dsn=pg.dsn)
        with other.connect() as locker:
            locker.execute("LOCK TABLE fact_copy_ready IN ACCESS EXCLUSIVE MODE")
            started = time.monotonic()
            out = hs.copy_facts(deadline=started + 3)
            elapsed = time.monotonic() - started
            locker.rollback()
        assert not out["ok"] and elapsed < 3.7, (out, elapsed)
        assert stamp() is None and shown() == 0, "folded readiness read prevented incarnation handoff"
    finally:
        pg_cluster.drop(other)


# ---- 4 (R7/D3): the shared session keeps its cleanup settings ----------------------

def _idle_timeout(conn) -> str:
    return conn.execute("SELECT current_setting('idle_in_transaction_session_timeout')").fetchone()[0]


def test_shared_pull_settings_survive_handoff(pg):
    with pg.connect() as conn:
        hs._one_exchange(conn.cursor(), [("probe", "SELECT 1", ())])
        conn.rollback()  # the copy's first exchange begins with one
        assert _idle_timeout(conn) == "10s", "handoff rollback loses D3 idle timeout but bounded marker remains"


def test_shared_pull_actual_copy_retains_session_settings(pg, replica, monkeypatch):
    replica(pg)
    _decision(pg, "SHARED_SESSION_OLD")
    assert hs.refresh()["facts"].get("copied")
    _decision(pg, "SHARED_SESSION_CHANGED")
    settings = []
    original = hs._read_fact_rows

    def checked(cur):
        cur.execute("SELECT current_setting('idle_in_transaction_session_timeout')")
        settings.append(cur.fetchone()[0])
        return original(cur)

    monkeypatch.setattr(hs, "_read_fact_rows", checked)
    out = hs.pull_changes(include_ledger=False)
    assert out["facts"].get("copied"), out
    assert settings == ["10s"]


def test_shared_session_abandoned_transaction_frees_its_locks(pg):
    conn = pg.connect()
    hs._one_exchange(conn.cursor(), [("probe", "SELECT 1", ())])
    conn.execute("ROLLBACK; BEGIN; SELECT pg_advisory_xact_lock(4242)")  # then the client goes silent
    start = time.monotonic()
    with pg.connect() as probe:
        while not probe.execute("SELECT pg_try_advisory_lock(4242)").fetchone()[0]:
            assert time.monotonic() - start <= 10 + 2, "the server kept the abandoned shared session's lock"
            time.sleep(0.1)
    conn.close()


# ---- 5 (R5): pending forgets come from the registry with no replica ----------------

def test_pending_forgets_without_replica(pg, replica):
    replica(pg)
    assert not hs.snapshot_path().exists()
    tombstones.publish_identities({"cd" * 32}, set())
    assert forget.unsent_forgets()["strings"] == ["k:" + "cd" * 32]


def test_pending_forgets_with_an_unreadable_replica(pg, replica):
    replica(pg)
    hs.snapshot_path().parent.mkdir(parents=True, exist_ok=True)
    hs.snapshot_path().write_bytes(b"not a database")
    tombstones.publish_identities({"ce" * 32}, set())
    assert hs.unsent_forgets()["strings"] == ["k:" + "ce" * 32]
