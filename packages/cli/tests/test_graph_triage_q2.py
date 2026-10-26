"""Graph section regressions from grokq-triage-2 (CONFIRMED rows only)."""
from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
GRAPH = ROOT / "third_party" / "graphify"
CLI_SRC = Path(__file__).resolve().parents[1] / "khipu" / "cli.py"


def _load(name: str):
    sys.path.insert(0, str(GRAPH))
    spec = importlib.util.spec_from_file_location("graph_q2_" + name, GRAPH / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_hops2_walk_does_not_return_the_seed():
    """Run the real CTE text from cli.py (translated to sqlite) on edge A-B."""
    src = CLI_SRC.read_text()
    m = re.search(r"(WITH RECURSIVE walk AS \(.*?LIMIT %\(lim\)s)", src, re.S)
    assert m, "walk CTE not found in cli.py"
    sql = m.group(1)
    # sqlite has no DISTINCT ON; keep one row per node_id at min hops.
    sql = re.sub(
        r"SELECT DISTINCT ON \(node_id\)\s+"
        r"node_id, via, type, hops\s+"
        r"FROM walk\s+"
        r"WHERE NOT \(node_id = ANY\(%\(ids\)s\)\)\s+"
        r"ORDER BY node_id, hops ASC",
        "SELECT node_id, via, type, hops FROM ("
        "SELECT node_id, via, type, hops, "
        "ROW_NUMBER() OVER (PARTITION BY node_id ORDER BY hops ASC) AS rn "
        "FROM walk WHERE NOT (node_id = ANY(%(ids)s))"
        ") WHERE rn = 1",
        sql,
    )
    sql = sql.replace("= ANY(%(ids)s)", "IN (SELECT value FROM json_each(:ids))")
    sql = re.sub(r"%\((\w+)\)s", r":\1", sql)
    db = sqlite3.connect(":memory:")
    db.execute("create table edges(src text, dst text, type text)")
    db.execute("insert into edges values('A','B','x')")
    rows = db.execute(
        sql, {"ids": json.dumps(["A"]), "id": "A", "max_hops": 2, "lim": 50}
    ).fetchall()
    assert "A" not in [r[0] for r in rows], rows


def test_model_call_log_collector_off_does_not_crash_build(tmp_path, monkeypatch):
    bg = _load("build_graph")
    membership = {
        "collectors": {
            k: False
            for k in [
                "tickers",
                "skills",
                "agents",
                "reports",
                "memory_topics",
                "predictive_gates",
                "frozen_tell",
                "hardcoded_data_sources",
                "hardcoded_notion_dbs",
                "biblical",
                "model_call_log",
                "code_ast",
                "code_semantic",
            ]
        },
        "code_roots": [str(tmp_path)],
    }
    monkeypatch.setattr(bg, "_membership", lambda: membership)
    monkeypatch.setattr(bg, "collect_similarity_edges", lambda *_a, **_k: [])
    monkeypatch.setattr(bg, "hardcoded_pattern_edges", lambda: [])
    monkeypatch.setattr(bg, "biblical_pattern_edges", lambda: [])
    monkeypatch.setattr(sys, "argv", ["build_graph", "--output", str(tmp_path / "g.sqlite")])
    assert bg.main() == 0


def test_graph_step_budgets_fit_within_driver_timeout():
    from khipu import jobs

    text = (GRAPH / "graphify_nightly.py").read_text()
    m = re.search(
        r'run_step\(\s*"semantic extract".*?timeout=(\d+)'
        r'.*?run_step\(\s*"build graph".*?timeout=(\d+)'
        r'.*?run_step\("embed corpus".*?timeout=(\d+)'
        r'.*?run_step\(\s*"embed edges".*?timeout=(\d+)'
        r".*?subprocess\.run\(.*?timeout=(\d+)",
        text,
        re.S,
    )
    assert m, "graphify_nightly step timeouts not found"
    total = sum(int(g) for g in m.groups())
    assert total <= jobs.DRIVER_TIMEOUT_SECONDS, (
        f"step budgets sum to {total}, driver timeout is {jobs.DRIVER_TIMEOUT_SECONDS}"
    )


def test_ast_ids_distinguish_pkg_mod_from_pkg_underscore_mod(tmp_path):
    ast_x = _load("code_ast_extractor")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("def one():\n    pass\n")
    (tmp_path / "pkg_mod.py").write_text("def two():\n    pass\n")
    nodes, _ = ast_x.collect_code_ast([tmp_path], "2026-01-01T00:00:00Z")
    modules = [n for n in nodes if n["id"].startswith("module:")]
    assert len(modules) == 2, [n["id"] for n in modules]


def _sem_targets(tmp_path, code, name):
    ast_x = _load("code_ast_extractor")
    sem = _load("code_semantic_extractor")
    (tmp_path / "m.py").write_text(code)
    nodes, _ = ast_x.collect_code_ast([tmp_path], "2026-01-01T00:00:00Z")
    layer = tmp_path / "semantic_layer.json"
    layer.write_text(
        json.dumps(
            {
                "files": [
                    {
                        "relpath": "m.py",
                        "functions": [{"name": name, "purpose": "does a thing"}],
                    }
                ]
            }
        )
    )
    sem.OUT_PATH = layer
    _, edges = sem.collect_code_semantic([tmp_path], "2026-01-01T00:00:00Z")
    return {n["id"] for n in nodes}, {e["dst"] for e in edges if e["type"] == "rationale_for"}


def test_semantic_edge_for_a_top_level_function_resolves(tmp_path):
    ids, targets = _sem_targets(tmp_path, "def helper():\n    pass\n", "helper")
    assert targets & ids, (targets, sorted(ids))


def test_semantic_edge_for_a_class_name_resolves(tmp_path):
    """Classes are asked for in the prompt; edges must use class: ids."""
    ids, targets = _sem_targets(tmp_path, "class Widget:\n    pass\n", "Widget")
    assert targets & ids, (targets, sorted(ids))
