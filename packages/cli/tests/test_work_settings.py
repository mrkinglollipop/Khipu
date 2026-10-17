"""Work view phase 5 (docs/plans/2026-10-15-work-view.md, "Settings -> Work"):
the reads and writes Settings, Work needs from the CLI. Ledger projects are
listed and renamed or hidden by the owner; hidden Owed items are listed for
Unhide; a summary file's first read names what it found; job runner folders
holding a summary file are suggested in `khipu config`.

Synthetic throughout: the ledger runs in the scratch PostgreSQL test_ledger
starts, and every summary file and runner folder is under tmp_path.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout

import pytest

from khipu import work
from tests import test_ledger as _tl
from tests.test_ledger import P, ok, run
from tests.test_work import commitment, put, summary

pg_dsn, hub = _tl.pg_dsn, _tl.hub


def _work(*argv):
    from khipu import cli

    args = cli.build_parser().parse_args(["work", *argv])
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = args.func(args)
    return rc, out.getvalue(), err.getvalue()


def _project(name=P):
    rc, out, _ = run("projects", "--json", "--project", P)
    assert rc == 0, out
    return next(p for p in json.loads(out)["projects"] if p["project"] == name)


# ---- ledger projects ---------------------------------------------------------------


def test_projects_lists_every_project_with_its_counts(hub):
    ok("add", "g-1", "--title", "Ship sign-in", "--kind", "goal")
    ok("add", "n-1", "--title", "Pick the icon", "--kind", "needs-matt", "--state", "NEEDS OWNER")
    ok("add", "s-1", "--title", "Old slice", "--kind", "slice", "--state", "DONE")
    proj = _project()
    assert proj["rows"] == 3 and proj["open"] == 2 and proj["needs_owner"] == 1
    assert proj["hidden"] is False and proj["name"] == P and proj["created_at"]


def test_the_owner_renames_and_hides_a_project_and_its_rows_stay(hub):
    ok("add", "g-1", "--title", "Ship sign-in", "--kind", "goal")
    assert run("project", "--rename", "Widget", "--json", "--project", P, "--from-app")[0] == 0
    assert run("project", "--hide", "--json", "--project", P, "--from-app")[0] == 0
    proj = _project()
    assert proj["name"] == "Widget" and proj["hidden"] is True and proj["rows"] == 1
    assert run("project", "--show", "--json", "--project", P, "--from-app")[0] == 0
    assert run("project", "--rename", "", "--json", "--project", P, "--from-app")[0] == 0
    proj = _project()
    assert proj["name"] == P and proj["hidden"] is False


def test_renaming_or_hiding_is_the_owners_change(hub, khipu_app):
    ok("add", "g-1", "--title", "Ship sign-in", "--kind", "goal")
    khipu_app.as_shell("/usr/local/bin/claude")
    for extra in ([], ["--from-app"]):
        rc, out, _ = run("project", "--hide", "--json", "--project", P, *extra)
        assert rc == 1 and json.loads(out)["reason"] == "not_app"
    khipu_app.as_app()
    assert _project()["hidden"] is False


def test_a_project_change_names_one_change_and_an_unknown_project_is_refused(hub):
    ok("add", "g-1", "--title", "Ship sign-in", "--kind", "goal")
    rc, out, _ = run("project", "--json", "--project", P, "--from-app")
    assert rc == 1 and json.loads(out)["reason"] == "nothing_to_change"
    assert run("project", "--rename", "x" * 81, "--json", "--project", P, "--from-app")[0] == 1
    assert run("project", "--rename", "Two\nlines", "--json", "--project", P, "--from-app")[0] == 1
    rc, out, _ = run("project", "--hide", "--json", "--project", "acme/absent", "--from-app")
    assert rc == 1 and json.loads(out)["reason"] == "no_project"


def test_a_project_change_needs_the_database(hub, monkeypatch):
    ok("add", "g-1", "--title", "Ship sign-in", "--kind", "goal")
    _tl._away(monkeypatch)
    rc, out, _ = run("project", "--hide", "--json", "--project", P, "--from-app")
    assert rc == 1 and json.loads(out)["reason"] == "away"


# ---- hidden Owed items --------------------------------------------------------------


def test_hidden_lists_each_hidden_commitment_with_its_words(monkeypatch, tmp_path):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    item = commitment("c5", "Look at the nightly report size again", project="acme/harbor")
    changed = commitment("c6", "Words that changed since")
    monkeypatch.setattr(work, "read_owed", lambda: {"items": [item, changed], "error": None})
    work.set_hidden("c5", item)
    work.set_hidden("c6", {**changed, "text": "the old words"})
    work.set_hidden("c7", commitment("c7", "Closed since"))
    rc, out, _ = _work("hidden", "--json")
    assert rc == 0, out
    got = json.loads(out)["hidden"]
    assert [h["id"] for h in got] == ["c5"]
    assert got[0]["text"] == "Look at the nightly report size again" and got[0]["project"] == "acme/harbor"


# ---- a summary file's first read and the suggestions ------------------------------------


def test_check_json_names_the_project_and_when_it_was_written(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    rc, out, _ = _work("check", str(put(tmp_path, summary())), "--json")
    assert rc == 0, out
    got = json.loads(out)
    assert got["project"] == "atlas" and got["written_at"].endswith("Z")
    assert got["counts"] == {"jobs": 1, "prs": 1, "trains": 1}
    assert str(tmp_path) not in out


def _runner(tmp_path, name, with_file=True):
    folder = tmp_path / name
    (folder / "state").mkdir(parents=True)
    if with_file:
        (folder / "state" / "work-summary.json").write_text(json.dumps(summary()))
    return folder


def test_runner_folders_holding_a_summary_file_are_suggested(tmp_path, monkeypatch):
    from khipu.config import mutate_config

    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    pebble, atlas = _runner(tmp_path, "pebble-runner"), _runner(tmp_path, "atlas-runner")
    quarry, private = _runner(tmp_path, "quarry-runner", False), _runner(tmp_path, "API Keys/runner")
    with mutate_config() as data:
        data["routing_sources"] = [{"name": n, "kind": "lanes", "path": str(p)} for n, p in
                                   (("Pebble", pebble), ("Atlas", atlas), ("Quarry", quarry), ("Hidden", private))]
    work.set_setting("work_projects", [{"project": "atlas", "file": str(atlas / "state" / "work-summary.json")}])
    assert work.suggested_files() == [{"name": "Pebble", "file": str(pebble / "state" / "work-summary.json")}]


def test_khipu_config_carries_the_suggestions(tmp_path, monkeypatch):
    from khipu import cli

    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(work, "suggested_files", lambda config=None: [{"name": "Pebble", "file": "/x/state/w.json"}])
    args = cli.build_parser().parse_args(["config"])
    with redirect_stdout(io.StringIO()) as out:
        assert args.func(args) == 0
    assert json.loads(out.getvalue())["work_suggested"] == [{"name": "Pebble", "file": "/x/state/w.json"}]


@pytest.mark.parametrize("argv", [["ledger", "projects", "--json", "--project=owner/atlas"],
                                  ["ledger", "project", "--json", "--project=owner/atlas", "--from-app",
                                   "--rename=Atlas"],
                                  ["ledger", "project", "--json", "--project=owner/atlas", "--from-app", "--hide"],
                                  ["work", "hidden", "--json"]])
def test_the_apps_argv_parse(argv):
    """The desktop's fixed argv (src-tauri lib.rs, work_command_tests)."""
    from khipu import cli

    assert cli.build_parser().parse_args(argv).func
