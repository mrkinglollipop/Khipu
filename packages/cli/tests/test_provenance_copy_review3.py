# ruff: noqa: F811 - pytest fixtures imported from test_provenance_forget and replica_copy
"""The third 7B fix round (astra-7b3 findings 1-4): each test is the
review's probe, ported, and fails on fb79997b. Synthetic stores only."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import zipfile

from khipu import hub_snapshot as hs, paths
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.replica_copy import point, replica, shown, stamp  # noqa: F401
from tests.test_provenance_copy import _decision, _new_hub
from tests.test_provenance_forget import _capture, env, pg  # noqa: F401


def test_export_fallback_outside_configured_data(pg, replica, tmp_path, monkeypatch):
    replica(pg)
    # HOME and the system temporary root inside the data folder (no TMPDIR).
    data = paths.ensure_data_dir()
    monkeypatch.setenv("HOME", str(data / "fake-home"))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(data))
    _decision(pg, "ordinary prefix longer than twenty characters CACHE_FALLBACK_FACT_ONLY_" + "wording-" * 200)
    with pg.connect() as conn:
        conn.execute("UPDATE episodes SET summary = 'ordinary anchor'")
    assert hs.refresh()["facts"].get("copied")
    ready, release, errors, original = threading.Event(), threading.Event(), [], hs._copy_kept_rows

    def pause(con):
        original(con)  # every kept row is in the export's file now
        if threading.current_thread().name == "paused-export":
            ready.set()
            assert release.wait(10)

    def first_export():
        try:
            hs.export_replica_copy(hs.snapshot_path(), tmp_path / "first.sqlite")
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(repr(exc))

    monkeypatch.setattr(hs, "_copy_kept_rows", pause)
    thread = threading.Thread(target=first_export, name="paused-export")
    thread.start()
    try:
        assert ready.wait(10)
        out = paths.backup_local(dest=tmp_path / "fallback.zip")
        with zipfile.ZipFile(out["archive"]) as z:
            leaks = [n for n in z.namelist() if b"CACHE_FALLBACK_FACT_ONLY_" in z.read(n)]
        staged = [p for p in tmp_path.iterdir() if hs.is_replica_temp(p.name)]
        assert staged and not [p for p in staged if b"CACHE_FALLBACK_FACT_ONLY_" in p.read_bytes()]
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and not errors, errors
    assert not leaks, "fallback scratch under configured data entered backup"
    assert b"CACHE_FALLBACK_FACT_ONLY_" not in (tmp_path / "first.sqlite").read_bytes()


def test_pull_new_incarnation_with_locked_episode_read(pg, replica, pg_cluster, monkeypatch):
    replica(pg)
    _decision(pg, "OLD_INCARNATION_PULL")
    assert hs.refresh()["facts"].get("copied")
    old, other, batches, exchange = stamp(), _new_hub(pg_cluster), [], hs._one_exchange
    try:
        point(monkeypatch, other, dsn=pg.dsn)

        def bounded_connect(**kw):
            conn = other.connect()
            conn.execute("SET statement_timeout = '600ms'")
            conn.commit()
            return conn

        def observed_exchange(*args, **kw):
            batches.append(kw.get("prelude"))
            return exchange(*args, **kw)

        monkeypatch.setattr(hs, "try_hub_connect", bounded_connect)
        monkeypatch.setattr(hs, "_one_exchange", observed_exchange)
        with other.connect() as conn:
            assert hs._hub_incarnation(conn.cursor()) != old["incarnation"]
        with other.connect() as locker:
            locker.execute("LOCK TABLE episodes IN ACCESS EXCLUSIVE MODE")
            out = hs.pull_changes(include_ledger=False)
            locker.rollback()
        assert not out["ok"] and batches == [hs._INCARNATION_READ], (out, batches)
        assert stamp() is None and shown() == 0, "new incarnation was read but pull failure left old facts visible"
    finally:
        pg_cluster.drop(other)


def test_backup_preserves_unrelated_hidden_file(pg, replica, tmp_path):
    replica(pg)
    unrelated = paths.ensure_data_dir() / ".owner-notes.facts-999999"
    unrelated.write_bytes(b"owner notes unrelated to a replica")
    out = paths.backup_local(dest=tmp_path / "unrelated.zip")
    with zipfile.ZipFile(out["archive"]) as z:
        assert unrelated.exists() and unrelated.name in z.namelist(), "backup deleted unrelated owner file"


_PROMPT = '''import json, os, sys
from pathlib import Path
from khipu import recall_prompt as rp
try:
    line = rp._run_with_timeout(rp._replica_deliverable_line, rp.DELIVERABLE_LINE_TIMEOUT_S,
                                'acme/widget', ['prompt', 'budget'])
except rp._TimedOut:
    line = 'TIMED_OUT'
print(json.dumps({'line': line, 'security': Path(os.environ['FAKE_SECURITY_STARTED']).exists(),
                  'db_loaded': 'khipu.db' in sys.modules}))
'''


def test_prompt_keychain_subprocess_outlives_read_budget(pg, replica, tmp_path):
    replica(pg)
    _capture(loop=None, title="Prompt budget report", path="docs/prompt-budget.md")
    assert hs.refresh()["facts"].get("copied")
    (tmp_path / "dsn").write_text(pg.dsn)
    (tmp_path / "bin").mkdir()
    security = tmp_path / "bin" / "security"
    security.write_text("#!" + sys.executable + "\nimport os, time\n"
                        "open(os.environ['FAKE_SECURITY_STARTED'], 'w').close()\ntime.sleep(1.1)\n")
    security.chmod(0o700)
    env2 = {**os.environ, "KHIPU_KEYCHAIN": "1", "KHIPU_DSN_FILE": str(tmp_path / "dsn"),
            "FAKE_SECURITY_STARTED": str(tmp_path / "started"),
            "PATH": str(tmp_path / "bin") + os.pathsep + os.environ["PATH"]}
    for name in ("KHIPU_DATABASE_URL", "ALZY_DATABASE_URL"):
        env2.pop(name, None)

    def prompt() -> dict:
        done = subprocess.run([sys.executable, "-c", _PROMPT], env=env2, capture_output=True, text=True, timeout=8)
        assert done.returncode == 0, done.stderr
        out = json.loads(done.stdout)
        assert out["line"] != "TIMED_OUT" and not out["security"] and not out["db_loaded"], out
        return out

    # No recorded endpoint (configured before any save or copy): facts hidden.
    (hs.snapshot_path().parent / hs._DESTINATION_FILE).unlink()
    assert "docs/prompt-budget.md" not in (prompt()["line"] or "")
    # A copy records the endpoint it resolved; the prompt then shows its facts.
    assert hs.copy_facts()["ok"]
    assert "docs/prompt-budget.md" in prompt()["line"]
