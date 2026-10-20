"""Synthetic regressions adapted from the slice 3a Opus probes."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from khipu import routing, accounts, config, usage, mcp_server

T0 = datetime(2026, 10, 20, 14, tzinfo=timezone.utc)
AUTHOR = {"kind": "conversation", "harness": "codex", "project": "synthetic", "session": "s1"}


@pytest.fixture
def mac(tmp_path, monkeypatch):
    for n in ("KHIPU_ISOLATED_DIR", "ROUTING_NOW_OFF", "KHIPU_SESSION_ID", "KHIPU_HARNESS", "CLAUDECODE",
              "CODEX_HOME", "CLAUDE_CONFIG_DIR", mcp_server.GATEWAY_ACTIVE_ENV):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setattr(mcp_server, "_GATEWAY_ACTIVE", False)
    monkeypatch.setattr(routing, "ROUTING_NOW_DISABLE_FILE", str(tmp_path / "disabled"))
    monkeypatch.setattr(routing, "kick_plan_refresh", lambda: False)
    monkeypatch.setattr(usage, "_command", lambda h: None)
    root = tmp_path / "home"
    data = root / "data"
    data.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(root / "capture"))
    clock = {"now": T0}
    monkeypatch.setattr(routing, "_now", lambda: clock["now"])
    class M:
        pass
    m = M(); m.root, m.data, m.clock, m.patch = root, data, clock, monkeypatch
    def cfg(**v):
        p = data / "config.json"
        c = json.loads(p.read_text()) if p.exists() else {"routing_reset_check": False}
        c.update(v); p.write_text(json.dumps(c))
    m.cfg = cfg
    def adv(s): clock["now"] += timedelta(seconds=s)
    m.adv = adv
    return m


CONV = {"by": AUTHOR, "request": "synthetic"}


def _setup(mac, entries):
    for e in entries:
        if e.get("home"):
            (mac.root / e["home"].removeprefix("~/")).mkdir(exist_ok=True)
    mac.cfg(usage_accounts=entries)


# ---- the brief's P1 question: can a login folder's basename reach the id? ----

def test_P_folder_basename_never_reaches_the_stable_id(mac):
    """track() names an unlabelled login from its folder (discover.default_name)."""
    _setup(mac, [{"label": "Claude", "harness": "claude"}])
    (mac.root / ".claude-zqprivate").mkdir()
    out = accounts.track([{"harness": "claude", "home": "~/.claude-zqprivate"}],
                         expect=config.accounts_version(), read=False, via="app")
    key = out["accounts"][0]["key"]
    ident = routing.account_id(key)
    row = usage.public_row({"harness": "claude", "home": "~/.claude-zqprivate", "account": out["accounts"][0]["label"]})
    print("label:", out["accounts"][0]["label"], "id:", ident, "usage id:", row["id"])
    assert "zqprivate" not in ident, f"folder basename reached the stable id: {ident}"


def test_P_owner_rename_cannot_scrub_a_folder_derived_id_from_conversation_output(mac):
    _setup(mac, [{"label": "Claude", "harness": "claude"}])
    (mac.root / ".claude-zqprivate").mkdir()
    out = accounts.track([{"harness": "claude", "home": "~/.claude-zqprivate"}],
                         expect=config.accounts_version(), read=False, via="app")
    key = out["accounts"][0]["key"]
    accounts.rename(key, "Claude work", expect=config.accounts_version(), via="app")
    assert routing.account_id(key) == "claude:work"
    conv = json.dumps(routing.conversation_view(now=mac.clock["now"]))
    mcp = json.dumps(mcp_server.TOOL_FUNCS["khipu_route"]({}))
    leaks = [s for s in (conv + mcp).split('"') if "zqprivate" in s]
    assert not leaks, f"renamed-away folder name still shown to conversations: {sorted(set(leaks))}"


# ---- (2) a conversation rebinding an account id to another login ----

def test_P_conversation_cannot_rebind_a_stable_id_to_another_login(mac):
    _setup(mac, [{"label": "Codex", "harness": "codex"},
                 {"label": "Codex work", "harness": "codex", "home": "~/.codex-a"}])
    (mac.root / ".codex-b").mkdir()
    routing.account_id("codex|")  # this Mac issued salted ids
    a_key = next(a["key"] for a in routing.accounts() if a["label"] == "Codex work")
    old = routing._legacy_account_id(a_key)
    routing.migrate_accounts()
    routing.report(account="Codex work", state="limited", until="30h", author=AUTHOR)
    routing.pause("Codex work", via="app")
    assert routing.resolve_account(old) == "codex:work"
    # A conversation stops login A and tracks login B under the same label.
    changed = [{"label": "Codex", "harness": "codex"},
               {"label": "Codex work", "harness": "codex", "home": "~/.codex-b"}]
    accounts.save_list(changed, expect=config.accounts_version(), request=CONV)
    b_key = next(a["key"] for a in routing.accounts() if a["label"] == "Codex work")
    view = routing.read_view(now=mac.clock["now"])
    state = {a["key"]: a["state"] for a in view["accounts"]}
    print("CONV old alias -> B:", routing._resolve_account_key(old) == b_key, "B state:", state.get(b_key),
          "pending request:", isinstance(config.load_config().get("usage_accounts_request"), dict))
    assert routing.account_id(b_key) != "codex:work"
    assert routing._resolve_account_key(old) == a_key
    assert state[b_key] == "unknown"
    accounts.answer_request("confirm", expect=config.accounts_version(), via="app")
    assert next(a for a in routing.read_view(now=mac.clock["now"])["accounts"] if a["key"] == b_key)["state"] == "unknown"
    assert routing._resolve_account_key(old) == a_key


def test_P_owner_readd_of_a_deleted_label_inherits_the_old_logins_history(mac):
    _setup(mac, [{"label": "Codex", "harness": "codex"},
                 {"label": "Codex work", "harness": "codex", "home": "~/.codex-a"}])
    (mac.root / ".codex-b").mkdir()
    a_key = next(a["key"] for a in routing.accounts() if a["label"] == "Codex work")
    old = routing._legacy_account_id(a_key)
    routing.migrate_accounts()
    routing.report(account="Codex work", state="limited", until="30h", author=AUTHOR)
    mac.adv(60)
    # Owner stops A, later tracks unrelated B with the same name.
    accounts.save_list([{"label": "Codex", "harness": "codex"}], expect=config.accounts_version(), via="app")
    mac.adv(60)
    accounts.save_list([{"label": "Codex", "harness": "codex"},
                        {"label": "Codex work", "harness": "codex", "home": "~/.codex-b"}],
                       expect=config.accounts_version(), via="app")
    b_key = next(a["key"] for a in routing.accounts() if a["label"] == "Codex work")
    view = routing.read_view(now=mac.clock["now"])
    state = {a["key"]: a["state"] for a in view["accounts"]}
    print("B state:", state.get(b_key), "old alias -> B:", routing._resolve_account_key(old) == b_key)
    assert state.get(b_key) != "limited", "a brand-new login inherited a stopped login's limit"
    assert routing.account_id(b_key) != "codex:work"
    assert routing._resolve_account_key(old) == a_key


# ---- (4) interrupt and re-run ----

def test_P_migration_interrupted_after_append_then_rerun(mac, monkeypatch):
    _setup(mac, [{"label": "Codex", "harness": "codex"}, {"label": "Grok", "harness": "grok"}])
    routing.account_id("codex|")
    from khipu import config as cfgmod
    orig = cfgmod.mutate_config
    calls = {"n": 0}
    import contextlib
    @contextlib.contextmanager
    def boom():
        calls["n"] += 1
        raise KeyboardInterrupt
        yield
    monkeypatch.setattr(cfgmod, "mutate_config", boom)
    with pytest.raises(KeyboardInterrupt):
        routing.migrate_accounts()
    monkeypatch.setattr(cfgmod, "mutate_config", orig)
    first = [e for e in routing.read_events()[0] if e["type"] == "account_migration"]
    mac.adv(86400)
    routing.migrate_accounts(); routing.migrate_accounts()
    again = [e for e in routing.read_events()[0] if e["type"] == "account_migration"]
    assert len(first) == len(again) == 1 and first[0]["started_at"] == again[0]["started_at"]
    assert config.load_config()[routing.account_identity.BINDINGS]["codex:main"]["harness"] == "codex"


def test_P_conversation_reads_do_not_write_config_each_time(mac):
    _setup(mac, [{"label": "Codex", "harness": "codex"}])
    routing.account_id("codex|")
    routing.read_view(now=mac.clock["now"])
    p = mac.data / "config.json"
    before = (p.read_bytes(), p.stat().st_mtime_ns)
    ev = routing.events_file(); n = len(ev.read_bytes()) if ev.exists() else 0
    for _ in range(3):
        mac.adv(5); routing.read_view(now=mac.clock["now"])
    assert (p.read_bytes(), p.stat().st_mtime_ns) == before
    assert (len(ev.read_bytes()) if ev.exists() else 0) == n


# ---- legacy slug collisions ----

def test_P_every_id_khipu_route_lists_resolves_back(mac):
    """Legacy configs may hold labels that differ but share a slug."""
    _setup(mac, [{"label": "Codex work", "harness": "codex", "home": "~/.codex-a"},
                 {"label": "Codex-work", "harness": "codex", "home": "~/.codex-b"},
                 {"label": "Grok", "harness": "grok"}])
    routing.account_id("grok|")
    conv = routing.conversation_view(now=mac.clock["now"])
    bad = {}
    for a in conv["accounts"]:
        try:
            routing._resolve_account_key(a["id"])
        except routing.RouteError as e:
            bad[a["id"]] = str(e)
    print(bad)
    assert not bad, f"ids listed to conversations that cannot be passed back: {bad}"




def test_identity_log_walk_is_cached_and_invalidated(mac, monkeypatch):
    _setup(mac, [{"label": "Codex", "harness": "codex"}, {"label": "Grok", "harness": "grok"}])
    path = routing.events_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"type": "evidence", "account": "codex|", "state": "open", "source": "report",
           "written_at": routing._iso(T0), "written_by": AUTHOR}
    path.write_text((json.dumps(row) + "\n") * 20000)
    real_state = routing.account_identity.state
    walks = []
    def counted_state(events, known):
        walks.append(True)
        return real_state(events, known)
    monkeypatch.setattr(routing.account_identity, "state", counted_state)
    real = routing._read_identity_events
    calls = []
    def counted():
        calls.append(True)
        return real()
    monkeypatch.setattr(routing, "_read_identity_events", counted)
    for _ in range(3):
        assert routing.account_id("codex|") == "codex:main"
        assert routing.account_id("grok|") == "grok:main"
    assert len(calls) == len(walks) == 1
    path = routing.events_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "account_rename", "account": "codex:main", "to": "codex:second",
                               "written_at": routing._iso(T0), "written_by": {"kind": "owner"}}) + "\n")
    assert routing.account_id("codex|") == "codex:second"
    assert len(calls) == len(walks) == 2
    assert routing.account_id("grok|") == "grok:main"
    assert len(calls) == len(walks) == 2


def test_folder_derived_stable_alias_survives_migration_and_owner_rename(mac):
    _setup(mac, [{"label": "Claude zqprivate", "harness": "claude", "home": "~/.claude-zqprivate"}])
    key = routing.accounts()[0]["key"]
    old = "claude:zqprivate"
    current = routing.account_id(key)
    assert current == "claude:account-1"
    assert routing.resolve_account(old) == current
    routing.migrate_accounts()
    accounts.rename(key, "Claude work", expect=config.accounts_version(), via="app")
    assert routing.resolve_account(old) == routing.resolve_account(current) == "claude:work"
    assert "zqprivate" not in json.dumps(routing.conversation_view(now=mac.clock["now"]))


def test_owner_move_preserves_id_alias_and_pause(mac):
    _setup(mac, [{"label": "Codex work", "harness": "codex", "home": "~/.codex-a"}])
    (mac.root / ".codex-b").mkdir()
    old_key = routing.accounts()[0]["key"]
    alias = routing._legacy_account_id(old_key)
    routing.migrate_accounts()
    routing.pause("Codex work", via="app")
    accounts.move(old_key, "~/.codex-b", expect=config.accounts_version(), via="app")
    new_key = routing.accounts()[0]["key"]
    assert routing.account_id(new_key) == "codex:work"
    assert routing._resolve_account_key(alias) == new_key
    assert next(a for a in routing.read_view(now=mac.clock["now"])["accounts"] if a["key"] == new_key)["state"] == "paused"


def test_historical_listed_ids_roundtrip_even_after_alias_expiry(mac):
    _setup(mac, [{"label": "Grok", "harness": "grok"}])
    path = routing.events_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "evidence", "account": "codex|/synthetic/old-login",
                               "state": "limited", "until": routing._iso(T0 + timedelta(days=100)),
                               "source": "report", "written_at": routing._iso(T0), "written_by": AUTHOR}) + "\n")
    routing._legacy_account_id("grok|")
    routing.migrate_accounts()
    mac.adv(91 * 86400)
    for row in routing.conversation_view(now=mac.clock["now"])["accounts"]:
        assert routing.account_id(routing._resolve_account_key(row["id"])) == row["id"]
    with pytest.raises(routing.RouteError, match="doesn't know that old account id"):
        routing.resolve_account("acct-000000000000")


def test_identity_reader_keeps_mixed_sequence_and_transaction_order(mac):
    _setup(mac, [{"label": "Codex", "harness": "codex"}])
    rows = [
        {"type": "account_rename", "account": "codex:main", "to": "codex:second", "seq": 3},
        {"type": "evidence", "account": "codex|", "state": "open", "seq": 1},
        {"type": "account_rename", "account": "codex:second", "to": "codex:third"},
        {"type": "account_txn", "id": "t-abort", "state": "begin"},
        {"type": "account_rename", "account": "codex:third", "to": "codex:wrong", "txn": "t-abort"},
        {"type": "account_txn", "id": "t-abort", "state": "abort"},
    ]
    path = routing.events_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(dict(row, written_at=routing._iso(T0), written_by={"kind": "owner"})) + "\n" for row in rows))
    known = routing.accounts()
    expected = routing.account_identity.state(routing._in_write_order(routing.read_events()[0]), known)
    assert routing._identity_snapshot(known)[0] == expected


def test_owner_rename_cannot_merge_into_another_logins_opaque_id(mac):
    _setup(mac, [{"label": "Claude zqprivate", "harness": "claude", "home": "~/.claude-zqprivate"},
                 {"label": "Claude other", "harness": "claude", "home": "~/.claude-b"}])
    other = next(a["key"] for a in routing.accounts() if a["label"] == "Claude other")
    with pytest.raises(usage.AccountsError, match="another account's stable id"):
        accounts.rename(other, "Claude account 1", expect=config.accounts_version(), via="app")
