"""Delivery audit regressions: synthetic state, private temp folders, no network."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import multiprocessing
import socket
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timedelta, timezone

import pytest

from khipu import db, owner_act, owner_direction, owner_turns, routing as r
from khipu import routing_advice as ra, routing_delivery as rd
from khipu import routing_directions as directions, routing_gateway_copy as gc
from khipu import routing_replication

NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({
        "usage_accounts": [{"label": "Codex", "harness": "codex"}],
        "routing_reset_check": False}))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setattr(r, "_now", lambda: NOW)
    monkeypatch.setattr(r, "kick_plan_refresh", lambda: False)
    monkeypatch.setattr(rd, "_path", lambda session: tmp_path / (
        hashlib.sha256(session.encode()).hexdigest() + ".routing.json"))
    def forbidden(*a, **kw):
        raise AssertionError("network forbidden")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def item(entity, text, *, whole=True, visible=False):
    return dict(entity=entity, text=text, revision=hashlib.sha256(text.encode()).hexdigest()[:16],
                label=entity, whole=whole, visible=visible, removed=False)


def planned(items, version=1, **extra):
    text = "## Routing\n" + "\n".join(i["text"] for i in items if i["visible"])
    plan = dict(items=copy.deepcopy(items), limit=400, page_limit=400, max_chars=9000,
                heading="## Routing", text=text, current_entities=[i["entity"] for i in items],
                notices=[], lease=60, harness="codex", at=rd._iso(NOW),
                pace_forecasts={}, advice_forecasts={})
    plan.update(extra)
    return dict(epoch="fake-epoch", version=version, kind="changes", text=text,
                generation={"fake_generation": version}, owed_entities=[i["entity"] for i in items],
                shown_entities=[], delivery_plan=plan)


@pytest.mark.parametrize("field,prefix", [("advice_forecasts", "advice"), ("pace_forecasts", "pace")])
def test_routing_forecasts_ack_only_complete_delivered_text(field, prefix):
    forecasts = {f"{prefix}:fake-{i}": dict(account=f"fake-{i}", label=f"Fake {i}",
                 verdict="hold_back") for i in range(8)}
    entries = [item("hold:fake", "All work held: " + "delay " * 45, whole=False, visible=True),
               *[item(e, f"{e}: hold back until reset; synthetic reason.", whole=False, visible=True)
                 for e in forecasts], item("rule:2", "Use only fake accounts. " * 100)]
    out = planned(entries, **{field: forecasts})
    out["delivery_plan"]["line_entities"] = [None, *[i for i, entry in enumerate(entries) if entry["visible"]]]
    answer = rd.offer(out, session="forecasts")
    shown = {e["entity"] for e in entries if e["entity"] in forecasts and e["text"] in answer["text"]}
    assert shown and shown != set(forecasts)
    assert rd.acknowledge("forecasts", answer["routing_changes"], now=NOW)
    acked = rd.snapshot("forecasts").get(prefix + "_acked", {})
    assert set(acked) == shown


def test_routing_full_push_does_not_ack_unshown_hold_back_advice(monkeypatch):
    account = dict(key="fake", harness="claude", label="Fake", state="open",
        caps=[dict(kind="pace_target", window="weekly", purpose="job", pct=65, slack=5, revision="cap1")],
        pace=[dict(window="weekly", used_pct=18, buckets=1, why="one synthetic reading",
                   pace_target=dict(verdict="ahead", allowance_pct=9.26, used_pct=18),
                   plan=dict(verdict="unknown"))])
    view = dict(accounts=[account], priority=dict(entries=[dict(account="fake")]), holds=[],
                effective={"account:fake": "open"}, record=True, epoch="fake-epoch", version=1)
    cfg = dict(r.settings(), routing_line_chars=1000, routing_summary_chars=8000,
               routing_prompt_chars=4000, routing_delivery_lease=60)
    monkeypatch.setattr(r, "settings", lambda: cfg)
    monkeypatch.setattr(r, "account_id", lambda *a: "fake:main")
    monkeypatch.setattr(r, "_for_agents", lambda value, *a: value)
    monkeypatch.setattr(r, "plan_facts", lambda *a: {})
    monkeypatch.setattr(r, "_replacement_notices", lambda *a: [])
    monkeypatch.setattr(r, "_current_line", lambda *a: "Fake: open")
    monkeypatch.setattr(r, "account_line", lambda *a, **kw: "Fake: open")
    monkeypatch.setattr(r, "priority_line", lambda *a, **kw: "Owner priority: Fake")
    monkeypatch.setattr(routing_replication, "age_line", lambda *a: None)
    assert ra.account_advice(view, account, NOW)["verdict"] == "hold_back"
    owners = []
    full = dict(r.summary(view, now=NOW, session_id="full", owners=owners),
                kind="full", line_entities=owners)
    adjusted = r._advice_push_adjust(full, view, NOW, "full", "main")
    first = rd.offer(rd.plan(adjusted, view, NOW, cfg, session="full"), session="full")
    assert "Advice for jobs" not in first["text"]
    assert rd.acknowledge("full", first["routing_changes"], now=NOW)
    assert "advice:fake" not in rd.snapshot("full").get("advice_acked", {})
    second = r._advice_push_adjust(dict(kind="none", text=None), view, NOW, "full", "main")
    assert second["advice_forecasts"]["advice:fake"]["verdict"] == "hold_back"


def test_routing_new_owner_hold_preempts_live_rule_lease():
    rule = item("rule:1", "Use fake account A. " * 100)
    first = rd.offer(planned([rule]), session="hold")
    hold = item("hold:new", "All work: held by the owner until 13:00Z.", whole=False, visible=True)
    second = rd.offer(planned([rule, hold], version=2), session="hold")
    assert hold["text"] in second["text"]
    assert second["routing_changes"]["batch"] != first["routing_changes"]["batch"]
    assert second["version"] == 2
    assert not rd.acknowledge("hold", first["routing_changes"], now=NOW)


@pytest.mark.parametrize("ack_new", [False, True])
def test_routing_delayed_pull_cannot_replace_newer_plan(ack_new):
    old = planned([item("rule:1", "Use fake A.")], version=1)
    new = planned([item("rule:1", "Use fake B.")], version=2)
    ctx = multiprocessing.get_context("fork")
    ready, release = ctx.Event(), ctx.Event()
    def delayed():
        captured = copy.deepcopy(old)
        ready.set()
        assert release.wait(5)
        changes, _ = rd.pull_offer(captured, session="pull", now=NOW)
        if changes:
            rd.pull_acknowledge("pull", changes["batch"], now=NOW)
    process = ctx.Process(target=delayed)
    process.start()
    try:
        assert ready.wait(5)
        changes, _ = rd.pull_offer(new, session="pull", now=NOW)
        if ack_new:
            assert rd.pull_acknowledge("pull", changes["batch"], now=NOW) == "acked"
    finally:
        release.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join()
    assert process.exitcode == 0
    state = rd.snapshot("pull", "mcp")
    assert state["entries"]["rule:1"]["text"] == "Use fake B."
    if ack_new:
        assert state["acked_through"] == 2
    else:
        assert state["flight"]["out"]["routing_changes"]["batch"] == changes["batch"]


@pytest.mark.parametrize("bad", [[], {"main": []}, {"main": {"epoch": "fake-epoch", "entries": {"rule:1": None}}},
                                {"main": {"epoch": "fake-epoch", "entries": {"bad": {"text": 42}}}},
                                {"main": {"epoch": "fake-epoch", "entries": [], "flight": {"until": "bad"}}}])
def test_routing_malformed_ledger_recovers_and_preserves_other_components(bad):
    good = planned([item("rule:good", "Use fake good.", visible=True)])
    rd.offer(good, session="malformed", component="other")
    other = rd.snapshot("malformed", "other")
    if isinstance(bad, dict):
        bad = dict(bad, other=copy.deepcopy(other))
    rd._path("malformed").write_text(json.dumps(bad))
    assert isinstance(rd.snapshot("malformed"), dict)
    assert isinstance(rd.notice_receipts("malformed"), set)
    answer = rd.offer(planned([item("rule:1", "Use fake A.", visible=True)]), session="malformed")
    assert "Use fake A." in answer["text"]
    assert rd.acknowledge("malformed", answer["routing_changes"], now=NOW)
    if isinstance(bad, dict):
        assert rd.snapshot("malformed", "other") == other


def seed_direction(monkeypatch, text="Use Codex for builds."):
    sha = owner_turns.prompt_sha256(text)
    monkeypatch.setattr(directions, "known_entities", lambda: {
        "all": {"codex": "Codex"}, "accounts": {"codex": "Codex"}})
    monkeypatch.setattr(owner_turns, "turns", lambda *a, **kw: [{"prompt_sha256": sha, "occurrence": 1}])
    monkeypatch.setattr(owner_turns, "occurrence_state", lambda *a, **kw: {"counts": {sha: 1}})
    row, = directions.capture_scan("fake-session", [text], actions=[])
    return row


@pytest.mark.parametrize("operation", ["refresh", "capture"])
def test_routing_direction_dismissal_survives_concurrent_writer(monkeypatch, operation):
    row = seed_direction(monkeypatch)
    saving, release = threading.Event(), threading.Event()
    original = directions._save
    def paused_save(data):
        if threading.current_thread().name.startswith("writer") and not saving.is_set():
            saving.set()
            assert release.wait(5)
        original(data)
    monkeypatch.setattr(directions, "_save", paused_save)
    with ThreadPoolExecutor(1, thread_name_prefix="writer") as writer, ThreadPoolExecutor(1) as owner:
        future = writer.submit(directions.refresh, []) if operation == "refresh" else writer.submit(
            directions.capture_scan, "fake-session", [row["words"]], actions=[])
        try:
            assert saving.wait(5)
            dismissed = owner.submit(directions.dismiss, row["id"])
            try:
                dismissed.result(timeout=0.2)
            except TimeoutError:
                pass  # A serialized dismiss waits for the earlier writer.
        finally:
            release.set()
        future.result(timeout=5)
        assert dismissed.result(timeout=5)["status"] == "dismissed"
    assert directions.directions()[0]["status"] == "dismissed"


@pytest.mark.parametrize("text,args", [
    ("Limit Codex to at most 20%.", dict(kind="share", window="weekly", pct=20)),
    ("Limit Codex spend to $20 per week.", dict(kind="spend", usd=20, days=7)),
    ("Codex at most 2 jobs.", dict(kind="concurrency", n=2))])
def test_routing_applied_cap_direction_closes(monkeypatch, text, args):
    row = seed_direction(monkeypatch, text)
    monkeypatch.setattr(owner_direction, "check", lambda *a, **kw: dict(auth="direction", harness="codex",
        session=row["session"], turn="fake-turn", prompt_sha256=row["prompt_sha256"], occurrence=1))
    ok, result = owner_act.act("route.cap.set", dict(account="Codex", **args))
    assert ok, result
    action = owner_direction.actions()[-1]
    assert action["state"] == "applied"
    refreshed, = directions.refresh()
    assert refreshed["status"] == "closed"
    assert refreshed["closed_by"] == action["id"]


@pytest.mark.parametrize("bad", [None, [], "bad", {}, {"id": "bad", "words": [], "session": 1}])
def test_routing_damaged_direction_row_does_not_break_reads_or_capture(monkeypatch, bad):
    row = seed_direction(monkeypatch)
    directions._save({"directions": {row["id"]: row, "damaged": bad}})
    assert [owed["id"] for owed in directions.owed_items()] == [row["id"]]
    assert directions.capture_scan(row["session"], [row["words"]], actions=[]) == [row]
    directions.forget_sessions([row["session"]])
    assert directions.directions() == []


class SQLStore:
    """In-memory adapter for production gateway delivery SQL, with no server."""
    def __init__(self):
        self.rows, self.result = {}, None

    @contextlib.contextmanager
    def connect(self, **kw):
        yield self

    def cursor(self):
        return contextlib.nullcontext(self)

    def execute(self, sql, args=()):
        self.result = None
        if sql.startswith("SELECT state"):
            row = self.rows.get(args[0])
            self.result = (copy.deepcopy(row),) if row is not None else None
        elif sql.startswith("INSERT INTO routing_gateway_delivery"):
            self.rows[args[0]] = json.loads(args[1])
        elif not sql.startswith(("DELETE FROM routing_gateway_delivery", "SELECT pg_advisory",
                                 "SELECT 1 FROM routing_gateway_delivery")):
            raise AssertionError(sql)

    def fetchone(self):
        return self.result


def test_routing_gateway_fold_metadata_does_not_redeliver(monkeypatch):
    store = SQLStore()
    monkeypatch.setattr(db, "connect", store.connect)
    hub = gc.PostgresHub()
    body = dict(available=True, record=True, epoch="fake-epoch", version=7,
                folded_at=r._iso(NOW), valid_until=None, accounts=[], holds=[], rules=[])
    first, _ = hub.delivery("fake-session", body, "", NOW)
    assert hub.delivery("fake-session", body, first["batch"], NOW)[0] is None
    refreshed = dict(body, folded_at=r._iso(NOW + timedelta(seconds=60)))
    changes, health = hub.delivery("fake-session", refreshed, "", NOW + timedelta(seconds=60))
    assert changes is None
    assert health["acked_through"] == 7 and health["in_flight"] == 0
    for field, value in [("accounts", [dict(id="fake", label="Fake", state="open")]),
                         ("rules", [dict(id="fake-rule", text="Use fake.")]),
                         ("holds", [dict(id="fake-hold")]), ("valid_until", r._iso(NOW))]:
        changed = dict(refreshed, **{field: value})
        changes, _ = hub.delivery("fake-session", changed, "", NOW + timedelta(seconds=61))
        assert changes is not None, field
        assert hub.delivery("fake-session", changed, changes["batch"], NOW + timedelta(seconds=61))[0] is None
