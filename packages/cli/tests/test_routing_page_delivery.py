"""Routing slice 6, delivery: the page is what routing delivers.
Scope: jobs/2026-11-07/scope-routing-one-page.md section 2 ("How it reaches
an agent") and section 6 item 6.

1. Every saved fold writes the page to page.txt, atomically, with its time.
2. The per-prompt hook reads that file, never the view or the log, so its
   budget no longer skips prompts; an old page still arrives, with its time.
3. The page goes whole at session start, and again only when a status word,
   the owner's order or his words change: a percentage drift never resends it.
4. ``khipu_route`` and ``khipu route`` answer the page by default and the full
   record on request.

Hermetic: a data dir per test, a fake clock, the search and the memory slice
stubbed. The page is switched on here (``page_delivery``)."""
from __future__ import annotations

import copy
import io
import itertools
import json
import os
import shutil
import tempfile
import time
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from khipu import agent_view, cli, recall_prompt, recall_rule, routing, routing_delivery, routing_page, routing_page_file
from khipu import mcp_server as ms
from tests.test_routing_page import NOW, _view

T0 = datetime(2026, 11, 8, 15, 0, tzinfo=timezone.utc)
ACCOUNTS = [{"label": "Claude", "harness": "claude"}, {"label": "Codex", "harness": "codex"}]
ME = {"kind": "conversation", "harness": "claude_code", "project": "project-a", "session": "s-one"}
SESSION = "11111111-2222-3333-4444-555555555555"
PROMPT = "what did we decide about the recall hook"
MEMORY = "# Khipu memory\nthe memory part"
HITS = [{"kind": "episode", "id": "41", "score": 0.9, "snippet": "kept the hook fail-open",
         "label": "kept the hook fail-open"}]
_HARNESS_ENV = ("KHIPU_HARNESS", "KHIPU_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID",
                "CLAUDE_CODE_ENTRYPOINT", "CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_HOME", "GROK_HOOK_NAME",
                "GROK_HOOK_EVENT", ms.GATEWAY_ACTIVE_ENV)


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> datetime:
        self.now = self.now + timedelta(**kw)
        return self.now


@pytest.fixture
def home(tmp_path, monkeypatch, page_delivery):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": ACCOUNTS, "routing_prompt_budget_ms": 1000,
                                                  "routing_reset_check": False}))
    capture = Path(tempfile.mkdtemp(prefix="krpd-", dir="/tmp"))  # AF_UNIX paths are short
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(capture))
    for name in ("KHIPU_ISOLATED_DIR", "ROUTING_NOW_OFF", *_HARNESS_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    monkeypatch.setattr(routing, "ROUTING_NOW_DISABLE_FILE", str(tmp_path / "routing_now_disable"))
    monkeypatch.setattr(routing, "kick_plan_refresh", lambda: False)
    monkeypatch.setattr(ms, "_GATEWAY_ACTIVE", False)
    clock = Clock(T0)
    monkeypatch.setattr(routing, "_now", clock)
    monkeypatch.setattr(recall_prompt, "_search_hits", lambda *_a, **_k: {
        "hits": [dict(h) for h in HITS], "legs": ["lexical"], "degraded": None})
    monkeypatch.setattr(recall_prompt, "_deliverable_context_line", lambda *_a, **_k: "")
    monkeypatch.setattr(recall_rule, "session_start_context", lambda cwd=None: MEMORY)
    yield {"data": data, "clock": clock, "tmp": tmp_path}
    shutil.rmtree(capture, ignore_errors=True)


def _set(home, **values) -> None:
    path = home["data"] / "config.json"
    config = json.loads(path.read_text())
    config.update(values)
    path.write_text(json.dumps(config))


def _record() -> dict:
    routing.report(account="Codex", state="limited", until="1h", author=ME)
    routing.report(account="Claude", state="open", author=ME)
    return routing.read_view()


def _push(session: str = "s-page", *, kick=None) -> dict:
    agent_view.prepare_routing_table(refresh=True)
    return routing.prompt_push(None, budget=1.0, session_id=session, kick=kick)


def _deliver(session: str = "s-page") -> dict:
    """One prompt's answer, acknowledged as the hook does once it printed it."""
    out = _push(session)
    if out.get("routing_changes"):
        assert routing_delivery.acknowledge(session, out["routing_changes"], take=True)
    return out


def _boom(*_a, **_k):
    raise AssertionError("the prompt path must not read the view or fold")


# ---- 1. the page file, written on every saved fold ----------------------------------------


def test_every_saved_fold_writes_the_page_file_with_its_time(home):
    view = _record()
    page = routing_page_file.read()
    assert page is not None
    assert (page["version"], page["epoch"]) == (view["version"], view["epoch"])
    assert page["rendered_at"] == view["folded_at"]
    folded = routing._parse(view["folded_at"])
    assert page["text"] == routing_page.page(view, folded)
    assert page["sig"] == routing_page.signature(view, folded)
    raw = routing_page_file.path().read_text()
    assert raw.startswith(routing_page_file.HEADER) and raw.split("\n", 1)[1].startswith("ROUTING ")
    assert "Codex: LIMIT REACHED" in page["text"]
    assert oct(routing_page_file.path().stat().st_mode & 0o777) == "0o600"
    assert not list(routing.routing_dir().glob(".page-*"))  # no temp file left behind
    # The next write's fold writes the next page.
    routing.report(account="Codex", state="open", author=ME)
    again = routing_page_file.read()
    assert again["version"] > page["version"] and again["sig"] != page["sig"]
    assert "Codex: LIMIT REACHED" not in again["text"]


def test_a_failed_page_write_never_fails_the_save_and_leaves_the_old_page_whole(home, monkeypatch):
    _record()
    before = routing_page_file.path().read_text()

    class NoSync:  # the page's fsync fails (a full disk); the view's own save is untouched
        def __getattr__(self, name):
            return getattr(os, name)

        def fsync(self, fd):
            raise OSError("no space left on device")

    monkeypatch.setattr(routing_page_file, "os", NoSync())
    routing.report(account="Codex", state="open", author=ME)  # the write and its save succeed
    view = routing.read_view()
    assert any(a["label"] == "Codex" and a["state"] == "open" for a in view["accounts"])
    assert routing_page_file.path().read_text() == before
    assert not list(routing.routing_dir().glob(".page-*"))


def test_a_page_file_khipu_did_not_write_is_not_read(home):
    _record()
    path = routing_page_file.path()
    for damaged in ("ROUTING 15:00Z. no header\n", routing_page_file.HEADER + "{not json\nROUTING x\n",
                    routing_page_file.HEADER + json.dumps({"sig": "a", "version": "7"}) + "\nROUTING x\n",
                    "x" * (routing_page_file.READ_MOST + 10)):
        path.write_text(damaged)
        assert routing_page_file.read() is None


# ---- 2. the prompt reads the file, never the view -----------------------------------------


def test_a_prompt_reads_the_page_file_and_never_folds_or_reads_the_view(home, monkeypatch):
    _record()
    page = routing_page_file.read()
    for name in ("fold", "_load_view", "read_view", "_prompt_view", "_inputs", "_light_inputs", "summary"):
        monkeypatch.setattr(routing, name, _boom)
    kicks: list[str] = []
    out = _push(kick=kicks.append)
    assert out["kind"] == "full" and out["text"] == page["text"]
    assert out["version"] == page["version"] and out["generation"] == []
    assert kicks == []  # the page is current: nothing to refold


def test_with_no_page_file_the_prompt_writes_one_from_the_saved_view_without_folding(home, monkeypatch):
    view = _record()
    routing_page_file.path().unlink()
    folds: list[int] = []
    real = routing.fold
    monkeypatch.setattr(routing, "fold", lambda *a, **k: folds.append(1) or real(*a, **k))
    out = _push()
    assert out["kind"] == "full" and out["text"].startswith("ROUTING ")
    assert folds == []
    page = routing_page_file.read()
    assert page is not None and page["version"] == view["version"]


def test_an_old_page_still_arrives_with_its_time_and_asks_for_a_refold(home):
    _record()
    page = routing_page_file.read()
    home["clock"].advance(minutes=10)  # past routing_not_refreshed_after (5 min); nothing refolded
    kicks: list[str] = []
    out = _push(kick=kicks.append)
    assert out["kind"] == "full"
    assert out["text"].startswith(page["text"])
    assert "This page was rendered 15:00Z; the record may have moved since." in out["text"]
    assert kicks == ["age"]
    # The refold the kick asks for finds the record current and stamps the page's time.
    assert routing.refold_in_background() == "fresh"
    assert routing_page_file.read()["rendered_at"] == routing._iso(home["clock"].now)


def test_a_write_behind_the_pages_back_asks_for_a_refold(home):
    _record()
    with routing.events_file().open("a") as fh:
        fh.write("\n")
    kicks: list[str] = []
    _push(kick=kicks.append)
    assert kicks == ["inputs"]


# ---- 3. when the page is sent again ---------------------------------------------------------


def _hand(view: dict, version: int) -> dict:
    out = copy.deepcopy(view)
    out.update(version=version, folded_at=routing._iso(NOW), valid_until=None)
    return out


def _claude(view: dict, used: float, resets: datetime | None = None) -> dict:
    pace = next(a for a in view["accounts"] if a["key"] == "claude|")["pace"][0]
    pace["used_pct"] = used
    pace["pace_target"].update(used_pct=used, verdict="ahead" if used > pace["pace_target"]["allowance_pct"] else "within")
    if resets is not None:
        pace["cycle_resets_at"] = resets.strftime("%Y-%m-%dT%H:%M:%SZ")
    return view


def test_the_signature_moves_with_status_order_and_rules_never_with_a_reading():
    base = _view()
    sig = routing_page.signature(base, NOW)
    drift = _claude(copy.deepcopy(base), 69.0, NOW + timedelta(days=1, hours=18, minutes=3))
    assert routing_page.page(drift, NOW) != routing_page.page(base, NOW)  # the page's numbers moved
    assert routing_page.signature(drift, NOW) == sig  # but nothing that resends it
    assert routing_page.signature(base, NOW + timedelta(minutes=7)) == sig  # nor the clock alone
    status = _claude(copy.deepcopy(base), 80.0)
    assert "Claude: HOLD BACK" in routing_page.page(status, NOW)
    order = copy.deepcopy(base)
    order["priority"]["entries"].reverse()
    rule = copy.deepcopy(base)
    rule["rules"][0]["text"] += " Haiku never reviews."
    role = copy.deepcopy(base)
    role["priority"]["entries"][0]["role"] = "Only for the riskiest reviews."
    for changed in (status, order, rule, role):
        assert routing_page.signature(changed, NOW) != sig


def test_the_page_is_sent_again_only_when_a_status_word_the_order_or_a_rule_changes(home):
    routing.routing_dir().mkdir(parents=True, exist_ok=True)
    home["clock"].now = NOW
    base = _hand(_view(), 41)
    steps = [
        ("first prompt", base, "full"),
        ("nothing changed", _hand(base, 42), "none"),
        ("percentages drift", _claude(_hand(base, 43), 69.0, NOW + timedelta(days=1, hours=18, minutes=3)), "none"),
        ("a status word", _claude(_hand(base, 44), 80.0), "full"),
        ("same again", _claude(_hand(base, 45), 81.0), "none"),
    ]
    order = _claude(_hand(base, 46), 81.0)
    order["priority"]["entries"].reverse()
    steps.append(("the order", order, "full"))
    rule = copy.deepcopy(order)
    rule.update(version=47)
    rule["rules"][0]["text"] += " Haiku never reviews."
    steps.append(("a rule", rule, "full"))
    for name, view, kind in steps:
        assert routing_page_file.write(view, NOW), name
        out = _deliver()
        assert out["kind"] == kind, (name, out.get("text"))
        if kind == "full":
            assert out["text"] == routing_page_file.read()["text"], name
    assert "1. Deepseek" not in out["text"]  # sanity: labels, not keys
    # Another conversation has seen none of it: it gets the current page.
    assert _deliver("s-other")["kind"] == "full"


def test_an_unacknowledged_page_is_offered_again(home):
    _record()
    first = _push()
    assert first["kind"] == "full" and first.get("routing_changes")
    again = _push()  # the hook never printed the first (late, too long): still owed
    assert again["kind"] == "full" and again["text"] == first["text"]


def test_a_page_too_long_for_the_hooks_room_arrives_in_pages(home):
    _record()
    out = _push()
    room = 120 + len(routing_delivery.PAGE_HEADING)
    pages, seen = [], 0
    while out.get("routing_changes") and seen < 80:
        fitted = routing_delivery.refit("s-page", out["routing_changes"], room)
        assert fitted and fitted["text"].startswith(routing_delivery.PAGE_HEADING)
        assert routing_delivery.acknowledge("s-page", fitted["routing_changes"], take=True)
        pages.append(fitted["text"])
        out = _push()
        seen += 1
    assert len(pages) > 1 and out["kind"] == "none"
    assert "Routing page; revision" in pages[0] and "page 1/" in pages[0]


# ---- the hooks, as their launchers run them ----------------------------------------------------


def _start(monkeypatch, source: str = "startup") -> str:
    payload = {"session_id": SESSION, "cwd": "/work/project-a", "source": source}
    monkeypatch.setattr(recall_rule.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(recall_rule.sys, "argv", ["khipu-recall-hook"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        recall_rule.session_start_main(shape="claude")
    return json.loads(buf.getvalue())["hookSpecificOutput"]["additionalContext"]


def _prompt(monkeypatch) -> str:
    payload = {"prompt": PROMPT, "cwd": "/work/project-a", "hook_event_name": "UserPromptSubmit",
               "session_id": SESSION}
    monkeypatch.setattr(recall_prompt.sys, "stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    with redirect_stdout(buf):
        recall_prompt.prompt_recall_main(shape="claude")
    data = json.loads(buf.getvalue())
    return data["hookSpecificOutput"]["additionalContext"] if data else ""


def _log() -> str:
    return recall_prompt._log_path().read_text().splitlines()[-1]


def test_session_start_sends_the_whole_page_every_time_and_prompts_stay_quiet(home, monkeypatch):
    _record()
    page = routing_page_file.read()["text"]
    assert _start(monkeypatch) == MEMORY + "\n\n" + page
    assert "ROUTING" not in _prompt(monkeypatch)  # the start's page was acknowledged
    assert _log().endswith(" routing=none")
    # A resume or compaction is a start again: the whole page, though already seen.
    home["clock"].advance(minutes=1)
    again = _start(monkeypatch, "compact")  # rendered at the start's own moment
    assert again.startswith(MEMORY + "\n\nROUTING 15:01Z. ") and again.endswith(page.split("\n", 1)[1])
    # The owner adds a rule: the next prompt carries the new page, once.
    routing.add_rule("Never a -fast tier.", via="app")
    ctx = _prompt(monkeypatch)
    assert ctx.endswith(routing_page_file.read()["text"]) and "Never a -fast tier." in ctx
    assert _log().endswith(" routing=full")
    assert "ROUTING" not in _prompt(monkeypatch)


@pytest.mark.parametrize("page_on", [True, False])
def test_a_slow_view_read_no_longer_skips_the_prompt(home, monkeypatch, page_on):
    """The per-prompt child of the one-shot hook is forked from this
    process, so a slow view read patched here is the child's. With the page
    the prompt never reads the view and the routing lands; the earlier
    change push reads it and is skipped as out of budget."""
    _record()
    if not page_on:
        monkeypatch.setenv("KHIPU_ROUTING_PAGE", "0")
    _set(home, routing_prompt_budget_ms=500)
    real = routing._load_view

    def slow():
        time.sleep(3.0)
        return real()

    monkeypatch.setattr(routing, "_load_view", slow)
    ctx = _prompt(monkeypatch)
    if page_on:
        assert ctx.endswith(routing_page_file.read()["text"])
        assert _log().endswith(" routing=full")
    else:
        assert "## Routing" not in ctx
        assert _log().endswith(" routing=skipped:budget")


# ---- 4. khipu_route and khipu route -------------------------------------------------------------

_IDS = itertools.count(1)


def _call(name: str, arguments: dict | None = None) -> tuple[bool, dict]:
    out = ms.handle_message({"jsonrpc": "2.0", "id": next(_IDS), "method": "tools/call",
                             "params": {"name": name, "arguments": arguments or {}}})
    result = out["result"]
    return result["isError"], json.loads(result["content"][0]["text"])


def test_khipu_route_answers_the_page_by_default_and_the_full_record_on_request(home, monkeypatch):
    project = home["tmp"] / "project-z"
    project.mkdir()
    monkeypatch.chdir(project)
    view = _record()
    error, body = _call("khipu_route")
    assert not error, body
    assert set(body) == {"page", "chars", "version", "epoch", "full_record"}
    assert body["page"].startswith("ROUTING ") and "Codex: LIMIT REACHED" in body["page"]
    assert body["chars"] == len(body["page"]) <= routing_page.LIMIT
    assert body["version"] == view["version"] and "full=true" in body["full_record"]
    error, full = _call("khipu_route", {"full": True})
    assert not error, full
    assert "page" not in full and isinstance(full.get("accounts"), list) and "delivery" in full
    for args in ({"view": "rules"}, {"account": "Codex"}, {"summary": True}, {"since": "0"}):
        error, other = _call("khipu_route", args)
        assert not error and "page" not in other, args  # a specific ask still gets the record
    error, bad = _call("khipu_route", {"full": "maybe"})
    assert error and "full must be boolean" in json.dumps(bad)
    tool = next(t for t in ms.TOOLS if t["name"] == "khipu_route")
    assert "pass `full: true` for the full record." in tool["description"]
    assert tool["inputSchema"]["properties"]["full"]["type"] == "boolean"


def test_khipu_route_on_the_command_line_prints_the_page_and_full_on_request(home, capsys):
    view = _record()
    assert cli.main(["route"]) == 0
    printed = capsys.readouterr().out
    folded = routing._now()
    assert printed.rstrip("\n") == routing.page(view, folded, *routing._page_inputs(view, folded))
    assert cli.main(["route", "--full"]) == 0
    full = capsys.readouterr().out
    assert not full.startswith("ROUTING ") and "Codex" in full
    assert cli.main(["route", "--json"]) == 0  # the app's read: always the record
    assert isinstance(json.loads(capsys.readouterr().out).get("accounts"), list)


# ---- the recall daemon's path (Claude Code and Codex hooks both reach it) ----------------------

CLIENT = Path(__file__).resolve().parents[1] / "bin" / "khipu-recall-client.py"


def test_the_daemon_path_delivers_the_page_once(home):
    import subprocess
    import sys
    import threading

    from khipu import recall_daemon

    capture = Path(os.environ["KHIPU_CAPTURE_HOME"])
    svc = recall_daemon.RecallService(capture / "recall.sock", stamp_fn=lambda: "fixed",
                                      idle_check=False, routing_forkserver=False)
    assert svc.bind()
    thread = threading.Thread(target=svc.run, daemon=True)
    thread.start()
    try:
        _record()
        payload = {"prompt": PROMPT, "cwd": "/work/project-a", "hook_event_name": "UserPromptSubmit",
                   "session_id": SESSION}
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ["HOME"], "KHIPU_CAPTURE_HOME": str(capture)}

        def client() -> str:
            proc = subprocess.run([sys.executable, "-S", "-E", "-B", str(CLIENT), "claude"],
                                  input=json.dumps(payload).encode(), env=env, capture_output=True, timeout=10)
            assert proc.returncode == 0, proc.stderr
            data = json.loads(proc.stdout.decode())
            return data["hookSpecificOutput"]["additionalContext"] if data else ""

        assert client().endswith(routing_page_file.read()["text"])
        deadline = time.monotonic() + 5
        while recall_prompt.routing_marker(SESSION).get("version") is None and time.monotonic() < deadline:
            time.sleep(0.02)  # the daemon records delivery after the client's ack, on its own thread
        assert "ROUTING" not in client()
    finally:
        svc.stop()
        thread.join(5)
        svc.close()


def test_the_routing_imports_cover_everything_the_page_read_imports(home):
    """The prompt's budget starts after ``routing.prompt_imports``: nothing
    the page read imports lazily is left for the budget to time."""
    import subprocess
    import sys

    _record()
    code = ("import sys\n"
            "from khipu import recall_prompt, routing\n"
            "routing.prompt_imports()\n"
            "from khipu import agent_view\n"
            "agent_view.prepare_routing_table()\n"
            "before = set(sys.modules)\n"
            "out = routing.prompt_push(recall_prompt.routing_marker('fresh'), budget=1.0, session_id='fresh')\n"
            "assert out['kind'] == 'full', out\n"
            "print(sorted(m for m in set(sys.modules) - before if m.startswith('khipu')))\n")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr.decode()[-2000:]
    assert proc.stdout.decode().strip() == "[]"


# ---- doctor speaks of the page, not the old summary --------------------------------------------


def _long_rules(n=12, size=450):
    for i in range(n):
        routing.add_rule(f"RULES: rule {i} " + "x" * size, via="app")


def test_doctor_names_the_headings_the_page_left_out_and_not_the_old_summary_as_a_conversations(home):
    _set(home, routing_rule_chars=0, routing_rules_max=0, routing_rules_chars=900)
    _record()
    _long_rules()
    block = routing.doctor_block()
    page_words = [w for w in block["warnings"] if w.startswith("The routing page left out")]
    assert len(page_words) == 1 and "RULES" in page_words[0] and "4,500" in page_words[0]
    assert not any("at the start of a conversation shows only part" in w for w in block["warnings"])
    assert not any(w.startswith("The summary had to leave out") for w in block["warnings"])
    assert any(w.startswith("The summary subagents get shows only part of your words") for w in block["warnings"])


def test_doctor_says_nothing_about_the_page_when_every_rule_fits_on_it(home):
    _record()
    routing.add_rule("NEVER: a short rule.", via="app")
    assert not any("routing page" in w for w in routing.doctor_block()["warnings"])


def test_doctor_keeps_the_old_wording_when_the_page_is_off(home, monkeypatch):
    monkeypatch.setenv("KHIPU_ROUTING_PAGE", "0")
    _set(home, routing_rule_chars=0, routing_rules_max=0, routing_rules_chars=900)
    _record()
    _long_rules()
    warnings = routing.doctor_block()["warnings"]
    assert any(w.startswith("The summary at the start of a conversation shows only part") for w in warnings)
    assert not any("routing page" in w for w in warnings)
