"""Delivery audit regressions: synthetic state, private temp folders, no network."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import multiprocessing
import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timedelta, timezone

import pytest

from khipu import db, forget, owner_act, owner_direction, owner_files, owner_turns, routing as r
from khipu import routing_advice as ra, routing_delivery as rd
from khipu import routing_directions as directions, routing_gateway_copy as gc
from khipu import routing_pace, routing_replication

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
    # The hold and reserved rule page consume a 400-character body. Leave
    # room for two forecasts while the other six remain genuinely unshown.
    out["delivery_plan"].update(limit=600, page_limit=600)
    out["delivery_plan"]["line_entities"] = [None, *[i for i, entry in enumerate(entries) if entry["visible"]]]
    answer = rd.offer(out, session="forecasts")
    shown = {e["entity"] for e in entries if e["entity"] in forecasts and e["text"] in answer["text"]}
    assert shown and shown != set(forecasts)
    assert rd.acknowledge("forecasts", answer["routing_changes"], now=NOW)
    acked = rd.snapshot("forecasts").get(prefix + "_acked", {})
    assert set(acked) == shown


def test_routing_full_push_does_not_ack_unshown_hold_back_advice(monkeypatch):
    caps = [dict(kind="pace_target", window="weekly", purpose="job", pct=65, slack=5, revision="cap1")]
    # Cover the full trailing day with fresh readings, well above the weekly
    # target's allowance but below any reached level or imminent crossing.
    readings = [dict(t=NOW - timedelta(minutes=minutes), used=18 + (1440 - minutes) / 120,
                     reset=NOW + timedelta(days=6), mac="fake", source="probe", cycle=None, t_err=0)
                for minutes in range(1440, -1, -30)]
    account = dict(key="fake", harness="claude", label="Fake", state="open", caps=caps,
                   pace=[routing_pace.compute_pace(readings, caps, NOW, "weekly")])
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


def test_routing_unconfirmed_flight_reissued_after_identical_view_rewrite(tmp_path):
    from khipu import recall_prompt

    source = tmp_path / "fake-view.json"
    source.write_text('{"epoch":"fake-epoch","version":1}')

    def generation():
        stat = source.stat()
        return [[str(source), [stat.st_mtime_ns, stat.st_size, stat.st_ino]]]

    out = planned([item("account:fake", "Fake: open", whole=False, visible=True)])
    out["generation"] = generation()
    first = rd.offer(out, session="unconfirmed")
    assert recall_prompt._routing_generation_current(first["generation"])
    replacement = source.with_suffix(".new")
    replacement.write_text(source.read_text())
    replacement.replace(source)
    assert not recall_prompt._routing_generation_current(first["generation"])

    out["generation"] = generation()
    second = rd.offer(out, session="unconfirmed")
    assert second["text"] == first["text"]
    assert second["version"] == first["version"]
    assert second["routing_changes"]["batch"] != first["routing_changes"]["batch"]
    assert second["generation"] == out["generation"]
    assert recall_prompt._routing_generation_current(second["generation"])
    assert not rd.acknowledge("unconfirmed", first["routing_changes"], now=NOW)
    assert rd.acknowledge("unconfirmed", second["routing_changes"], now=NOW)


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


@pytest.mark.parametrize("stale", [False, True])
def test_routing_direction_lock_does_not_keep_forget_open_or_get_cleaned(monkeypatch, stale):
    row = seed_direction(monkeypatch)
    lock = directions.store_path().with_suffix(".lock")
    assert lock.read_bytes() == b""
    inode = lock.stat().st_ino
    if stale:
        at = lock.stat().st_mtime - owner_files.TEMP_STALE_S - 1
        os.utime(lock, (at, at))

    result = forget._owner_words_scan(row["session"], gateway=False)

    assert result["ok"], result
    assert directions.directions() == []
    assert owner_files.kind_of(lock) == "lock"
    assert lock.stat().st_ino == inode
    assert owner_files.clean_temps(
        [row["session"]], now=lock.stat().st_mtime + owner_files.TEMP_STALE_S + 1
    ) == {"removed": 0, "left": 0}
    assert lock.stat().st_ino == inode


@pytest.mark.parametrize("name", ["routing-directions.other.lock", ".routing-directions.lock.tmp"])
def test_routing_direction_lock_exception_does_not_exempt_unknown_files(monkeypatch, name):
    row = seed_direction(monkeypatch)
    other = owner_turns.folder() / name
    other.write_text("unattributed synthetic owner words")
    at = other.stat().st_mtime

    assert owner_files.clean_temps([row["session"]], now=at) == {"removed": 0, "left": 1}
    assert other.exists()
    assert owner_files.clean_temps(
        [row["session"]], now=at + owner_files.TEMP_STALE_S + 1
    ) == {"removed": 1, "left": 0}
    assert not other.exists()


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


@pytest.mark.parametrize("operation", ["refresh", "capture"])
def test_routing_direction_dismissal_survives_process_writer(monkeypatch, operation):
    row = seed_direction(monkeypatch)
    ctx = multiprocessing.get_context("fork")
    saving, release, attempting, dismissed = (ctx.Event() for _ in range(4))
    original = directions._save

    def paused_save(data):
        if multiprocessing.current_process().name == "direction-writer" and not saving.is_set():
            saving.set()
            assert release.wait(5)
        original(data)

    monkeypatch.setattr(directions, "_save", paused_save)

    def write():
        if operation == "refresh":
            directions.refresh([])
        else:
            directions.capture_scan("fake-session", [row["words"]], actions=[])

    def dismiss():
        attempting.set()
        assert directions.dismiss(row["id"])["status"] == "dismissed"
        dismissed.set()

    writer = ctx.Process(target=write, name="direction-writer")
    owner = ctx.Process(target=dismiss, name="direction-owner")
    writer.start()
    try:
        assert saving.wait(5)
        lock = directions.store_path().with_suffix(".lock")
        inode = lock.stat().st_ino
        assert owner_files.clean_temps(
            ["fake-session"], now=lock.stat().st_mtime + owner_files.TEMP_STALE_S + 1
        ) == {"removed": 0, "left": 0}
        assert lock.stat().st_ino == inode
        owner.start()
        assert attempting.wait(5)
        # The independent process must wait for the writer's entire
        # read/modify/write transaction, not overwrite its stale snapshot.
        assert not dismissed.wait(0.2)
    finally:
        release.set()
        for process in (writer, owner):
            if process.pid is not None:
                process.join(5)
                if process.is_alive():
                    process.terminate()
                    process.join()
    assert writer.exitcode == owner.exitcode == 0
    assert dismissed.is_set()
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


@pytest.mark.parametrize("prefix", ["pace", "advice", "advice:check"])
def test_routing_forecast_that_leaves_the_plan_is_dropped_not_announced_removed(prefix):
    # A pace or advice forecast is an offer, never part of the routing view.
    # When a later plan stops offering it (the SessionStart parent's scrubbed
    # plan, or a forecast that no longer differs), the unacknowledged offer
    # ends silently: no "is no longer current" page, no "changed while being
    # shown" restart, and one page per entity sharing a label.
    rows = [f"{prefix}:fake-{i}" for i in range(3)]
    forecast_field = "pace_forecasts" if prefix == "pace" else "advice_forecasts"
    first = [item("account:fake", "Fake: open; read just now.", whole=False, visible=True),
             *[dict(item(e, f"Fake {prefix}: hold back until reset {i}.", whole=False, visible=True), label="Fake")
               for i, e in enumerate(rows)]]
    out = planned(first, **{forecast_field: {e: dict(account="fake", label="Fake") for e in rows}})
    out["delivery_plan"]["line_entities"] = [None, *range(len(first))]
    shown = rd.offer(out, session="gone")
    assert shown["text"] and "Fake" in shown["text"]  # offered, never acknowledged
    second = planned([first[0]], version=2)
    second["delivery_plan"]["line_entities"] = [None, 0]
    # While the first display's lease is live its offers stay for the ack;
    # the leftovers must still never turn into removal pages.
    leased = rd.offer(copy.deepcopy(second), session="gone")
    assert "no longer current" not in (leased.get("text") or "")
    second["delivery_plan"]["at"] = rd._iso(NOW + timedelta(seconds=120))
    answer = rd.offer(second, session="gone")
    text = (answer.get("text") or "") + json.dumps(answer.get("routing_changes") or {})
    assert "no longer current" not in text and "changed while being shown" not in text
    ledger = json.loads(rd._path("gone").read_text())["main"]["entries"]
    assert not any(e.get("removed") or "no longer current" in (e.get("text") or "") for e in ledger.values())
    assert not any(e in ledger and not ledger[e].get("acked") for e in rows)


@pytest.mark.parametrize("field,prefix", [("pace_forecasts", "pace"), ("advice_forecasts", "advice")])
def test_routing_absent_forecast_never_holds_a_renewable_lease_after_invalidation(field, prefix):
    # A generation change withdraws the live lease. A forecast the new plan no
    # longer carries must end there, not render empty, claim and renew a lease
    # at every call, or have its batch acknowledged as shown text.
    entity = f"{prefix}:fake"
    forecast = {entity: dict(account="fake", label="Fake", verdict="hold_back")}
    row = dict(item(entity, f"Fake {prefix}: hold back until reset.", whole=False, visible=True), label="Fake")
    anchor = item("account:fake", "Fake: open; read just now.", whole=False, visible=True)

    def plan(items, version, forecasts, at=NOW):
        out = planned(items, version=version, **{field: forecasts})
        out["delivery_plan"]["at"] = rd._iso(at)
        out["delivery_plan"]["line_entities"] = [None, *range(len(items))]
        return out

    first = rd.offer(plan([anchor, row], 1, forecast), session="phantom")
    assert row["text"] in first["text"]  # offered, never acknowledged
    for seconds in (30, 60, 90, 120):
        answer = rd.offer(plan([anchor], 2, {}, NOW + timedelta(seconds=seconds)), session="phantom")
        assert row["text"] not in (answer.get("text") or "")
        claimed = [i["entity"] for i in (answer.get("routing_changes") or {}).get("items", [])]
        assert entity not in claimed, seconds  # nothing claimed or leased for the absent offer
    ledger = json.loads(rd._path("phantom").read_text())["main"]
    assert entity not in ledger["entries"]
    assert entity not in [i["entity"] for i in ledger["flight"]["out"]["routing_changes"]["items"]]
    # The forecast returns: shown once under a fresh batch, acknowledged normally.
    later = NOW + timedelta(seconds=150)
    back = rd.offer(plan([anchor, row], 3, forecast, later), session="phantom")
    assert back["text"].count(row["text"]) == 1
    assert back["routing_changes"]["batch"] != first["routing_changes"]["batch"]
    assert rd.acknowledge("phantom", back["routing_changes"], now=later)
    assert entity in rd.snapshot("phantom").get(prefix + "_acked", {})
    again = rd.offer(plan([anchor, row], 3, forecast, later + timedelta(seconds=1)), session="phantom")
    assert entity not in [i["entity"] for i in (again.get("routing_changes") or {}).get("items", [])]
