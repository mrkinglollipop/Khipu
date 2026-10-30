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
               "without_withheld", "forget_fence_sql", "_fenced_owed_rows", "fenced_texts", "fence_chunks"}
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




# ---- the provider guard: text reaches a provider only through the chokepoint --
#
# A function that calls a provider client (an embedding, summariser, rerank or
# model call) must itself call ``provider_text.fenced_texts`` /
# ``fence_chunks`` (text derived from stored episodes) or ``live_input`` (input
# that is not stored memory). An exemption comment does not count: only a real
# call does, and the client functions themselves are the one layer judged by
# their callers.
PROVIDER_CLIENTS = {
    "embed_batch", "embed_one", "embed_batch_images", "embed_text", "_embed_query_local",
    "_generate", "_generate_local", "_generate_gemini", "_generate_cloud", "_generate_api",
    "_generate_anthropic", "_generate_openai", "_generate_openrouter", "_generate_claude_cli",
    "_generate_codex_cli", "_call_model", "_call_within",
}
CHOKEPOINT_CALLS = {"fenced_texts", "fence_chunks", "live_input"}


def unchoked_provider_callers(root) -> list[str]:
    """Every function under ``root`` that calls a provider client without
    calling the chokepoint, whatever comments it carries."""
    offenders: list[str] = []

    def visit(node, path: str, inside_client: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                is_client = child.name in PROVIDER_CLIENTS
                calls = _called_names(child)
                if (not (inside_client or is_client) and calls & PROVIDER_CLIENTS
                        and not calls & CHOKEPOINT_CALLS):
                    offenders.append(f"{path}.{child.name}:{child.lineno}")
                visit(child, path, inside_client or is_client)
            else:
                visit(child, path, inside_client)

    for file in sorted(root.glob("*.py")):
        visit(ast.parse(file.read_text(encoding="utf-8")), file.stem, False)
    return offenders


def test_every_provider_caller_gets_its_text_from_the_chokepoint():
    from pathlib import Path

    offenders = unchoked_provider_callers(Path(cli.__file__).parent)
    assert not offenders, "provider callers that skip provider_text:\n" + "\n".join(offenders)


PROVIDER_MODULE = '''
def sends_rows(cur):
    # forget-fence: a writer; returns no row to a reader
    rows = cur.fetchall()
    return embed_batch([r[1] for r in rows])


def sends_summary(prompt):
    # forget-fence source: this comment grants nothing to a provider caller
    return extract._generate(prompt)


def fenced_sender(texts):
    safe = provider_text.fenced_texts(texts, [1] * len(texts), "episode")
    return embed_batch([t for t in safe if t is not None])


def live_sender(query):
    return embed_one(provider_text.live_input(query, "query"))


def embed_one(text):
    return embed_batch([text])


def nested(texts):
    def _again(t):
        return _generate_gemini(t)
    return provider_text.fenced_texts(texts, None, "reflect"), _again
'''


def test_the_provider_guard_finds_a_caller_that_skips_the_chokepoint(tmp_path):
    (tmp_path / "sender.py").write_text(PROVIDER_MODULE, encoding="utf-8")
    found = {name.split(":")[0] for name in unchoked_provider_callers(tmp_path)}
    assert found == {"sender.sends_rows", "sender.sends_summary", "sender._again"}



# ---- embedding sources and the briefs plan --------------------------------

def _episode_source_cur():
    from unittest import mock

    cur = mock.MagicMock()
    cur.fetchall.return_value = [
        (902, "FORGOTTEN-EMBED", [], [], ["secret-topic"], []),
        (904, "plain live text", [], [], [], []),
    ]
    return cur


def test_embedding_sources_never_yield_a_forgotten_episode(forgotten):
    from unittest import mock

    with mock.patch("khipu.db.has_columns", side_effect=lambda c, t, col: col != "verbatim"):
        out = list(embed._iter_sources(_episode_source_cur(), kind="episode"))
    assert [ref for _k, ref, _t, _ti in out] == ["904"]
    assert not any("FORGOTTEN-EMBED" in text for _k, _r, text, _ti in out)


def test_embedding_sources_yield_no_episode_when_the_list_is_unreadable():
    from unittest import mock

    _corrupt_list()
    with mock.patch("khipu.db.has_columns", side_effect=lambda c, t, col: col != "verbatim"):
        out = list(embed._iter_sources(_episode_source_cur(), kind="episode"))
    assert out == []
    state = tombstones.read_fence_state()
    assert state is not None and state["readable"] is False


def test_the_recent_missing_catch_up_sends_no_forgotten_text_to_the_provider(forgotten):
    from unittest import mock

    cur = mock.MagicMock()
    cur.fetchall.side_effect = [[(902, "FORGOTTEN-EMBED", [], [], [], [])], [], []]
    conn = mock.MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur
    sent: list = []

    def fake_embed(texts, **kw):
        sent.extend(texts)
        return [[0.0] for _ in texts]

    with mock.patch("khipu.db.connect", return_value=conn), \
            mock.patch("khipu.db.has_columns", return_value=False), \
            mock.patch.object(embed, "_active_profile", return_value="p"), \
            mock.patch.object(embed, "_existing_hashes_for_refs", return_value={}), \
            mock.patch.object(embed, "embed_batch", side_effect=fake_embed), \
            mock.patch.object(embed, "_lock_live_embedding_refs", return_value=set()):
        try:
            embed.embed_recent_missing(limit=5)
        except Exception:  # noqa: BLE001 - only what reached the provider matters here
            pass
    assert not any("FORGOTTEN-EMBED" in t for t in sent)
    assert sent == []


# ---- every provider path, in both list states (Kimi's fourth probes) --------

@pytest.fixture(params=["forgotten", "unreadable"])
def list_state(request):
    if request.param == "forgotten":
        tombstones.publish_episode_ids({FORGOT})
    else:
        _corrupt_list()
    return request.param


def _cursor(*answers):
    cur = mock.MagicMock()
    cur.fetchall.side_effect = list(answers)
    return cur


def test_capture_embedding_sends_nothing_for_a_forgotten_episode(list_state):
    sent: list = []
    row = {"id": 902, "summary": "FORGOTTEN-CAPTURE", "decisions": [], "preferences": [],
           "topics": [], "people": []}
    cur = mock.MagicMock()
    cur.fetchone.return_value = (row,)
    conn = mock.MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur
    with mock.patch("khipu.db.connect", return_value=conn), \
            mock.patch.object(embed, "_active_profile", return_value="p"), \
            mock.patch.object(embed, "embed_batch", side_effect=lambda t, **k: sent.extend(t) or []):
        assert embed.embed_on_capture({"summary": "FORGOTTEN-CAPTURE", "ts": "2026-10-30T00:00:00Z"},
                                      episode_id=902) is False
    assert sent == []


def test_episode_edit_re_embedding_sends_nothing_for_a_forgotten_episode(list_state):
    sent: list = []
    cur = mock.MagicMock()
    cur.fetchone.return_value = ("FORGOTTEN-EDIT", [], [], [], [], None)
    with mock.patch.object(cli, "_episode_live_clause", return_value=""), \
            mock.patch.object(cli, "_episode_verbatim_select", return_value="NULL"), \
            mock.patch.object(embed, "_active_profile", return_value="p"), \
            mock.patch.object(embed, "embed_batch", side_effect=lambda t, **k: sent.extend(t) or []):
        assert cli._reembed_episode(cur, 902) is False
    assert sent == []


def test_commitment_sources_never_yield_text_from_a_forgotten_episode(list_state):
    cur = _cursor([(7, "FORGOTTEN-COMMITMENT")], [(7, 902, None)])
    assert list(embed._iter_sources(cur, kind="commitment")) == []


def test_a_commitment_from_a_live_episode_is_still_embedded():
    tombstones.publish_episode_ids({FORGOT})
    cur = _cursor([(8, "plain commitment")], [(8, 904, None)])
    assert [t for _k, _r, t, _ti in embed._iter_sources(cur, kind="commitment")] == ["plain commitment"]


def test_topic_sources_never_yield_a_topic_a_forgotten_episode_feeds(list_state):
    cur = _cursor([("secret-topic", "secret-topic", "FORGOTTEN-TOPIC")], [("secret-topic", 902)])
    assert list(embed._iter_sources(cur, kind="topic")) == []


def test_a_topic_whose_sources_are_unknown_is_not_sent():
    tombstones.publish_episode_ids({999})
    cur = mock.MagicMock()
    cur.fetchall.side_effect = [[("t", "t", "body")], RuntimeError("lookup failed")]
    assert list(embed._iter_sources(cur, kind="topic")) == []


def test_backfill_batches_pass_the_chokepoint_again(list_state):
    cur = _cursor([("secret-topic", 902)])
    out = embed.provider_text.fence_chunks(cur, [("topic", "secret-topic", "FORGOTTEN-TOPIC"),
                                                 ("episode", "902", "FORGOTTEN-EPISODE")])
    assert out == [None, None]


def test_reflect_sends_no_source_derived_from_a_forgotten_episode(list_state):
    from khipu import reflect

    prompts: list = []
    sources = [{"kind": "topic", "id": "secret-topic", "origin": [902], "text": "FORGOTTEN-TOPIC",
                "summary": "FORGOTTEN-TOPIC", "decisions": []}]
    rows = [{"kind": "topic", "id": "secret-topic"}]
    conn = mock.MagicMock()
    conn.__enter__.return_value = conn
    with mock.patch.object(reflect, "_search", return_value=rows), \
            mock.patch.object(reflect, "gather", return_value=sources), \
            mock.patch("khipu.db.connect", return_value=conn), \
            mock.patch("khipu.rerank._call_model", side_effect=lambda p, **k: prompts.append(p) or ("{}", "m")):
        out = reflect.reflect("what is secret-topic?")
    assert prompts == []
    assert out.get("reason") == "no-sources"


def test_rerank_shows_the_model_no_candidate_a_forgotten_episode_feeds(list_state):
    import time

    from khipu import rerank

    prompts: list = []
    rows = [{"kind": "topic", "id": "secret-topic", "snippet": "FORGOTTEN-TOPIC"},
            {"kind": "episode", "id": "904", "snippet": "plain live text"},
            {"kind": "episode", "id": "905", "snippet": "other live text"}]
    with mock.patch.object(rerank, "_call_model",
                           side_effect=lambda p, **k: prompts.append(p) or ('{"order": [0, 1]}', "m")):
        out = rerank.rerank("q", rows, deadline=time.monotonic() + 5,
                            topic_sources={"secret-topic": [902]})
    assert not any("FORGOTTEN-TOPIC" in p for p in prompts)
    if list_state == "unreadable":
        assert prompts == [] and out["order"] is None
    else:
        assert out["order"] == [1, 2]


def test_a_topic_row_with_unknown_sources_is_not_reranked():
    import time

    from khipu import rerank

    tombstones.publish_episode_ids({999})
    prompts: list = []
    rows = [{"kind": "topic", "id": "t", "snippet": "TOPIC-TEXT"},
            {"kind": "episode", "id": "904", "snippet": "a"}, {"kind": "episode", "id": "905", "snippet": "b"}]
    with mock.patch.object(rerank, "_call_model",
                           side_effect=lambda p, **k: prompts.append(p) or ('{"order": [0, 1]}', "m")):
        out = rerank.rerank("q", rows, deadline=time.monotonic() + 5)
    assert prompts and "TOPIC-TEXT" not in prompts[0] and out["order"] == [1, 2]


def test_the_chokepoint_refuses_the_wrong_door():
    from khipu import provider_text

    with pytest.raises(ValueError):
        provider_text.fenced_texts(["x"], [1], "query")
    with pytest.raises(ValueError):
        provider_text.live_input("x", "topic")


def test_hygiene_and_owed_tidy_judges_send_no_forgotten_or_unsourced_commitment(list_state):
    from khipu import hygiene, owed_tidy
    from khipu.provider_text import sourced

    sent: list = []
    texts = [sourced("FORGOTTEN-JUDGE", [902]), sourced("live judge text", [904]), "no source at all"]
    with mock.patch("khipu.extract._generate", side_effect=lambda p, **k: sent.append(p) or "{}"):
        hygiene._model_judge(texts)
        owed_tidy._model_owner_judge(texts)
        hygiene._model_adjudicate_pairs([(texts[0], texts[1]), (texts[1], texts[2])])
    assert not any("FORGOTTEN-JUDGE" in p or "no source at all" in p for p in sent)
    if list_state == "unreadable":
        assert sent == []
