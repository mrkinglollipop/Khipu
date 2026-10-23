"""Repo renames, slice C: the GitHub sweep, its CLI, the nightly step and the
doctor block. ``gh`` is always mocked."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import pytest

from khipu import cli, identity, jobs, project_group, project_renames as pr
from tests.hermetic_pg import MIGRATIONS, hub, pg_cluster  # noqa: F401

OLD, NEW = "o/church", "o/olive"


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)


@pytest.fixture
def conn(hub):  # noqa: F811
    c = hub.connect()
    c.execute((MIGRATIONS / "0039_project_rename_evidence.sql").read_text(encoding="utf-8"))
    c.commit()
    yield c
    c.close()


def _episode(conn, project, days_ago=1):
    conn.execute("INSERT INTO episodes (ts, summary, project) VALUES (now() - make_interval(days => %s), 's', %s)",
                 (days_ago, project))
    conn.commit()


def _rows(conn, sql):
    return conn.execute(sql).fetchall()


def _answers(table):
    return lambda slug: table.get(slug, (None, "HTTP 404"))


def test_github_rename_records_alias_with_source_github(conn):
    _episode(conn, OLD)
    out = pr.sweep(conn, _answers({OLD: (NEW, None)}))
    assert out["result"] == "ok" and out["renamed"] == [{"old": OLD, "new": NEW}]
    [(alias, project, source, ev)] = _rows(conn, "SELECT alias, project, source, evidence FROM project_aliases")
    assert (alias, project, source) == (OLD, NEW, "github")
    assert ev["full_name"] == NEW and ev["checked_at"]


def test_same_name_any_case_changes_nothing(conn):
    _episode(conn, OLD)
    out = pr.sweep(conn, _answers({OLD: (OLD.upper(), None)}))
    assert out["result"] == "ok" and out["renamed"] == []
    assert _rows(conn, "SELECT 1 FROM project_aliases") == []


def test_old_episodes_aliases_and_non_github_names_are_not_asked(conn):
    _episode(conn, "o/stale", days_ago=40)
    _episode(conn, "just-a-folder")
    _episode(conn, "o/known")
    conn.execute("INSERT INTO project_aliases (alias, project) VALUES ('o/known', 'o/canon')")
    conn.commit()
    asked = []
    pr.sweep(conn, lambda slug: asked.append(slug) or (slug, None))
    assert asked == []


def test_at_most_fifty_lookups(conn):
    for i in range(60):
        _episode(conn, f"o/r{i}")
    asked = []
    pr.sweep(conn, lambda slug: asked.append(slug) or (slug, None))
    assert len(asked) == 50


def test_gh_missing_is_skipped_and_changes_nothing(conn):
    _episode(conn, OLD)
    conn.execute("INSERT INTO project_pairs (a, b, state) VALUES (%s, %s, 'asked')", (OLD, NEW))
    conn.commit()
    with mock.patch("khipu.synth_cli.binary", return_value=None):
        out = pr.sweep(conn)
    assert out["result"] == "skipped" and out["reason"] == "gh missing"
    assert _rows(conn, "SELECT 1 FROM project_aliases") == []
    assert _rows(conn, "SELECT state FROM project_pairs") == [("asked",)]


def test_asked_pair_confirmed_merges_and_resolves(conn):
    conn.execute("INSERT INTO project_pairs (a, b, state) VALUES (%s, %s, 'asked')", (OLD, NEW))
    conn.commit()
    out = pr.sweep(conn, _answers({OLD: (NEW, None)}))
    assert out["renamed"] == [{"old": OLD, "new": NEW}]
    assert _rows(conn, "SELECT alias, project, source FROM project_aliases") == [(OLD, NEW, "github")]
    assert _rows(conn, "SELECT state FROM project_pairs") == [("merged_github",)]


def test_asked_pair_unconfirmed_stays_asked(conn):
    conn.execute("INSERT INTO project_pairs (a, b, state) VALUES (%s, %s, 'asked')", (OLD, NEW))
    conn.commit()
    out = pr.sweep(conn, _answers({OLD: ("o/other", None), NEW: (NEW, None)}))
    assert out["renamed"] == []
    assert _rows(conn, "SELECT state FROM project_pairs") == [("asked",)]
    assert _rows(conn, "SELECT 1 FROM project_aliases") == []


def test_database_error_is_failed_not_raised():
    bad = mock.Mock()
    bad.cursor.side_effect = RuntimeError("no hub")
    out = pr.sweep(bad, _answers({}))
    assert out["result"] == "failed" and "no hub" in out["error"]


def test_cli_json(conn):
    _episode(conn, OLD)
    buf = io.StringIO()
    with mock.patch("khipu.db.connect", return_value=conn), \
            mock.patch.object(pr, "github_full_name", _answers({OLD: (NEW, None)})), \
            redirect_stdout(buf):
        rc = cli.cmd_project_renames(SimpleNamespace(json=True))
    data = json.loads(buf.getvalue())
    assert rc == 0 and data["result"] == "ok" and data["renamed"] == [{"old": OLD, "new": NEW}]


def test_cli_skipped_exits_zero(conn):
    _episode(conn, OLD)
    buf = io.StringIO()
    with mock.patch("khipu.db.connect", return_value=conn), \
            mock.patch("khipu.synth_cli.binary", return_value=None), redirect_stdout(buf):
        rc = cli.cmd_project_renames(SimpleNamespace(json=True))
    assert rc == 0 and json.loads(buf.getvalue())["result"] == "skipped"


def test_nightly_step_skipped_is_not_a_failure(conn):
    _episode(conn, OLD)
    with mock.patch("khipu.db.connect", return_value=conn), mock.patch("khipu.synth_cli.binary", return_value=None):
        step = jobs._project_renames_sweep()
    assert step["ok"] is True and step["why"] == "gh missing"
    assert jobs._step_result(step)["ok"] is True


def test_nightly_step_failed_sweep_is_recorded_not_raised():
    with mock.patch.object(pr, "sweep", side_effect=RuntimeError("boom")):
        assert jobs._project_renames_sweep()["ok"] is False


def test_nightly_runs_the_step():
    wrapper_names = jobs.run_nightly.__code__.co_names
    assert "_job_lock" in wrapper_names
    assert "_run_nightly_locked" in wrapper_names
    assert "_project_renames_sweep" in jobs._run_nightly_locked.__code__.co_names


# ---- doctor block ----------------------------------------------------------------


def test_doctor_block_empty_never_swept(conn):
    block = pr.doctor_block(conn.cursor())
    assert block == {"aliases_total": 0, "aliases_by_source": {}, "newest_alias": None, "pairs_waiting": 0,
                     "last_sweep": {"result": None, "at": None, "reason": None}, "severity": "ok"}


def test_doctor_block_counts_newest_and_pairs(conn):
    identity.record_rename(conn.cursor(), OLD, NEW, source="github", evidence={"full_name": NEW})
    conn.execute("INSERT INTO project_aliases (alias, project, created_at) VALUES ('o/x', 'o/y', now() + interval '1 hour')")
    conn.execute("INSERT INTO project_pairs (a, b, state) VALUES ('o/a', 'o/b', 'asked')")
    conn.commit()
    pr._write_last({"result": "ok", "checked": 3})
    block = pr.doctor_block(conn.cursor())
    assert block["aliases_total"] == 2 and block["aliases_by_source"] == {"github": 1, "owner": 1}
    assert block["newest_alias"]["alias"] == "o/x" and block["newest_alias"]["source"] == "owner"
    assert block["pairs_waiting"] == 1
    assert block["last_sweep"]["result"] == "ok" and block["severity"] == "ok"


def test_doctor_block_skipped_recently_is_ok_then_yellow_after_three_days(conn):
    pr._write_last({"result": "skipped", "reason": "gh missing"})
    block = pr.doctor_block(conn.cursor())
    assert block["last_sweep"]["reason"] == "gh missing" and block["severity"] == "ok"
    path = pr._last_path()
    data = json.loads(path.read_text())
    data["non_ok_since"] = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    path.write_text(json.dumps(data))
    assert pr.doctor_block(conn.cursor())["severity"] == "yellow"
    pr._write_last({"result": "failed", "error": "x"})  # keeps the original non_ok_since
    assert pr.doctor_block(conn.cursor())["severity"] == "yellow"
    pr._write_last({"result": "ok"})
    assert pr.doctor_block(conn.cursor())["severity"] == "ok"


def test_a_kept_separate_pair_is_never_linked_by_the_sweep(conn):
    _episode(conn, OLD)
    conn.execute("INSERT INTO project_pairs (a, b, state, decided_at) VALUES (%s, %s, 'kept_separate', now())",
                 tuple(sorted((OLD, NEW))))
    conn.commit()
    out = pr.sweep(conn, _answers({OLD: (NEW, None)}))
    assert out["result"] == "ok" and out["renamed"] == []
    assert out["skipped_by_owner"] == [{"old": OLD, "new": NEW}]
    assert _rows(conn, "SELECT alias FROM project_aliases") == []
    assert _rows(conn, "SELECT state FROM project_pairs") == [("kept_separate",)]


def test_a_name_kept_separate_from_a_project_stays_separate_after_that_project_is_renamed(conn):
    """Separate B from C, rename C to D, then GitHub says B is now D: B and
    C would share D's group, so the sweep links nothing."""
    b, c, d = "o/bee", "o/cee", "o/dee"
    conn.execute("INSERT INTO project_aliases (alias, project, source) VALUES (%s, %s, 'github')", (b, c))
    conn.commit()
    with conn.cursor() as cur:
        assert project_group.apply(cur, project_group.ACTION_SEPARATE, [b])["ok"]
    conn.commit()
    with conn.cursor() as cur:
        assert identity.record_rename(cur, c, d, source="github")["ok"]
    conn.commit()
    _episode(conn, b)
    out = pr.sweep(conn, _answers({b: (d, None)}))
    assert out["result"] == "ok" and out["renamed"] == [], out
    assert out["skipped_by_owner"] == [{"old": b, "new": d}]
    assert dict(_rows(conn, "SELECT alias, project FROM project_aliases")) == {c: d}
    assert _rows(conn, "SELECT a, b, state FROM project_pairs") == [(b, c, "kept_separate")]
