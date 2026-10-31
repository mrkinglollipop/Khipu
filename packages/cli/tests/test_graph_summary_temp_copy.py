"""The graph summary's staged copy of a /Volumes graph is removed after the
read, and a stale one is swept from the real temp dir (not /tmp): one 3.7 GB
copy per nightly used to pile up in $TMPDIR."""
from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
from pathlib import Path

GRAPH = Path(__file__).resolve().parents[3] / "third_party" / "graphify"


def _builder():
    spec = importlib.util.spec_from_file_location("summary_build_graph", GRAPH / "build_graph.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_staged_copy_is_removed_and_stale_ones_swept(tmp_path, monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(GRAPH))
    b = _builder()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    stale = tmp_path / "graph_summary_old.sqlite"
    stale.write_bytes(b"x")
    db = tmp_path / "graph.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE nodes (type TEXT, frozen INT)")
    con.execute("CREATE TABLE edges (type TEXT)")
    con.execute("INSERT INTO nodes VALUES ('concept', 0)")
    con.commit()
    con.close()

    class OnVolumes(type(db)):
        def __str__(self):
            return "/Volumes/fake/graph.sqlite"

    import shutil
    monkeypatch.setattr(shutil, "copyfile", lambda src, dst: Path(dst).write_bytes(db.read_bytes()))
    b.print_summary(OnVolumes(db))
    assert "nodes:         1" in capsys.readouterr().out
    assert sorted(p.name for p in tmp_path.iterdir()) == ["graph.sqlite"]
