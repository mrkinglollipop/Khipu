"""Two disposable Macs and an in-memory hub; never a credential or network read."""
import copy
import json
from datetime import timedelta

import pytest

from khipu import routing
from tests.test_routing_acceptance import Replay, T0, AUTHOR


class Hub:
    def __init__(self):
        self.now = T0
        self.rows = {}
        self.calls = []
        self.fail_upload = False
        self.offline = False
        self.number_floor = 0

    def reserve_numbers(self, mac, floor):
        if self.offline:
            raise OSError("SENTINEL-PRIVATE-CONNECTION")
        self.number_floor = max(self.number_floor, floor)

    def upload(self, mac, payload):
        self.calls.append(copy.deepcopy(payload))
        if self.offline:
            raise OSError("SENTINEL-PRIVATE-CONNECTION")
        if payload["id"] not in self.rows:
            number = None
            if payload["type"] == "rule_add":
                number = routing._rule_number(payload)
                if number is None:
                    number = max([self.number_floor] + [r.get("rule_number") or 0 for r in self.rows.values()]) + 1
            self.rows[payload["id"]] = dict(payload=copy.deepcopy(payload),
                event_id=payload["id"], hub_order=len(self.rows) * 3 + 1,
                received_at=routing._iso(self.now), rule_number=number)
        if self.fail_upload:
            self.fail_upload = False
            raise OSError("committed, response lost")
        return copy.deepcopy(self.rows[payload["id"]])

    def pull(self, after, until, limit):
        if self.offline:
            raise OSError("SENTINEL-PRIVATE-CONNECTION")
        upper = until or routing._iso(self.now)
        rows = sorted((r for r in self.rows.values()
                       if (r["received_at"], r["event_id"]) > tuple(after)
                       and r["received_at"] <= upper),
                      key=lambda r: (r["received_at"], r["event_id"]))
        return dict(rows=copy.deepcopy(rows[:limit]), complete=len(rows) <= limit,
                    hub_now=routing._iso(self.now), until=upper,
                    sources=[dict(mac="synthetic", last_seen_at=routing._iso(self.now))])


@pytest.fixture
def pair(tmp_path, monkeypatch):
    monkeypatch.setattr(routing, "kick_plan_refresh", lambda: False)
    monkeypatch.setattr(routing, "_hub_configured", lambda: True)
    monkeypatch.setenv("KHIPU_SESSION_ID", "synthetic-s11b")
    a = Replay(tmp_path / "a", monkeypatch)
    b = Replay(tmp_path / "b", monkeypatch)
    for mac in (a, b):
        mac.configure(routing_replication_live=True, routing_proposal_text_between_macs=False)
    return a, b, Hub()


def sync(mac, hub, **kw):
    from khipu import routing_replication as replication
    mac.activate()
    return replication.sync(hub=hub, **kw)


@pytest.mark.parametrize("reverse", [False, True])
def test_12_hub_order_numbers_converge_and_local_history_is_unchanged(pair, reverse):
    a, b, hub = pair
    added = []
    before = []
    for mac, words in ((a, "alpha"), (b, "beta")):
        mac.activate()
        added.append(routing.add_rule(words, via="app"))
        assert added[-1]["n"] is None
        with pytest.raises(routing.RouteError, match=added[-1]["entity"]):
            routing.rule_reference(1)
        before.append(routing.events_file().read_bytes())
    order = (b, a) if reverse else (a, b)
    for mac in (*order, *order):
        assert sync(mac, hub)["ok"]
    expected = {r["payload"]["entity"]: i for i, r in enumerate(
        sorted(hub.rows.values(), key=lambda r: r["hub_order"]), 1)}
    assert set(expected.values()) == {1, 2}
    for mac, original in zip((a, b), before):
        mac.activate()
        rules = mac.view()["rules"]
        assert {r["entity"]: r["n"] for r in rules} == expected
        assert not any(r["number_pending"] for r in rules)
        assert routing.events_file().read_bytes() == original
        for rule in rules:
            assert routing.rule_reference(rule["n"])[0] == rule["entity"]


def test_12_interrupted_upload_retries_by_id_and_never_reships_remote(pair):
    a, b, hub = pair
    a.activate()
    routing.add_rule("one", via="app")
    routing.add_rule("two", via="app")
    hub.fail_upload = True
    assert not sync(a, hub)["ok"]
    assert len(hub.rows) == 1
    assert sync(a, hub)["ok"]
    assert len(hub.rows) == 2
    assert hub.calls[0]["id"] == hub.calls[1]["id"]
    calls = len(hub.calls)
    assert sync(a, hub)["ok"]
    assert sync(b, hub)["ok"]
    assert len(hub.calls) == calls
    assert len(b.view()["rules"]) == 2


def test_12_partial_pull_does_not_claim_caught_up_and_resumes(pair):
    from khipu import routing_replication as replication
    a, b, hub = pair
    a.activate()
    for words in ("one", "two", "three"):
        routing.add_rule(words, via="app")
    assert sync(a, hub)["ok"]
    first = sync(b, hub, limit=1, max_pages=1)
    assert not first["caught_up"]
    assert replication.status()["caught_up_at"] is None
    assert sync(b, hub, limit=1, max_pages=1)["caught_up"] is False
    assert sync(b, hub, limit=1, max_pages=1)["caught_up"] is True
    assert len(b.view()["rules"]) == 3
    assert replication.status()["sources"]


def test_12_overlap_finds_late_commit_behind_previous_cursor(pair):
    from khipu.hub_snapshot import FRESHNESS_OVERLAP_S
    a, b, hub = pair
    assert sync(b, hub)["ok"]
    a.activate()
    routing.add_rule("late commit", via="app")
    hub.now -= timedelta(seconds=FRESHNESS_OVERLAP_S // 2)
    assert sync(a, hub)["ok"]
    hub.now += timedelta(seconds=FRESHNESS_OVERLAP_S // 2 + 1)
    assert sync(b, hub)["ok"]
    assert [r["text"] for r in b.view()["rules"]] == ["late commit"]


def test_12_restore_rotates_mac_replays_old_local_ids_and_rebuilds_remote(pair):
    from khipu import routing_replication as replication
    a, b, hub = pair
    a.activate()
    routing.add_rule("old local", via="app")
    assert sync(a, hub)["ok"]
    old_mac = routing._local_mac()
    b.activate()
    routing.add_rule("other Mac", via="app")
    assert sync(b, hub)["ok"]
    a.activate()
    original = routing.events_file().read_bytes()
    replication.restore()
    assert routing._local_mac() != old_mac
    assert routing.events_file().read_bytes() == original
    assert sync(a, hub)["ok"]
    assert len(hub.rows) == 2
    assert len(a.view()["rules"]) == 2
    routing.add_rule("after restore", via="app")
    latest = routing.read_events()[0][-1]
    assert latest["mac"] != old_mac
    assert sync(a, hub)["ok"]
    assert len(hub.rows) == 3


def test_12_default_off_never_constructs_transport(pair, monkeypatch):
    from khipu import routing_replication as replication
    a, _, _ = pair
    a.activate()
    a.configure(routing_replication_live=False)
    def forbidden():
        pytest.fail("disabled replication attempted a hub connection")
    monkeypatch.setattr(replication, "PostgresHub", forbidden)
    assert replication.sync()["disabled"]
    routing.poll_refold()


def test_12_missing_and_path_ids_are_skipped_before_upload(pair):
    a, _, hub = pair
    a.activate()
    routing.add_rule("valid", via="app")
    with routing.events_file().open("a") as stream:
        for ident in (None, "/private/synthetic/event"):
            row = dict(type="note_set", text="SENTINEL", written_at=routing._iso(T0), seq=10)
            if ident is not None:
                row["id"] = ident
            stream.write(json.dumps(row) + "\n")
    assert sync(a, hub)["ok"]
    assert len(hub.rows) == 1
    assert all(p.get("id") and "/" not in p["id"] for p in hub.calls)


def test_12_offline_age_and_skew_use_hub_clock_and_keep_local_writes(pair):
    from khipu import routing_replication as replication
    a, _, hub = pair
    a.advance(3600)
    assert sync(a, hub)["ok"]
    assert replication.status()["age_seconds"] < 1
    caught = replication.status()["caught_up_at"]
    hub.offline = True
    a.advance(601)
    result = sync(a, hub)
    assert not result["ok"] and "SENTINEL" not in json.dumps(result)
    assert replication.status()["caught_up_at"] == caught
    assert replication.status()["stale"]
    routing.add_rule("offline", via="app")
    assert "routing from other Macs is 10 min old" in routing.summary(now=a.now)["text"]
    out = routing.prompt_push(None, budget=1, now=a.now)
    assert "routing from other Macs is 10 min old" in out["text"]


@pytest.mark.parametrize("addition", [False, True])
def test_4_text_off_placeholder_and_confirmed_words_cross_macs(pair, addition):
    a, b, hub = pair
    a.activate()
    owner = routing.add_rule("owner words", via="app")
    sync(a, hub)
    if addition:
        proposal = routing.request_rule("proposed private words", author=AUTHOR)
        entity = proposal["entity"]
    else:
        shown = routing.conversation_view()
        rule = next(r for r in shown["rules"] if r["entity"] == owner["entity"])
        routing.request_rule("proposed private words", n=rule["n"], base=rule["owner_revision"], author=AUTHOR)
        entity = owner["entity"]
    assert sync(a, hub)["ok"]
    assert "proposed private words" not in json.dumps(hub.calls)
    assert sync(b, hub)["ok"]
    remote = next(r for r in b.view()["rules"] if r["entity"] == entity)
    assert remote["pending"] and "pending rule proposal" in remote["text"]
    with pytest.raises(routing.RouteError, match="source Mac"):
        routing.confirm_rule(entity, via="app")
    a.activate()
    routing.confirm_rule(entity, via="app", expect=next(r["revision"] for r in routing.read_view()["rules"] if r["entity"] == entity))
    assert sync(a, hub)["ok"]
    assert sync(b, hub)["ok"]
    remote = next(r for r in b.view()["rules"] if r["entity"] == entity)
    assert remote["confirmed"] and remote["text"] == "proposed private words"


def test_12_observation_renewal_silence_expiry_and_machine_scope(pair):
    a, b, hub = pair
    a.activate()
    with routing._lock():
        mac_a = routing._mac_locked()
    b.activate()
    with routing._lock():
        mac_b = routing._mac_locked()
    a.activate()
    a.write(a.event("observation", 1, account="codex|", state="limited", source="probe",
                    scope="all", witness_at=routing._iso(T0), until=routing._iso(T0 + timedelta(seconds=60)),
                    expires_at=routing._iso(T0 + timedelta(seconds=60)), mac=mac_a))
    sync(a, hub)
    b.activate()
    b.write(b.event("observation", 2, account="codex|", state="down", source="probe",
                    reason="not_installed", observation_scope={"kind": "machine", "mac": mac_b},
                    expires_at=routing._iso(T0 + timedelta(hours=1)), mac=mac_b))
    sync(b, hub)
    sync(a, hub)
    a.advance(61)
    a.write(a.event("observation", 3, account="codex|", state="open", source="runner",
                    scope="all", witness_at=routing._iso(a.now),
                    expires_at=routing._iso(T0 + timedelta(seconds=151)), mac=mac_a))
    a.write(a.event("observation_renew", 4, target="e3", witness_at=routing._iso(T0 + timedelta(seconds=90)),
                    expires_at=routing._iso(T0 + timedelta(seconds=180)), mac=mac_a))
    a.write(a.event("source_seen", 5, source="probe", witness_at=routing._iso(T0 + timedelta(seconds=100)), mac=mac_a))
    hub.now += timedelta(seconds=100)
    a.now = b.now = hub.now
    sync(a, hub)
    sync(b, hub)
    for mac in (a, b):
        mac.now = T0 + timedelta(seconds=150)
        mac.activate()
        account = next(x for x in mac.view()["accounts"] if x["key"] == "codex|")
        assert account["state"] == ("open" if mac is a else "down")
    a.advance(31)
    a.activate()
    assert next(x for x in a.view()["accounts"] if x["key"] == "codex|")["state"] == "unknown"


def test_generated_identity_survives_projection_but_paths_do_not(pair):
    a, _, _ = pair
    a.activate()
    routing.add_rule("synthetic", via="app")
    local = routing._read_event_lines()[0][0]
    payload = routing.replication_payload([local], bindings={})[0]
    for key in ("id", "mac", "entity", "op", "hlc", "base"):
        assert payload[key] == local[key]
    damaged = dict(local, id="/private/synthetic/id", mac="/private/synthetic/mac")
    projected = routing.replication_payload([damaged], bindings={})[0]
    assert "id" not in projected and "mac" not in projected


def test_12_skewed_mac_expires_the_same_shared_observation(pair):
    a, b, hub = pair
    a.activate()
    a.write(a.event("observation", 1, account="codex|", state="open", source="probe",
                    scope="all", witness_at=routing._iso(T0),
                    expires_at=routing._iso(T0 + timedelta(seconds=60))))
    sync(a, hub)
    b.advance(600)
    sync(b, hub)
    for mac in (a, b):
        mac.activate()
        assert next(x for x in mac.view()["accounts"] if x["key"] == "codex|")["state"] == "open"
        mac.advance(61)
        assert next(x for x in mac.view()["accounts"] if x["key"] == "codex|")["state"] == "unknown"


def test_12_source_silence_is_mac_scoped_and_replicates_without_renewal(pair):
    a, b, hub = pair
    a.activate()
    a.write(a.event("observation", 1, account="codex|", state="open", source="runner", runner="synthetic",
                    scope="all", witness_at=routing._iso(T0),
                    expires_at=routing._iso(T0 + timedelta(hours=1))))
    a.write(a.event("source_seen", 2, runner="synthetic", witness_at=routing._iso(T0),
                    status=json.dumps({"silent": True, "unreadable": False, "lanes": []})))
    assert sync(a, hub)["ok"]
    assert sync(b, hub)["ok"]
    for mac in (a, b):
        mac.activate()
        row = next(x for x in mac.view()["accounts"] if x["key"] == "codex|")
        assert row["state"] == "unknown" and row["silent"]
        assert not any(e["type"] == "observation_renew" for e in routing.read_events()[0])


def test_12_download_failure_keeps_cursor_and_replays_durable_page(pair, monkeypatch):
    from khipu import routing_replication as replication
    a, b, hub = pair
    a.activate()
    routing.add_rule("one", via="app")
    sync(a, hub)
    b.activate()
    original = replication._save
    def fail(state):
        raise OSError("SYNTHETIC disk full")
    monkeypatch.setattr(replication, "_save", fail)
    assert not replication.sync(hub=hub)["ok"]
    assert replication.status()["caught_up_at"] is None
    monkeypatch.setattr(replication, "_save", original)
    assert replication.sync(hub=hub)["ok"]
    assert len(b.view()["rules"]) == 1


def test_4_priority_text_off_converges_after_source_confirmation(pair):
    a, b, hub = pair
    a.activate()
    shown = routing.conversation_view()
    routing.request_priority([{"account": "Codex", "role": "SYNTHETIC private proposal"}],
                             base=shown["priority_owner_revision"], owner_request="SYNTHETIC request", author=AUTHOR)
    assert sync(a, hub)["ok"]
    assert "SYNTHETIC private proposal" not in json.dumps(hub.calls)
    assert sync(b, hub)["ok"]
    assert b.view()["priority"]["pending"]
    assert "pending priority proposal" in b.view()["priority"]["entries"][0]["role"]
    a.activate()
    routing.confirm_priority(via="app", expect=routing.priority_version())
    assert sync(a, hub)["ok"]
    assert sync(b, hub)["ok"]
    assert not b.view()["priority"]["pending"]
    assert b.view()["priority"]["entries"][0]["role"] == "SYNTHETIC private proposal"


def test_poll_drives_replication_before_fold_and_caps_interval(pair, monkeypatch):
    from khipu import routing_replication as replication
    a, b, hub = pair
    a.activate()
    routing.add_rule("poll delivery", via="app")
    real = replication.sync
    monkeypatch.setattr(replication, "sync", lambda: real(hub=hub))
    a.configure(routing_poll_interval=120)
    assert routing.poll_interval() <= 30
    routing.poll_refold()
    b.activate()
    routing.poll_refold()
    assert [r["text"] for r in b.view()["rules"]] == ["poll delivery"]


def test_unprojected_kinds_are_reported_and_retried_after_projection_upgrade(pair, monkeypatch):
    a, _, hub = pair
    a.activate()
    a.write(a.event("usage_sample", 1, account="codex|", window="weekly", used_pct=1))
    routing.add_rule("still ships", via="app")
    result = sync(a, hub)
    assert result["deferred_kinds"] == ["usage_sample"]
    assert len(hub.rows) == 1
    monkeypatch.setitem(routing._REPLICATION_FIELDS, "usage_sample", {"account": "account"})
    assert sync(a, hub)["deferred_kinds"] == []
    assert len(hub.rows) == 2


def test_completed_old_scan_reports_its_actual_age(pair):
    from khipu import routing_replication as replication
    a, b, hub = pair
    a.activate()
    routing.add_rule("one", via="app")
    routing.add_rule("two", via="app")
    sync(a, hub)
    assert not sync(b, hub, limit=1, max_pages=1)["caught_up"]
    b.advance(700)
    hub.now += timedelta(seconds=700)
    assert sync(b, hub, limit=1, max_pages=1)["caught_up"]
    assert replication.status()["age_seconds"] == 700
    assert replication.status()["stale"]


def test_postgres_transport_uses_primary_key_and_preserves_microsecond_cursor(pair, monkeypatch):
    from khipu import db, routing_replication as replication
    a, _, _ = pair
    a.activate()
    routing.add_rule("synthetic", via="app")
    payload = routing.replication_payload(routing._read_event_lines()[0], bindings={})[0]
    stamp = T0.replace(microsecond=123456)
    statements = []
    committed = []
    stored = []

    class Cursor:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def execute(self, sql, params=()):
            statements.append((sql, params))
            self.sql = sql
            if sql.startswith("INSERT INTO routing_events"):
                stored.append((payload["id"], 19, json.loads(params[2]), stamp, params[3]))
        def fetchone(self):
            if self.sql == "SELECT clock_timestamp()":
                return (stamp,)
            if self.sql.startswith("SELECT greatest"):
                return (3,)
            return stored[0] if stored else None
        def fetchall(self):
            if "FROM routing_events" in self.sql:
                return list(stored)
            return [("synthetic", 0, None, 0, stamp)]

    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            committed.append(True)
        def cursor(self):
            return Cursor()

    monkeypatch.setattr(db, "connect", lambda **kw: Connection())
    hub = replication.PostgresHub()
    hub.reserve_numbers("restored", 3)
    assert committed == [True]
    committed.clear()
    receipt = hub.upload("restored", payload)
    assert committed == [True]
    assert receipt["received_at"].endswith(".123456Z")
    assert receipt["rule_number"] == 4
    assert receipt["payload"] == payload
    assert hub.upload("restored", payload) == receipt
    assert len(stored) == 1
    assert any("greatest(routing_sources.rule_number_floor" in sql for sql, _ in statements)
    assert any("ON CONFLICT (event_id) DO NOTHING" in sql for sql, _ in statements)
    assert any("coalesce(max(hub_order), 0)" in sql for sql, _ in statements)
    assert not any("ON CONFLICT (hub_order)" in sql for sql, _ in statements)
    after = (receipt["received_at"], receipt["event_id"])
    result = hub.pull(after, None, 1)
    assert result["hub_now"].endswith(".123456Z")
    query, params = next((sql, params) for sql, params in statements if "ORDER BY received_at" in sql)
    assert "received_at, event_id" in query and params[:2] == after


def test_confirmation_of_withheld_priority_is_refused(pair):
    a, b, hub = pair
    a.activate()
    shown = routing.conversation_view()
    routing.request_priority([{"account": "Codex", "role": "synthetic role"}],
                             base=shown["priority_owner_revision"], owner_request="synthetic request", author=AUTHOR)
    sync(a, hub)
    sync(b, hub)
    with pytest.raises(routing.RouteError, match="source Mac"):
        routing.confirm_priority(via="app", expect=routing.priority_version())


def test_no_change_push_still_reports_replica_age(pair):
    a, _, hub = pair
    sync(a, hub)
    out = routing._replication_push_age({"kind": "none", "text": None}, a.now)
    assert out["kind"] == "changes"
    assert "routing from other Macs is 0 min old" in out["text"]


def test_confirmed_words_and_proposal_identity_share_one_locked_read(pair, monkeypatch):
    import threading

    a, _, _ = pair
    a.activate()
    added = routing.request_rule("first proposal", author=AUTHOR)
    shown = routing.conversation_view()
    rule = next(r for r in shown["rules"] if r["entity"] == added["entity"])
    original_rule = routing._rule
    done = threading.Event()
    errors = []
    results = []

    def replace():
        try:
            results.append(routing.request_rule("second proposal", n=rule["entity"],
                                                base=rule["owner_revision"], author=AUTHOR))
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=replace)

    def read_then_race(*args, **kw):
        current = original_rule(*args, **kw)
        if threading.current_thread() is threading.main_thread():
            worker.start()
            done.wait(0.2)
        return current

    monkeypatch.setattr(routing, "_rule", read_then_race)
    routing.confirm_rule(rule["entity"], via="app", expect=rule["revision"])
    worker.join(5)
    assert not worker.is_alive() and not errors
    view = a.view()
    assert view["rules"][0]["text"] == "first proposal"
    second_event = next(e for e in routing.read_events()[0] if e["id"] == results[0]["id"])
    assert view["proposals"][second_event["proposal"]]["status"] == "crossed"
