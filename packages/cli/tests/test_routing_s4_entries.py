"""Entry-model contracts; every file and source is disposable."""
import json
from datetime import timedelta

import pytest

from khipu import agent_view
from khipu import accounts, owner_direction, routing, routing_entries
from tests.test_routing_acceptance import AUTHOR, FIXTURES, LEGACY, T0

pytest_plugins = ("tests.test_routing_acceptance",)


def rows():
    events, bad = routing.read_events()
    assert bad == 0
    return events


def codex(view):
    return next(a for a in view["accounts"] if a["harness"] == "codex")


def test_old_format_log_matches_pre_slice_effective_snapshot_without_rewriting(replay):
    path = routing.events_file()
    path.parent.mkdir(parents=True)
    original = LEGACY.read_bytes()
    path.write_bytes(original)
    expected = json.loads((FIXTURES / "entry_legacy_effective.json").read_text())
    assert replay.view()["effective"] == expected
    replay.advance(1)
    assert replay.view()["effective"] == expected
    assert path.read_bytes() == original


def test_entry_kinds_and_owner_words_survive_append_and_fold_byte_identically(replay):
    words = "  Codex rôle — keep\n  exactly\tthese spaces.  "
    routing.add_rule(words, via="app")
    routing.set_note(words, via="app")
    routing.set_priority([{"account": "Codex", "role": words}], via="app", expect=routing.priority_version())
    routing.report(account="Codex", state="open", author=AUTHOR)
    events = rows()
    assert [e["kind"] for e in events] == ["owner_rule", "owner_rule", "fact", "observation"]
    view = replay.view()
    assert view["rules"][0]["text"].encode() == words.encode()
    assert view["note"]["text"].encode() == words.encode()
    assert view["priority"]["entries"][0]["role"].encode() == words.encode()
    assert events[-1]["witness_at"] == routing._iso(T0)
    assert events[-1]["expires_at"] == routing._iso(T0 + timedelta(hours=12))


def test_cached_reading_cannot_renew_and_new_witness_is_quarter_throttled(replay):
    replay.configure(routing_stale_after=400)
    reset = T0 + timedelta(days=6)
    replay.usage(reset=reset)
    replay.view()
    first = rows()
    assert [e["type"] for e in first] == ["observation"]
    assert first[0]["account"] == "codex:main"
    replay.advance(99)
    replay.usage(reset=reset)
    replay.view()
    assert rows() == first
    replay.advance(1)
    replay.usage(reset=reset)
    renewed = replay.view()
    assert [e["type"] for e in rows()] == ["observation", "observation_renew"]
    assert rows()[1]["target"] == first[0]["id"]
    assert codex(renewed)["witness"]["at"] == routing._iso(replay.now)
    assert rows()[1]["expires_at"] == routing._iso(T0 + timedelta(seconds=500))
    replay.advance(400)
    assert codex(replay.view())["state"] == "unknown"
    assert len(rows()) == 2


def test_state_transition_is_immediate_and_cache_failure_never_freshens_it(replay):
    replay.configure(routing_stale_after=400)
    reset = T0 + timedelta(hours=1)
    replay.usage(reset=reset)
    replay.view()
    replay.advance(1)
    replay.usage(100, reset=reset)
    assert codex(replay.view())["state"] == "limited"
    assert [e["type"] for e in rows()] == ["observation", "observation"]
    replay.advance(1)
    replay.usage(100, witness=T0 + timedelta(seconds=1), reset=reset, ok=False,
                 last_good_at=routing._iso(T0 + timedelta(seconds=1)),
                 probed_at=routing._iso(replay.now), error="timeout")
    replay.view()
    assert len(rows()) == 2


def test_failed_probe_without_observed_at_keeps_last_good_witness(replay):
    from khipu import routing_sources

    replay.usage(witness=T0, ok=False, error="timeout", observed_at=None,
                 probed_at=routing._iso(T0 + timedelta(hours=3)))
    evidence, _status = routing_sources.usage_evidence({"codex|"})
    assert evidence[0]["at"] == T0


def test_written_expiry_is_independent_of_reader_staleness_setting(replay):
    replay.configure(routing_stale_after=100)
    routing.report(account="Codex", state="open", author=AUTHOR)
    replay.configure(routing_stale_after=10000)
    replay.advance(100)
    assert codex(replay.view())["state"] == "unknown"


@pytest.mark.parametrize("local", [False, True])
def test_quota_travels_but_machine_observation_and_hold_stay_on_their_mac(replay, local):
    mac = "a" * 32
    quota = replay.event("observation", 1, mac=mac, state="limited", account="codex|", source="probe",
                         witness_at=routing._iso(T0), until=routing._iso(T0 + timedelta(hours=2)),
                         expires_at=routing._iso(T0 + timedelta(hours=2)), observation_scope={"kind": "account_quota"})
    down = replay.event("observation", 2, mac=mac, state="down", reason="not_installed", account="codex|",
                        source="probe", witness_at=routing._iso(T0 + timedelta(seconds=2)),
                        expires_at=routing._iso(T0 + timedelta(hours=1)),
                        observation_scope={"kind": "machine", "mac": mac})
    hold = replay.event("hold", 3, mac=mac, scope={"kind": "machine", "mac": mac}, text="busy")
    cfg = dict(routing.settings(replay.config), _mac=mac if local else "b" * 32)
    view = routing.fold([dict(e, _seq=i) for i, e in enumerate([quota, down, hold])], cfg=cfg,
                        known=routing.accounts(), now=T0 + timedelta(seconds=4))
    assert codex(view)["state"] == ("down" if local else "limited")
    assert bool(view["holds"]) is local
    assert view["machine_holds"][0]["mac"] == mac
    assert mac in view["machine_holds"][0]["scope_label"]


def test_renewal_before_base_newest_wins_but_another_source_cannot_renew(replay):
    mac = "a" * 32
    base = replay.event("observation", 1, mac=mac, state="open", source="probe", account="codex|",
                        witness_at=routing._iso(T0), expires_at=routing._iso(T0 + timedelta(seconds=100)))
    renew = replay.event("observation_renew", 2, mac=mac, target=base["id"],
                         witness_at=routing._iso(T0 + timedelta(seconds=90)),
                         expires_at=routing._iso(T0 + timedelta(seconds=190)))
    bad = dict(renew, id="bad", written_by=AUTHOR, witness_at=routing._iso(T0 + timedelta(seconds=180)),
               expires_at=routing._iso(T0 + timedelta(days=100)))
    replay.now = T0 + timedelta(seconds=150)
    view = replay.fold([bad, renew, base])
    assert codex(view)["state"] == "open"
    assert codex(view)["witness"]["at"] == renew["witness_at"]
    replay.now = T0 + timedelta(seconds=190)
    assert codex(replay.fold([bad, renew, base]))["state"] == "unknown"


def test_source_heartbeat_writes_on_availability_change_and_never_renews_account(replay, monkeypatch):
    replay.usage()
    replay.view()
    from khipu import routing_sources

    collect = routing_sources.collect
    def heartbeat(*args):
        evidence, status, cache = collect(*args)
        status["runners"] = [{"name": "runner", "heartbeat_at": routing._iso(replay.now),
                              "silent": False, "unreadable": False, "lanes": []}]
        return evidence, status, cache
    monkeypatch.setattr(routing_sources, "collect", heartbeat)
    with routing._lock():
        routing._refold_locked(replay.now)
    assert [e["type"] for e in rows()] == ["observation", "source_seen"]
    replay.advance(299)
    with routing._lock():
        routing._refold_locked(replay.now)
    assert len(rows()) == 2
    replay.advance(1)
    with routing._lock():
        routing._refold_locked(replay.now)
    assert [e["type"] for e in rows()] == ["observation", "source_seen"]
    assert rows()[-1]["status"] == '{"lanes":[],"silent":false,"unreadable":false}'
    replay.advance(3601)  # well beyond the old five-minute heartbeat writer throttle
    with routing._lock():
        live = routing._refold_locked(replay.now)
    assert [e["type"] for e in rows()] == ["observation", "source_seen"]
    runner = live["sources"]["runners"][0]
    assert runner["heartbeat_at"] == routing._iso(replay.now) and runner["silent"] is False

    def unavailable(*args):
        evidence, status, cache = heartbeat(*args)
        status["runners"][0]["unreadable"] = True
        return evidence, status, cache

    monkeypatch.setattr(routing_sources, "collect", unavailable)
    replay.advance(1)
    with routing._lock():
        routing._refold_locked(replay.now)
    assert [e["type"] for e in rows()] == ["observation", "source_seen", "source_seen"]
    assert rows()[-1]["status"] == '{"lanes":[],"silent":false,"unreadable":true}'
    assert not any(e["type"] == "observation_renew" for e in rows())


@pytest.mark.parametrize("choice", ["keep", "end"])
def test_rule_write_names_each_hold_and_pause_and_applies_choices_atomically(replay, choice):
    hold = routing.add_hold("  owner words  ", scope="account", account="Codex", reason="keep", owner=True, via="app")
    routing.pause("Codex", via="app")
    pause_id = codex(replay.view())["witness"]["id"]
    before = routing.events_file().read_bytes()
    with pytest.raises(routing.RouteError, match=pause_id):
        routing.add_rule("Codex builds", via="app", holds={hold["id"]: "keep"})
    assert routing.events_file().read_bytes() == before
    routing.add_rule("Codex builds", via="app", holds={hold["id"]: choice, pause_id: choice})
    view = replay.view()
    assert bool(view["holds"]) is (choice == "keep")
    assert (codex(view)["state"] == "paused") is (choice == "keep")
    assert rows()[-1]["holds"] == {hold["id"]: choice, pause_id: choice}


def test_level_is_public_expires_on_earlier_reset_and_is_retained_for_owner_undo(replay):
    reset = T0 + timedelta(hours=1)
    replay.usage(reset=reset)
    level = {"claim": "low", "as_of": routing._iso(T0), "until": routing._iso(T0 + timedelta(days=1))}
    routing.set_priority([{"account": "Codex", "role": "builder", "level": level}], via="app",
                         expect=routing.priority_version())
    held = replay.view()["priority"]
    expected = dict(level, until=routing._iso(reset))
    assert held["entries"][0]["level"] == expected
    assert owner_direction._priority_rows(held)[0]["level"] == expected
    assert replay.pull()["priority"]["entries"][0]["level"] == expected
    assert replay.view()["valid_until"] == routing._iso(reset)
    replay.now = reset
    assert "level" not in replay.view()["priority"]["entries"][0]
    assert rows()[-1]["entries"][0]["level"] == expected


@pytest.mark.parametrize("level", [{}, {"claim": "low"}, {"claim": "low", "as_of": "bad", "until": "bad"},
                                    {"claim": "low", "as_of": routing._iso(T0), "until": routing._iso(T0)}])
def test_invalid_level_is_refused_without_append(replay, level):
    with pytest.raises(routing.RouteError, match="level"):
        routing.set_priority([{"account": "Codex", "level": level}], via="app", expect=routing.priority_version())
    assert not routing.events_file().exists()


def test_owner_definition_facts_fold_without_config_and_lane_maps_are_mac_keyed(replay):
    provider = {"id": "codex", "label": "Codex", "family": "OpenAI"}
    account = {"id": "codex:main", "provider": "codex", "label": "Codex"}
    lane = {"runner": "runner", "lane": "build", "account": "codex:main"}
    for name, value in [("provider", provider), ("account", account), ("lane_map", lane)]:
        routing.set_definition(name, value, via="app")
    events = rows()
    mac = events[0]["mac"]
    positioned = [dict(e, _seq=i) for i, e in enumerate(events)]
    for local in (True, False):
        view = routing.fold(positioned, now=T0, cfg=routing.settings({}), known=[], mac=mac if local else "other")
        assert view["definitions"]["providers"] == {"codex": provider}
        assert view["definitions"]["accounts"] == {"codex:main": account}
        assert bool(view["definitions"]["lane_maps"]) is local
        assert codex(view)["label"] == "Codex"
    foreign = [dict(e, written_by=AUTHOR) for e in positioned]
    assert not any(replay.fold(foreign)["definitions"].values())
    with pytest.raises(routing.RouteError):
        routing.set_definition("account", dict(account, home="/private"), via="app")


def test_entry_shape_rejects_damaged_new_fields(replay):
    base = replay.event("observation", state="open", account="codex|")
    for fields in ({"kind": "owner_rule"}, {"expires_at": "bad"}, {"witness_at": []},
                   {"observation_scope": {"kind": "machine"}}, {"holds": {"h": "guess"}}):
        assert routing._checked(dict(base, **fields)) is None


def test_definition_facts_supply_current_labels_and_local_lane_resolution(replay):
    routing.set_definition("account", {"id": "codex:main", "provider": "codex", "label": "Builder"}, via="app")
    assert [a["label"] for a in routing.accounts() if a["harness"] == "codex"] == ["Builder"]
    view = replay.view()
    assert len([a for a in view["accounts"] if a["harness"] == "codex"]) == 1
    assert codex(view)["label"] == "Builder"
    routing.set_definition("lane_map", {"runner": "runner", "lane": "build", "account": "codex:main"}, via="app")
    assert routing._inputs()[1]["routing_lane_map"]["runner"]["build"] == "codex:main"
    assert replay.config.get("routing_lane_map") is None
    routing.report(runner="runner", lane="build", state="limited", author=AUTHOR)
    assert codex(replay.view())["state"] == "limited"


def test_persisting_sources_preserves_an_old_source_report_dismissal(replay):
    from khipu import routing_sources

    replay.usage()
    evidence, _ = routing_sources.usage_evidence({"codex|"})
    dismissal = replay.event("dismiss", target=evidence[0]["id"])
    path = routing.events_file()
    path.parent.mkdir(parents=True)
    original = json.dumps(dismissal) + "\n"
    path.write_text(original)
    expected = replay.fold([dismissal], extra=evidence)["effective"]
    assert replay.view()["effective"] == expected
    assert path.read_text().startswith(original)


def test_role_naming_a_different_held_account_requires_a_choice(replay):
    hold = routing.add_hold("wait", scope="account", account="Grok", reason="keep", author=AUTHOR)
    with pytest.raises(routing.RouteError, match=hold["id"]):
        routing.set_priority([{"account": "Codex", "role": "Grok also builds"}], via="app",
                             expect=routing.priority_version())
    routing.set_priority([{"account": "Codex", "role": "Grok also builds"}], via="app",
                         expect=routing.priority_version(), holds={hold["id"]: "keep"})
    assert replay.view()["holds"][0]["id"] == hold["id"]


def test_reset_check_uses_the_reading_time_instead_of_its_append_time(replay):
    replay.configure(routing_stale_after=400)
    row = replay.usage(witness=T0 - timedelta(seconds=399))
    evidence, _limits, headroom = routing._check_evidence("codex|", row)
    assert headroom and evidence[0]["witness_at"] == row["observed_at"]
    replay.write(*evidence)
    assert codex(replay.view())["witness"]["at"] == row["observed_at"]
    replay.advance(1)
    assert codex(replay.view())["state"] == "unknown"


def test_empty_wrong_type_hold_choices_are_refused_without_appending(replay):
    with pytest.raises(routing.RouteError, match="holds"):
        routing.set_priority([{"account": "Codex"}], via="app", expect=routing.priority_version(), holds=[])
    assert not routing.events_file().exists()


def test_same_state_newer_used_share_is_live_while_renewal_stays_throttled(replay):
    reset = T0 + timedelta(days=6)
    replay.usage(40, reset=reset)
    replay.view()
    replay.advance(4 * 60)
    replay.usage(96, reset=reset)
    account = codex(replay.view())
    assert "96" in account["witness"]["evidence"]
    assert [event["type"] for event in rows()] == ["observation"]


def test_renewal_expiry_cannot_outlive_the_base_lifetime(replay):
    base = replay.event("observation", 1, account="codex|", state="open", source="probe",
                        witness_at=routing._iso(T0), expires_at=routing._iso(T0 + timedelta(hours=12)))
    renewal = replay.event("observation_renew", 2, target="e1", witness_at=routing._iso(T0 + timedelta(seconds=1)),
                           expires_at=routing._iso(T0 + timedelta(days=365)))
    replay.now = T0 + timedelta(days=30)
    assert codex(replay.fold([base, renewal]))["state"] == "unknown"


def test_limited_renewal_cannot_slide_past_its_original_limit_end(replay):
    until = T0 + timedelta(hours=1)
    base = replay.event("observation", 1, account="codex|", state="limited", source="probe",
                        witness_at=routing._iso(T0), until=routing._iso(until), expires_at=routing._iso(until))
    renewal = replay.event("observation_renew", 2, target="e1", witness_at=routing._iso(T0 + timedelta(minutes=30)),
                           expires_at=routing._iso(T0 + timedelta(days=1)))
    renewal["hlc"] = {"wall": 1, "counter": 0, "mac": "synthetic"}
    resolved = routing_entries.observations([base, renewal], routing.settings(replay.config))
    assert resolved[0]["expires_at"] == routing._iso(until)
    replay.now = T0 + timedelta(minutes=61)
    assert codex(replay.fold([base, renewal]))["state"] != "limited"


def test_persisted_source_id_is_salted_and_depends_on_the_local_salt(replay, monkeypatch):
    from khipu import routing_sources

    replay.usage()
    replay.view()
    persisted = next(event for event in rows() if event["type"] == "observation")
    evidence, _status = routing_sources.usage_evidence({"codex|"})
    raw_source_id = evidence[0]["id"]
    assert persisted["source_id"] == routing_entries.source_id(raw_source_id)
    monkeypatch.setattr(routing, "_id_salt", lambda operations=False: b"a" * 32)
    first = routing_entries.source_id(raw_source_id)
    monkeypatch.setattr(routing, "_id_salt", lambda operations=False: b"b" * 32)
    assert routing_entries.source_id(raw_source_id) != first


def test_stop_tracking_keeps_a_priority_level_and_checks_its_remaining_entries(replay):
    level = {"claim": "low", "as_of": routing._iso(T0), "until": routing._iso(T0 + timedelta(days=1))}
    view = {"priority": {"entries": [{"account": "codex|", "label": "Codex", "role": "build", "level": level},
                                  {"account": "grok|", "label": "Grok", "role": "review"}]},
            "accounts": []}
    events, _done = accounts._stop_events(view, ["grok|"], keep_priority=False, keep_pause=False, end_holds=[])
    assert events[0]["entries"] == [{"account": "codex|", "label": "Codex", "role": "build", "until": None,
                                      "level": level}]
    routing.add_hold("wait", scope="account", account="Grok", reason="keep", owner=True, via="app")
    with pytest.raises(routing.RouteError, match="end or keep"):
        routing.account_txn_begin([{"type": "priority_set", "entries": [{"account": "codex|", "role": "Grok"}],
                                    "written_by": routing.OWNER, "via": "app"}], before="a", after="b")


def test_cached_definition_inputs_borrow_without_whole_log_work(replay, monkeypatch):
    path = routing.events_file()
    path.parent.mkdir(parents=True)
    rows = [replay.event("evidence", i, account="codex|", state="open", source="report") for i in range(1, 20_001)]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    view = replay.view()
    routing._entry_definitions()  # warm the unchanged event-stat cache after source writes
    calls = {"parse": 0, "fold": 0, "order": 0}
    read = routing._read_event_lines
    fold = routing.fold
    order = routing._in_write_order

    def counted_read(*args, **kwargs):
        calls["parse"] += 1
        return read(*args, **kwargs)

    def counted_fold(*args, **kwargs):
        calls["fold"] += 1
        return fold(*args, **kwargs)

    def counted_order(*args, **kwargs):
        calls["order"] += 1
        return order(*args, **kwargs)

    monkeypatch.setattr(routing, "_read_event_lines", counted_read)
    monkeypatch.setattr(routing, "fold", counted_fold)
    monkeypatch.setattr(routing, "_in_write_order", counted_order)
    assert replay.view()["version"] == view["version"]
    agent_view.prepare_routing_table(refresh=True)
    routing.prompt_push({"version": view["version"], "epoch": view["epoch"], "full_at": routing._iso(replay.now)},
                        budget=1.0, now=replay.now)
    routing.accounts()
    routing._inputs()
    assert calls == {"parse": 0, "fold": 0, "order": 0}

    replay.usage()
    routing._start_view(replay.now)
    assert calls["fold"] == 1

    routing.set_definition("account", {"id": "codex:main", "provider": "codex", "label": "Builder"}, via="app")
    routing._inputs()  # one walk populates the definition overlay after the changed log.
    calls.update(parse=0, fold=0, order=0)
    assert next(account for account in routing.accounts() if account["harness"] == "codex")["label"] == "Builder"
    assert next(account for account in routing._inputs()[2] if account["harness"] == "codex")["label"] == "Builder"
    assert calls == {"parse": 0, "fold": 0, "order": 0}

    replay.configure(usage_accounts=[], routing_account_bindings={"codex:main": {"home": "/bound"}})
    changed = routing._inputs()[2]
    assert changed == [{"key": "codex|/bound", "home": "/bound", "label": "Builder", "harness": "codex",
                        "label_source": "owner", "definition_id": "codex:main"}]
    calls.update(parse=0, fold=0, order=0)
    assert routing._inputs()[2] == changed
    assert calls == {"parse": 0, "fold": 0, "order": 0}
