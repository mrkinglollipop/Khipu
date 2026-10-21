"""Opus round-2 probes, adapted to the orchestrator's round-4 ruling."""
import json
import re
import pytest
from khipu import routing, accounts, config, usage, discover
from tests import test_routing_s3a_review as review

mac = review.mac


def _mac_ids(mac, entries):
    for e in entries:
        if e.get("home"):
            (mac.root / e["home"].removeprefix("~/")).mkdir(exist_ok=True)
    mac.cfg(usage_accounts=entries)
    return {a["label"]: routing.account_id(a["key"]) for a in routing.accounts()}


def _second_mac(mac, tmp_path, monkeypatch, name):
    root = tmp_path / name
    data = root / "data"
    data.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    routing._IDENTITY_CACHE.clear()
    routing._MIGRATION_REVISION = None
    mac.root, mac.data = root, data
    p = data / "config.json"
    def cfg(**v):
        c = json.loads(p.read_text()) if p.exists() else {"routing_reset_check": False}
        c.update(v)
        p.write_text(json.dumps(c))
    mac.cfg = cfg


def test_R_matts_labels_stay_readable(mac):
    ids = _mac_ids(mac, [{"label": "Claude", "harness": "claude"},
                         {"label": "Claude second", "harness": "claude", "home": "~/.claude-t3-second"},
                         {"label": "Codex", "harness": "codex"}, {"label": "Grok", "harness": "grok"}])
    print("MATT", ids)
    assert ids == {"Claude": "claude:main", "Claude second": "claude:second", "Codex": "codex:main", "Grok": "grok:main"}


def test_R_owner_typed_label_matching_folder_stays_readable(mac):
    ids = _mac_ids(mac, [{"label": "Claude", "harness": "claude", "label_source": "owner"},
                         {"label": "Claude second", "harness": "claude", "label_source": "owner", "home": "~/.claude-second"},
                         {"label": "Claude work", "harness": "claude", "label_source": "owner", "home": "~/.claude-work"}])
    print("TYPED", ids)
    assert ids == {"Claude": "claude:main", "Claude second": "claude:second", "Claude work": "claude:work"}


def test_R_same_label_on_two_macs_is_one_id(mac, tmp_path, monkeypatch):
    a = _mac_ids(mac, [{"label": "Claude work", "harness": "claude", "label_source": "owner", "home": "~/.claude-work"}])
    _second_mac(mac, tmp_path, monkeypatch, "mac-b")
    b = _mac_ids(mac, [{"label": "Claude work", "harness": "claude", "label_source": "owner", "home": "~/.claude-office"}])
    print("A", a, "B", b)
    assert a["Claude work"] == b["Claude work"], "decision 9: one label, one account across Macs"


def test_R_two_different_accounts_on_two_macs_never_share_an_id(mac, tmp_path, monkeypatch):
    a = _mac_ids(mac, [{"label": "Claude work", "harness": "claude", "label_source": "suggested", "home": "~/.claude-work"}])
    _second_mac(mac, tmp_path, monkeypatch, "mac-b")
    b = _mac_ids(mac, [{"label": "Claude spare", "harness": "claude", "label_source": "suggested", "home": "~/.claude-spare"}])
    print("A", a, "B", b)
    assert a["Claude work"] != b["Claude spare"], "two different accounts got one id (one quota after merge)"


def test_R_no_folder_derived_id_is_written_to_the_log(mac):
    _mac_ids(mac, [{"label": "Claude", "harness": "claude"}])
    (mac.root / ".claude-zqprivate").mkdir()
    out = accounts.track([{"harness": "claude", "home": "~/.claude-zqprivate"}],
                         expect=config.accounts_version(), read=False, via="app")
    accounts.rename(out["accounts"][0]["key"], "Claude office", expect=config.accounts_version(), via="app")
    bad = []
    for line in routing.events_file().read_text().splitlines():
        e = json.loads(line)
        for k, v in e.items():
            vals = v.values() if isinstance(v, dict) else v if isinstance(v, list) else [v]
            for x in vals:
                if isinstance(x, str) and "zqprivate" in x and "|" not in x and k not in ("text", "prior_text"):
                    bad.append((e["type"], k, x))
    print("LOG", bad)
    assert not bad, f"folder-derived ids written to the log (replicated by 11a): {bad}"


def test_R_old_folder_name_in_a_label_after_a_recorded_move_stays_private(mac):
    """Label suggested from the old folder; the login later moved (or a fresh Mac binds by label)."""
    path = routing.events_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "account_move", "account": "claude|" + str(mac.root / ".claude-zqold"),
                               "target": "claude|" + str(mac.root / ".claude-zqnew"),
                               "written_by": {"kind": "owner"}, "written_at": routing._iso(mac.clock["now"])}) + "\n")
    ids = _mac_ids(mac, [{"label": "Claude zqold", "harness": "claude", "home": "~/.claude-zqnew"}])
    print("MOVED", ids)
    assert "zqold" not in ids["Claude zqold"]


def test_R_unset_list_readers_agree(mac, monkeypatch):
    monkeypatch.setattr(usage, "_command", lambda h: "/bin/true" if h in ("codex", "claude") else None)
    mac.cfg()
    routing.account_id("codex|")
    conv = routing.conversation_view(now=mac.clock["now"])
    ids = {a.get("label"): a.get("id") for a in conv["accounts"]}
    row = usage.public_row({"harness": "codex", "home": None})["id"]
    direct = routing.account_id("codex|")
    print("UNSET", ids, row, direct, routing.resolve_account("Codex"))
    assert ids == {}  # An unset list deliberately lists no conversation accounts.
    assert row == direct == routing.resolve_account("Codex")


@pytest.mark.parametrize("source", ["owner", "suggested", None])
def test_legacy_current_and_former_folder_classification(mac, source):
    entry = {"harness": "claude", "label": "Claude zqold", "home": "~/.claude-zqold"}
    if source:
        entry["label_source"] = source
    _mac_ids(mac, [entry])
    key = routing.accounts()[0]["key"]
    first = routing.account_id(key)
    if source == "owner":
        assert first == "claude:zqold"
    else:
        assert re.fullmatch(r"claude:a-[a-z2-7]{8}", first)
    (mac.root / ".claude-middle").mkdir()
    accounts.move(key, "~/.claude-middle", expect=config.accounts_version(), via="app")
    key = routing.accounts()[0]["key"]
    (mac.root / ".claude-new").mkdir()
    accounts.move(key, "~/.claude-new", expect=config.accounts_version(), via="app")
    assert routing.account_id(routing.accounts()[0]["key"]) == first
    assert config.load_config()["usage_accounts"][0]["label_source"] == (source or "suggested")


@pytest.mark.parametrize("via,label,source", [("app", None, "suggested"),
                                               ("app", "Claude work", "owner"),
                                               ("cli", "Claude work", "owner"),
                                               ("auto", None, "suggested"),
                                               ("offer", "Claude work", "suggested")])
def test_track_records_provenance_and_pins_id(mac, via, label, source):
    mac.cfg(usage_accounts=[])
    (mac.root / ".claude-work").mkdir()
    entry = {"harness": "claude", "home": "~/.claude-work"}
    if label:
        entry["label"] = label
    if via == "offer":
        entry["label_source"] = "suggested"
    out = accounts.track([entry], expect=config.accounts_version(), read=False,
                         via="app" if via == "offer" else via)
    key = out["accounts"][0]["key"]
    assert config.load_config()["usage_accounts"][0]["label_source"] == source
    ident = routing.account_id(key)
    if source == "suggested":
        assert re.fullmatch(r"claude:a-[a-z2-7]{8}", ident)
        assert accounts.status()["accounts"][0]["naming_hint"] == routing.account_identity.NAME_ACCOUNT
        assert routing.conversation_view()["accounts"][0]["naming_hint"] == routing.account_identity.NAME_ACCOUNT
        assert usage.public_row({"harness": "claude", "home": "~/.claude-work"})["naming_hint"] == routing.account_identity.NAME_ACCOUNT
        assert routing.account_identity.NAME_ACCOUNT in routing.summary()["text"]
    else:
        assert ident == "claude:work"
    before = routing.events_file().read_bytes(), (mac.data / "config.json").read_bytes()
    routing._IDENTITY_CACHE.clear()
    routing._MIGRATION_REVISION = None
    for _ in range(3):
        assert routing.account_id(key) == ident
        assert routing.resolve_account(ident) == ident
    assert before == (routing.events_file().read_bytes(), (mac.data / "config.json").read_bytes())
    # Even choosing the unchanged suggestion explicitly is an owner naming.
    accounts.rename(key, "Claude work", expect=config.accounts_version(), via="app")
    assert config.load_config()["usage_accounts"][0]["label_source"] == "owner"
    assert routing.account_id(key) == routing.resolve_account(ident) == "claude:work"
    assert "naming_hint" not in accounts.status()["accounts"][0]
    assert "naming_hint" not in routing.conversation_view()["accounts"][0]


def test_same_suggested_label_and_folder_on_two_macs_still_have_distinct_ids(mac, tmp_path, monkeypatch):
    entries = [{"harness": "claude", "label": "Claude work", "home": "~/.claude-work", "label_source": "suggested"}]
    first = _mac_ids(mac, entries)["Claude work"]
    _second_mac(mac, tmp_path, monkeypatch, "second-mac")
    second = _mac_ids(mac, entries)["Claude work"]
    assert first != second
    assert re.fullmatch(r"claude:a-[a-z2-7]{8}", first)
    assert re.fullmatch(r"claude:a-[a-z2-7]{8}", second)


def test_legacy_label_without_folder_evidence_is_owner(mac):
    ids = _mac_ids(mac, [{"harness": "codex", "label": "Codex office", "home": "~/.codex-login"}])
    assert ids == {"Codex office": "codex:office"}


def test_auto_track_keeps_suggested_provenance(mac, monkeypatch):
    mac.cfg(usage_accounts=[], usage_auto_track=True)
    (mac.root / ".claude-auto").mkdir()
    monkeypatch.setattr(usage, "report", lambda **kw: {"accounts": []})
    out = accounts.auto_track({"candidates": [{"offer_on_home": True, "cli": "claude", "path": "~/.claude-auto"}]})
    assert len(out) == 1
    entry = config.load_config()["usage_accounts"][0]
    assert entry["label_source"] == "suggested"
    assert re.fullmatch(r"claude:a-[a-z2-7]{8}", routing.account_id(out[0]["key"]))


def test_suggested_account_added_after_migration_is_pinned_once(mac):
    _mac_ids(mac, [{"harness": "grok", "label": "Grok"}])
    routing.migrate_accounts()
    mac.cfg(usage_accounts=[{"harness": "grok", "label": "Grok"},
                            {"harness": "claude", "label": "Claude later", "home": "~/.claude-later",
                             "label_source": "suggested"}])
    key = usage._key({"harness": "claude", "home": "~/.claude-later"})
    ident = routing.account_id(key)
    assert re.fullmatch(r"claude:a-[a-z2-7]{8}", ident)
    before = routing.events_file().read_bytes()
    routing._IDENTITY_CACHE.clear()
    routing._MIGRATION_REVISION = None
    assert routing.account_id(key) == ident
    assert routing.events_file().read_bytes() == before
    bindings = [e for e in routing.read_events()[0] if e["type"] == "account_binding" and e["account"] == key]
    assert len(bindings) == 1 and bindings[0]["to"] == ident


def test_owner_whole_list_rename_promotes_a_suggestion(mac):
    _mac_ids(mac, [{"harness": "claude", "label": "Claude work", "home": "~/.claude-work",
                    "label_source": "suggested"}])
    key = routing.accounts()[0]["key"]
    old = routing.account_id(key)
    rows = config.load_config()["usage_accounts"]
    rows[0]["label"] = "Claude second"
    accounts.save_list(rows, expect=config.accounts_version(), via="app")
    assert config.load_config()["usage_accounts"][0]["label_source"] == "owner"
    assert routing.account_id(key) == routing.resolve_account(old) == "claude:second"


def test_provenance_reader_cache_invalidates_when_owner_names_account(mac):
    entries = [{"harness": "claude", "label": "Claude work", "home": "~/.claude-work",
                "label_source": "suggested"}]
    _mac_ids(mac, entries)
    key = routing.accounts()[0]["key"]
    assert routing.account_label_source(key) == "suggested"
    accounts.rename(key, "Claude work", expect=config.accounts_version(), via="app")
    assert routing.account_label_source(key) == "owner"
    assert routing.account_id(key) == "claude:work"


@pytest.mark.parametrize("source", ["unknown", 42])
def test_invalid_provenance_is_refused(mac, source):
    mac.cfg(usage_accounts=[])
    with pytest.raises(usage.AccountsError):
        accounts.track([{"harness": "claude", "label": "Claude work", "label_source": source}],
                       expect=config.accounts_version(), read=False)
    with pytest.raises(usage.AccountsError):
        accounts.save_list([{"harness": "claude", "label": "Claude work", "label_source": source}],
                           expect=config.accounts_version())
    assert config.load_config()["usage_accounts"] == []
    assert not routing.events_file().exists()


def test_discovery_offer_marks_its_suggested_label():
    out = discover.public({"id": "synthetic", "cli": "claude", "name": "Claude work",
                           "path": "~/.claude-work", "default": False, "sources": ["conversation"],
                           "state": discover.SIGNED_IN})
    assert out["offer_on_home"] is True
    assert out["name"] == "Claude work" and out["label_source"] == "suggested"
