"""One forget fence (``tombstones.withhold``) at every output a reader sees:
session start (live, stale and recent slices), the per-prompt push and its
produced line, MCP and CLI search, get and graph, and the query log. A row
carrying or derived from an episode on the forget list (or queued for
erasure) never leaves; an unreadable list pushes nothing and lets an explicit
search show only rows that touch no episode. Kimi's review probes, 2026-10-30."""
from __future__ import annotations

import argparse
import ast
import io
import json
import re
import textwrap
import tokenize
import sqlite3
from unittest import mock

import pytest

from khipu import activity, cli, db, deliverables, embed
from khipu import hub_snapshot as hs
from khipu import mcp_server, query_log
from khipu import recall_prompt as rp
from khipu import recall_rule as rr
from khipu import tombstones

FORGOT = 902


@pytest.fixture
def forgotten():
    tombstones.publish_episode_ids({FORGOT})
    return FORGOT


def _corrupt_list() -> None:
    path = tombstones.registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")


GRAPH_ROW = {"kind": "topic", "id": "secret-topic", "snippet": "FORGOTTEN-GRAPH",
             "via": {"seed": "episode:902", "relation": "capture_topic"}}

SEARCH_ROWS = [
    {"kind": "episode", "id": "902"},
    {"kind": "decision", "id": "5", "episode_id": 902},
    {"kind": "commitment", "id": "6", "episode_id": "902"},
    {"kind": "topic", "id": "t", "via": "episode:902"},
    {"kind": "topic", "id": "g", "via": {"seed": "episode:902"}},
    {"kind": "episode", "id": "904"},
    {"kind": "topic", "id": "plain"},
]


# ---- per-prompt push -------------------------------------------------------

def test_a_graph_candidate_seeded_by_a_forgotten_episode_is_not_pushed(forgotten):
    assert tombstones.withhold([GRAPH_ROW]) == []
    with mock.patch.object(rp, "_search_hits", return_value={"hits": [dict(GRAPH_ROW)], "legs": ["fake"]}), \
         mock.patch.object(rp, "_deliverable_context_line", return_value=""):
        pushed = rp.prior_work_for_prompt("alpha beta")
    assert pushed["hits"] == []
    assert "FORGOTTEN-GRAPH" not in pushed["context"]


class _FakeCursor:
    def execute(self, *args):
        return self

    def fetchall(self):
        return [(1, "p", "note", None, None, "FORGOTTEN-DELIVERABLE alpha beta", FORGOT, "2026-10-30")]


class _FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def cursor(self):
        cur = _FakeCursor()
        ctx = mock.MagicMock()
        ctx.__enter__.return_value = cur
        return ctx


def _produced_push():
    with mock.patch.object(rp, "_search_hits", return_value={"hits": [], "legs": ["fake"]}), \
         mock.patch.object(rp, "_replica_deliverable_line", return_value=None), \
         mock.patch.object(rp, "_project_for_cwd", return_value="p"), \
         mock.patch("khipu.hub_reach.known_unreachable", return_value=None), \
         mock.patch.object(db, "connect", return_value=_FakeConnection()), \
         mock.patch.object(deliverables, "_deliverables_ready", return_value=True), \
         mock.patch.object(deliverables, "_not_forgotten_clause", return_value="1=1"):
        return rp.prior_work_for_prompt("alpha beta", cwd="/repo")


def test_the_hub_produced_line_never_names_a_forgotten_episode(forgotten):
    assert "FORGOTTEN-DELIVERABLE" not in _produced_push()["context"]


def test_an_unreadable_forget_list_pushes_no_produced_line():
    _corrupt_list()
    produced = _produced_push()
    assert produced["context"] == ""


def test_the_produced_line_still_shows_a_live_episode():
    assert "FORGOTTEN-DELIVERABLE" in _produced_push()["context"]


# ---- session start ---------------------------------------------------------

SLICE = {
    "commitments": [{"id": 6, "text": "FORGOTTEN-COMMITMENT", "opened_episode": FORGOT}],
    "decisions": [{"id": 5, "text": "FORGOTTEN-DECISION", "episode_id": FORGOT}],
    "episodes": [{"id": FORGOT, "summary": "FORGOTTEN-EPISODE", "topics": ["secret-topic"]},
                 {"id": 904, "summary": "LIVE-EPISODE", "topics": ["shared-topic"]}],
    "topics": [{"slug": "secret-topic", "title": "FORGOTTEN-TOPIC"},
               {"slug": "shared-topic", "title": "LIVE-TOPIC"}],
    "pairs": [],
}
FORGOTTEN_WORDS = ("FORGOTTEN-EPISODE", "FORGOTTEN-DECISION", "FORGOTTEN-COMMITMENT", "FORGOTTEN-TOPIC")


def _session_start(slice_data=SLICE):
    with mock.patch("khipu.identity.resolve_repo_root", return_value={"repo_root": "/repo", "project": "p"}), \
         mock.patch.object(rr, "cwd_search_term", return_value=None), \
         mock.patch.object(activity, "recent_episodes", return_value=[{"id": 904, "summary": "LIVE-EPISODE"}]), \
         mock.patch.object(activity, "project_slice", return_value=slice_data), \
         mock.patch.object(rr, "_current_host_session_id", return_value=None), \
         mock.patch("khipu.routing_directions.pushed_slice_ids", return_value=[]):
        return rr._pushed_memory_slice("/repo")


def test_session_start_never_pushes_a_forgotten_episode_or_its_rows(forgotten):
    start = _session_start()
    assert not [w for w in FORGOTTEN_WORDS if w in start]
    assert "LIVE-EPISODE" in start and "LIVE-TOPIC" in start


def test_session_start_with_an_unreadable_forget_list_pushes_no_memory():
    _corrupt_list()
    start = _session_start()
    assert not [w for w in (*FORGOTTEN_WORDS, "LIVE-EPISODE", "LIVE-TOPIC") if w in start]


def _stale_con():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE episodes (id INTEGER, summary TEXT, raw TEXT, deleted_at TEXT, "
                "ingested_at TEXT, ts TEXT)")
    for eid, word in ((FORGOT, "FORGOTTEN-STALE"), (904, "LIVE-STALE")):
        con.execute("INSERT INTO episodes VALUES (?, ?, '{\"project\":\"p\"}', NULL, '2026-01-01', '2026-01-01')",
                    (eid, word))
    return con


def test_the_stale_session_start_slice_never_pushes_a_forgotten_episode(forgotten):
    with mock.patch.object(hs, "open_snapshot", return_value=_stale_con()), \
         mock.patch.object(hs, "snapshot_project_group", return_value=(None, ["p"])):
        stale = rr._stale_project_slice("p", None)
    assert "FORGOTTEN-STALE" not in stale and "LIVE-STALE" in stale


def test_the_stale_session_start_slice_pushes_nothing_on_an_unreadable_list():
    _corrupt_list()
    with mock.patch.object(hs, "open_snapshot", return_value=_stale_con()), \
         mock.patch.object(hs, "snapshot_project_group", return_value=(None, ["p"])):
        assert rr._stale_project_slice("p", None) == ""


def test_the_recent_episodes_session_start_fallback_never_pushes_a_forgotten_episode(forgotten):
    recents = [{"id": FORGOT, "summary": "FORGOTTEN-RECENT"}, {"id": 904, "summary": "LIVE-RECENT"}]
    with mock.patch("khipu.identity.resolve_repo_root", return_value={"repo_root": None, "project": None}), \
         mock.patch.object(rr, "_current_host_session_id", return_value=None), \
         mock.patch.object(rr, "cwd_search_term", return_value=None), \
         mock.patch.object(activity, "recent_episodes", return_value=recents):
        start = rr._pushed_memory_slice(None)
    assert "FORGOTTEN-RECENT" not in start and "LIVE-RECENT" in start


# ---- explicit search and its log -------------------------------------------

def _mcp_search():
    logged = []
    with mock.patch.object(mcp_server, "_ensure_path"), \
         mock.patch.object(embed, "hybrid_search", return_value={"results": [dict(r) for r in SEARCH_ROWS]}), \
         mock.patch.object(query_log, "log_query", lambda *a, **kw: logged.append(kw["top"])):
        found = mcp_server._tool_search({"query": "alpha"})["results"]
    return [r["id"] for r in found], [r["id"] for r in logged[0]]


def test_mcp_search_drops_and_never_logs_rows_linked_to_a_forgotten_episode(forgotten):
    assert _mcp_search() == (["904", "plain"], ["904", "plain"])


def test_mcp_search_with_an_unreadable_list_shows_no_episode_derived_row():
    _corrupt_list()
    assert _mcp_search() == (["plain"], ["plain"])


def test_cli_search_with_an_unreadable_list_shows_no_episode_derived_row(capsys):
    _corrupt_list()
    logged = []
    with mock.patch.object(embed, "hybrid_search", return_value={"results": [dict(r) for r in SEARCH_ROWS]}), \
         mock.patch.object(query_log, "log_query", lambda *a, **kw: logged.append(kw["top"])):
        cli.cmd_search(argparse.Namespace(query="alpha", limit=10))
    assert [r["id"] for r in json.loads(capsys.readouterr().out)["results"]] == ["plain"]
    assert [r["id"] for r in logged[0]] == ["plain"]


def test_the_query_log_never_writes_a_forgotten_episode(forgotten, monkeypatch, tmp_path):
    log = tmp_path / "queries.jsonl"
    monkeypatch.setattr(query_log, "log_path", lambda: log)
    query_log.log_query("q", mode="hybrid", result_count=3, top=[dict(r) for r in SEARCH_ROWS])
    entry = json.loads(log.read_text().splitlines()[-1])
    assert [r["id"] for r in entry["top"]] == ["904", "plain"]


# ---- graph and get ---------------------------------------------------------

def _graph_con():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE episodes (id INTEGER, topics TEXT, summary TEXT)")
    con.execute("CREATE TABLE edges (src TEXT, dst TEXT, type TEXT, weight REAL)")
    con.execute("INSERT INTO episodes VALUES (902, '[\"secret-topic\"]', 'x')")
    con.execute("INSERT INTO edges VALUES ('episode:902', 'topic:secret-topic', 'capture_topic', 1.0)")
    con.execute("INSERT INTO edges VALUES ('topic:secret-topic', 'topic:other', 'related', 1.0)")
    return con


def test_graph_lookup_of_a_forgotten_episode_reads_as_missing(forgotten):
    with mock.patch.object(hs, "open_snapshot", side_effect=lambda *a, **k: _graph_con()):
        out = hs.graph_neighbors_snapshot("902", 1, 10)
    assert out["edges"] == []
    assert out["episode"] == {"id": FORGOT, "missing": True, "topics": []}
    assert "secret-topic" not in json.dumps(out)


def test_graph_lookup_drops_edges_to_a_forgotten_episode(forgotten):
    with mock.patch.object(hs, "open_snapshot", side_effect=lambda *a, **k: _graph_con()):
        out = hs.graph_neighbors_snapshot("topic:secret-topic", 1, 10)
    assert "episode:902" not in json.dumps(out)
    assert any(e["dst"] == "topic:other" for e in out["edges"])


def test_get_of_a_queued_erasure_reads_as_not_found(monkeypatch):
    monkeypatch.setattr(hs, "_pending_erasure_ids", lambda path=None: {905})
    assert tombstones.episode_withheld(905)
    monkeypatch.setattr(mcp_server, "_ensure_path", lambda: None)
    with pytest.raises(ValueError, match="not found"):
        mcp_server._tool_get({"id": "905"})


def test_get_detail_drops_linked_rows_of_a_forgotten_episode(forgotten):
    row = {"slug": "s", "episodes": [{"kind": "episode", "id": "902"}, {"kind": "episode", "id": "904"}]}
    out = mcp_server._detail_response("topic", row)
    assert [e["id"] for e in out["episodes"]] == ["904"]


# ---- the fence covers every output builder ---------------------------------
#
# Every function that hands rows drawn from episodes to a reader, a push or
# the query log. A new output path must be added here and fenced; the test
# below proves each one reaches tombstones.withhold.
FENCED_OUTPUTS = {
    "recall_rule._pushed_memory_slice": "session start: live, cwd-token and recent slices",
    "recall_rule._stale_project_slice": "session start: offline replica slice",
    "recall_prompt.prior_work_for_prompt": "per-prompt push, khipu_status prior work, warm recall",
    "deliverables.line_from_rows": "the produced line (hub and replica readers)",
    "mcp_server._tool_search": "MCP search",
    "mcp_server._detail_response": "MCP get",
    "cli.cmd_search": "CLI search",
    "cli.cmd_get": "CLI get",
    "cli.cmd_activity": "CLI activity --show",
    "cli._graph_query": "MCP and CLI graph (hub)",
    "hub_snapshot.graph_neighbors_snapshot": "MCP and CLI graph (replica)",
    "hub_snapshot.search_stale_payload": "offline search",
    "query_log.log_query": "the query log",
    "drift.status_payload": "khipu_status recent captures",
    "reflect._search": "khipu_reflect evidence",
}

def test_every_output_builder_reaches_the_fence():
    import importlib
    import inspect

    for name in FENCED_OUTPUTS:
        module, func = name.split(".")
        source = textwrap.dedent(inspect.getsource(getattr(importlib.import_module(f"khipu.{module}"), func)))
        assert _called_names(ast.parse(source)) & FENCE_CALLS, f"{name} bypasses the forget fence"


# ---- the guard: find outputs by what they read ------------------------------
#
# A function that runs a SELECT on a table of episode-derived rows must call
# the fence (a real call; the word in a comment does not count), or carry a
# reviewed exemption comment: ``# forget-fence: <reason>`` when its rows
# never reach a reader (a writer, a count, a check), or ``# forget-fence
# source: <reason>`` when it hands rows to its callers, which the guard then
# checks the same way, call by call up to an output.
FENCED_TABLES = ("episodes", "decisions", "decision_links", "commitments", "deliverables",
                 "briefs", "nodes", "edges")
FENCE_CALLS = {"withhold", "withhold_graph", "withhold_detail", "episode_withheld", "_fenced_slice",
               "without_withheld", "forget_fence_sql", "_fenced_owed_rows"}
_READ_SQL = re.compile(r"\b(?:FROM|JOIN)\s+(?:main\.)?(?:%s)\b" % "|".join(FENCED_TABLES), re.I)
# A SELECT of only aggregates or a constant returns no row's content.
_AGGREGATE_SQL = re.compile(r"^\s*SELECT\s+(?:(?:COUNT|MAX|MIN|SUM|EXISTS)\s*\(|1\b|TRUE\b)", re.I)
_WRITE_SQL = re.compile(r"^\s*(?:INSERT|UPDATE|DELETE)\b", re.I)
_EXEMPT = re.compile(r"#\s*forget-fence:\s*\S.{9,}")
_SOURCE = re.compile(r"#\s*forget-fence source:\s*\S.{9,}")


def _sql_strings(node) -> list[str]:
    doc = ast.get_docstring(node)
    out = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str) and child.value != doc:
            out.append(child.value)
        elif isinstance(child, ast.JoinedStr):
            out.append("".join(v.value if isinstance(v, ast.Constant) else " ? " for v in child.values))
    return out


def _reads_rows(sql: str) -> bool:
    if not _READ_SQL.search(sql) or not re.search(r"\bSELECT\b", sql, re.I):
        return False
    if _WRITE_SQL.search(sql) and not re.search(r"\bRETURNING\b", sql, re.I):
        return False
    return not _AGGREGATE_SQL.search(sql)


def _called_names(node) -> set[str]:
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            names.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None))
    return names


def _comments(source: str) -> list[str]:
    return [tok.string for tok in tokenize.generate_tokens(io.StringIO(source).readline)
            if tok.type == tokenize.COMMENT]


def unfenced_readers(root) -> list[str]:
    """Every function under ``root`` that reads episode-derived rows (runs a
    SELECT on ``FENCED_TABLES``, or calls a ``forget-fence source``) and
    neither calls the fence nor carries an exemption."""
    funcs = {}
    for path in sorted(root.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                source = textwrap.dedent(ast.get_source_segment(text, node) or "")
                comments = _comments(source)
                funcs[f"{path.stem}.{node.name}:{node.lineno}"] = {
                    "name": node.name,
                    "reads": bool(re.search(r"\.execute\(", source))
                    and any(_reads_rows(s) for s in _sql_strings(node)),
                    "calls": _called_names(node),
                    "exempt": any(_EXEMPT.search(c) for c in comments),
                    "source": any(_SOURCE.search(c) for c in comments),
                }
    sources = {f["name"] for f in funcs.values() if f["source"]}
    offenders = []
    for key, f in funcs.items():
        reads = f["reads"] or bool((f["calls"] - {f["name"]}) & sources)
        if reads and not (f["calls"] & FENCE_CALLS or f["exempt"] or f["source"]):
            offenders.append(key)
    return offenders


def test_no_function_reads_episode_rows_past_the_fence():
    from pathlib import Path

    offenders = unfenced_readers(Path(cli.__file__).parent)
    assert not offenders, "unfenced readers of episode-derived rows:\n" + "\n".join(offenders)


LEAKY_MODULE = '''
def owed_for_reader(cur):
    # withhold is only a word here
    cur.execute("SELECT id, text FROM commitments WHERE status = 'open'")
    return cur.fetchall()


def decision_rows(cur):
    # forget-fence source: rows go to callers that fence them
    cur.execute(f"SELECT text FROM decisions WHERE {clause}")
    return cur.fetchall()


def leaky_tool(cur):
    return decision_rows(cur)


def fenced_tool(cur):
    return withhold(decision_rows(cur))


def counter(cur):
    cur.execute("SELECT COUNT(*) FROM episodes")
    return cur.fetchone()


def writer(cur):
    # forget-fence: a writer; returns no row to any reader
    cur.execute("SELECT id FROM episodes WHERE id = %s", (1,))
    return bool(cur.fetchone())
'''


def test_the_guard_finds_a_reader_by_what_it_reads(tmp_path):
    (tmp_path / "leaky.py").write_text(LEAKY_MODULE, encoding="utf-8")
    found = {name.split(":")[0] for name in unfenced_readers(tmp_path)}
    assert found == {"leaky.owed_for_reader", "leaky.leaky_tool"}


