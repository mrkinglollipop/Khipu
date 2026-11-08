"""Graph walk regression from grokq-triage-2 (CONFIRMED rows only)."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

CLI_SRC = Path(__file__).resolve().parents[1] / "khipu" / "cli.py"


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
