"""Synthetic CLI logins only: identity never leaves the probe in clear."""
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from khipu import config, routing, routing_advice, routing_page, routing_sources, usage


@pytest.fixture
def logins(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    folders = [home / ".claude", home / "second", home / "third"]
    for folder in folders:
        folder.mkdir()
    accounts = [{"harness": "claude", "label": label, **({"home": str(folder)} if i else {})}
                for i, (label, folder) in enumerate(zip(("Claude", "Claude second", "Claude third"), folders))]
    settings = {"usage_accounts": accounts[:2]}
    monkeypatch.setattr(config, "load_config", lambda: settings)
    monkeypatch.setattr(config, "read_config_unsettled", lambda: settings)
    frames = [json.loads(line) for line in (Path(__file__).parent / "fixtures/usage/claude.jsonl").read_text().splitlines()]
    # The usage read deliberately names no account id; only auth status
    # distinguishes the synthetic subscriptions.
    frames[0]["response"]["response"]["account"] = {}
    until = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    limits = frames[1]["response"]["response"]["rate_limits"]
    for block in (limits["five_hour"], limits["seven_day"], *limits["model_scoped"]):
        block["resets_at"] = until
    fixture = tmp_path / "frames.json"
    fixture.write_text(json.dumps(frames))
    calls = tmp_path / "calls.jsonl"
    program = tmp_path / "fake.py"
    program.write_text('''import json, os, sys, time
from pathlib import Path
root = Path(os.environ["CLAUDE_CONFIG_DIR"]) if "CLAUDE_CONFIG_DIR" in os.environ else Path.home() / ".claude"
with open(sys.argv[2], "a") as log:
    log.write(json.dumps(sys.argv[3:]) + "\\n")
if sys.argv[3:5] == ["auth", "status"]:
    data = json.loads((root / "synthetic-auth.json").read_text())
    if data.get("mode") == "fail":
        sys.stderr.write(json.dumps(data))
        sys.exit(7)
    if data.get("mode") == "malformed":
        print("not JSON")
        sys.exit(0)
    if data.get("mode") == "nonobject":
        print("[]")
        sys.exit(0)
    time.sleep(data.get("delay", 0))
    print(json.dumps(data, indent=2))
else:
    for frame in json.loads(Path(sys.argv[1]).read_text()):
        print(json.dumps(frame), flush=True)
''')
    binary = tmp_path / "claude"
    binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{program}" "{fixture}" "{calls}" "$@"\n')
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + ":/usr/bin:/bin")
    monkeypatch.setattr(usage, "_command", lambda cli: str(binary) if cli == "claude" else None)

    def identity(i, *, email="synthetic-person@example.invalid", org="synthetic-org-a", **extra):
        (folders[i] / "synthetic-auth.json").write_text(json.dumps(
            {"loggedIn": True, "email": email, "orgId": org, "orgName": "synthetic-private-name", **extra}))

    for i in range(3):
        identity(i)
    return accounts, settings, identity, calls, folders


def read(accounts, *, fresh=True):
    return usage.report(accounts=accounts, fresh=fresh, timeout=2)


def assert_private(report, caplog, capsys):
    # Check output, persistent row state, attempts and every other state file.
    output = json.dumps(report) + json.dumps([usage.public_row(row) for row in report["accounts"]])
    output += usage.table(report) + caplog.text + str(capsys.readouterr())
    for path in usage.cache_file().parent.rglob("*"):
        if path.is_file():
            output += path.read_text()
    for private in ("synthetic-person@example.invalid", "synthetic-org-a", "synthetic-org-b", "synthetic-private-name"):
        assert private not in output


def test_two_folders_same_account_and_owner_order(logins, caplog, capsys):
    accounts, settings, _, calls, _ = logins
    report = read(accounts[:2])
    first, second = report["accounts"]
    assert first["same_account_as"] == "Claude second"
    assert second["same_account_as"] == "Claude"
    assert first["ok"] and second["ok"]
    assert first["account_digest"] == second["account_digest"]
    assert first["note"] == usage.same_account_note("Claude second")
    assert_private(report, caplog, capsys)
    before = calls.read_text()
    assert read(accounts[:2], fresh=False)["accounts"][0]["same_account_as"] == "Claude second"
    assert calls.read_text() == before  # no identity process on a cached read
    _, status = routing_sources.usage_evidence({usage._key(a) for a in accounts[:2]})
    entries = [{"key": usage._key(a), "label": a["label"], "harness": "claude", "state": "open"}
               for a in accounts[:2]]
    for order in (entries, list(reversed(entries))):
        view = {"record": True, "accounts": entries, "sources": {"usage": status},
                "priority": {"entries": [{"account": e["key"]} for e in order]}}
        page = routing.page(view, datetime.now(timezone.utc))
        assert f"1. {order[0]['label']}: OK." in page
        assert f"2. {order[1]['label']}: SAME ACCOUNT." in page
        assert usage.same_account_note(order[1]["label"]) in page
        assert f"Same account as {order[0]['label']}." in page
    assert settings["usage_accounts"] == accounts[:2]


@pytest.mark.parametrize("change", [{"email": "another-person@example.invalid"}, {"org": "synthetic-org-b"}])
def test_different_accounts_or_organizations_are_not_duplicates(logins, change):
    accounts, _, identity, _, _ = logins
    identity(1, **change)
    assert all("same_account_as" not in row for row in read(accounts[:2])["accounts"])


@pytest.mark.parametrize("failure", [{"mode": "fail"}, {"mode": "malformed"}, {"mode": "nonobject"}])
def test_identity_failure_keeps_hash_and_match_for_only_thirty_minutes(logins, failure, caplog, capsys, monkeypatch):
    accounts, _, identity, _, _ = logins
    now = usage._now()
    monkeypatch.setattr(usage, "_now", lambda: now)
    old = read(accounts[:2])["accounts"][1]["account_digest"]
    identity(1, **failure)
    report = read(accounts[:2])
    assert all(row.get("same_account_as") for row in report["accounts"])
    failed = report["accounts"][1]
    assert failed["account_digest"] == old
    assert failed["ok"] and failed["error"] is None
    assert_private(report, caplog, capsys)
    # Failed attempts must not slide the confirmation time forward.
    now += timedelta(minutes=29)
    assert read(accounts[:2])["accounts"][1]["same_account_as"]
    now += timedelta(minutes=1, seconds=1)
    report = read(accounts[:2])
    assert all("same_account_as" not in row for row in report["accounts"])
    failed = report["accounts"][1]
    assert failed["account_digest"] == old and failed["note"] is None
    assert not failed["account_identity_ok"]
    assert "same_accounts" not in routing_sources.usage_evidence({usage._key(a) for a in accounts[:2]})[1]
    assert_private(report, caplog, capsys)


@pytest.mark.parametrize("signed_out", [{"loggedIn": False}, {"loggedIn": 1}, {"orgId": None}, {"email": " "}])
@pytest.mark.parametrize("figures_fail", [False, True])
def test_definite_no_account_clears_identity_and_restores_peer_immediately(
        logins, signed_out, figures_fail, caplog, capsys, monkeypatch):
    accounts, _, identity, _, _ = logins
    assert all(row.get("same_account_as") for row in read(accounts[:2])["accounts"])
    identity(0, **signed_out)
    if figures_fail:
        converse = usage._converse

        def figures(argv, *args, **kwargs):
            if "CLAUDE_CONFIG_DIR" not in kwargs["env"]:
                raise usage.ProbeError(usage.E_SIGNIN)
            return converse(argv, *args, **kwargs)

        monkeypatch.setattr(usage, "_converse", figures)
    report = read(accounts[:2])
    assert all("same_account_as" not in row for row in report["accounts"])
    cleared = report["accounts"][0]
    assert cleared["account_identity_ok"] is False
    assert "account_digest" not in cleared and "account_identity_at" not in cleared
    assert not usage.same_account_groups({row["key"]: row for row in report["accounts"]})
    cached = usage._load_cache()
    assert "account_digest" not in cached[usage._key(accounts[0])]
    assert "account_identity_at" not in cached[usage._key(accounts[0])]
    assert all("_account_identity_clear" not in row for row in [*report["accounts"], *cached.values()])
    _, status = routing_sources.usage_evidence({usage._key(a) for a in accounts[:2]})
    assert "same_accounts" not in status
    entries = [{"key": usage._key(a), "label": a["label"], "harness": "claude", "state": "open"}
               for a in accounts[:2]]
    view = {"record": True, "accounts": entries, "sources": {"usage": status},
            "priority": {"entries": [{"account": e["key"]} for e in entries]}}
    if figures_fail:
        view["accounts"][0]["state"] = "down"
    normal = {**view, "sources": {"usage": {}}}
    peer = next(row for row in routing_advice.rank(view, usage._now()) if row["label"] == accounts[1]["label"])
    normal_peer = next(row for row in routing_advice.rank(normal, usage._now()) if row["label"] == accounts[1]["label"])
    assert peer["verdict"] == "go" and "same_account_as" not in peer
    assert peer["rank"] == normal_peer["rank"]
    assert peer["why_ranked"] == normal_peer["why_ranked"] == "priority 2"
    assert "SAME ACCOUNT" not in routing.page(view, usage._now())
    assert_private(report, caplog, capsys)
    # A fresh sign-in restores the match on the very next read.
    identity(0)
    assert all(row.get("same_account_as") for row in read(accounts[:2])["accounts"])


def test_mark_clears_after_a_folder_changes_account(logins):
    accounts, _, identity, _, _ = logins
    assert read(accounts[:2])["accounts"][0]["same_account_as"]
    identity(1, org="synthetic-org-b")
    report = read(accounts[:2])
    assert all("same_account_as" not in row and row["note"] is None for row in report["accounts"])


def test_three_folders_and_untracked_cached_rows(logins):
    accounts, settings, _, _, _ = logins
    settings["usage_accounts"] = accounts
    report = read(accounts)
    assert report["accounts"][0]["same_account_as"] == "Claude second, Claude third"
    settings["usage_accounts"] = accounts[:1]
    assert "same_account_as" not in read(accounts[:1], fresh=False)["accounts"][0]
    assert "same_accounts" not in routing_sources.usage_evidence({usage._key(accounts[0])})[1]


def test_optional_identity_timeout_is_bounded(logins):
    accounts, _, identity, _, _ = logins
    identity(1, delay=10)
    start = time.monotonic()
    row = usage.probe_account(accounts[1], timeout=0.3, turn=False)
    elapsed = time.monotonic() - start
    assert row["ok"] and row["error"] is None and not row["account_identity_ok"]
    assert elapsed < 2.5


def test_identity_read_timing_in_throwaway_folder(logins, record_property):
    _, _, _, calls, folders = logins
    start = time.monotonic()
    digest = usage._claude_identity(usage._command("claude"), str(folders[0]), start + 1.5)
    elapsed = time.monotonic() - start
    assert digest
    assert ["auth", "status", "--json"] in [json.loads(line) for line in calls.read_text().splitlines()]
    record_property("fake_auth_status_seconds", round(elapsed, 3))


def test_grok_reuses_existing_digest_and_never_matches_another_cli(monkeypatch, logins):
    from tests.test_usage import FIXTURES

    frames = [json.loads(line) for line in (FIXTURES / "grok.jsonl").read_text().splitlines()]
    for frame in frames:
        if frame.get("id") == 2:
            frame["result"]["accountId"] = "synthetic-grok-private-id"
    data = usage.parse_grok(frames)
    assert "synthetic-grok-private-id" not in json.dumps(data)
    monkeypatch.setattr(usage, "probe_grok", lambda *a, **kw: data)
    accounts, _, _, _, folders = logins
    grok = [{"harness": "grok", "label": f"Grok {i}", "home": str(folder)} for i, folder in enumerate(folders[:2])]
    rows = [usage.probe_account(a) for a in grok]
    for a, row in zip(grok, rows):
        row["key"] = usage._key(a)
    rows.append(dict(rows[0], key="claude|", harness="claude", account="Claude"))
    usage.mark_same_accounts(rows)
    assert rows[0]["same_account_as"] == "Grok 1"
    assert rows[1]["same_account_as"] == "Grok 0"
    assert "same_account_as" not in rows[2]


def test_identity_updates_even_when_newer_figures_are_kept():
    before = {"harness": "claude", "ok": True, "account_digest": usage._account_digest("claude", "old"),
              "account_identity_ok": True, "probed_at": "2026-11-11T12:00:00Z", "last_good_at": "2026-11-11T12:00:00Z"}
    after = {"harness": "claude", "ok": False, "error": "timeout", "probed_at": "2026-11-11T12:01:00Z",
             "account_digest": usage._account_digest("claude", "new"), "account_identity_ok": True}
    merged = usage._merge(before, after)
    assert merged["account_digest"] == after["account_digest"]
    assert merged["account_identity_ok"]


@pytest.mark.parametrize("error", [usage.E_NO_FIGURES, usage.E_NO_LIMITS, usage.E_TIMEOUT])
@pytest.mark.parametrize("turn", [False, True])
def test_figures_failure_still_reads_identity_and_keeps_cached_room(logins, monkeypatch, error, turn):
    accounts, _, _, _, _ = logins
    before = read(accounts[:2])["accounts"]
    monkeypatch.setattr(usage, "_turn_due", lambda *_a, **_kw: False)

    def refused(*_a, **_kw):
        if error == usage.E_TIMEOUT:
            raise usage.ProbeError(error)
        return [{"type": "control_response", "response": {"request_id": "usage", "response": {
            "rate_limits": None, "rate_limits_available": error == usage.E_NO_FIGURES}}}]

    monkeypatch.setattr(usage, "_converse", refused)
    rows = [usage._merge(old, usage.probe_account(a, turn=turn)) for a, old in zip(accounts[:2], before)]
    usage.mark_same_accounts(rows)
    assert all(not r["ok"] and r["error"] == error for r in rows)
    assert all(r["account_identity_ok"] and r.get("same_account_as") for r in rows)
    assert all(r["last_good_at"] == old["last_good_at"] and r["windows"] == old["windows"]
               for r, old in zip(rows, before))


def test_cached_identity_expires_without_another_attempt(logins, monkeypatch):
    accounts, _, _, _, _ = logins
    now = usage._now()
    monkeypatch.setattr(usage, "_now", lambda: now)
    rows = read(accounts[:2])["accounts"]
    now += timedelta(minutes=30, seconds=1)
    assert not usage.same_account_groups({r["key"]: r for r in rows})


def test_identity_deadline_never_extends_the_callers_timeout(logins, monkeypatch):
    accounts, _, _, _, _ = logins
    clock = [100.0]
    deadlines = []
    monkeypatch.setattr(usage.time, "monotonic", lambda: clock[0])

    def figures(*_a, **_kw):
        clock[0] = 100.8
        return []

    monkeypatch.setattr(usage, "_converse", figures)
    monkeypatch.setattr(usage, "parse_claude", lambda _: {"ok": True})
    monkeypatch.setattr(usage, "_claude_identity", lambda _b, _h, deadline: deadlines.append(deadline))
    usage.probe_account(accounts[0], timeout=1, turn=False)
    assert deadlines == [101.0]


def test_identity_can_be_disabled_by_plan_only_callers(logins, monkeypatch):
    accounts, _, identity, calls, _ = logins
    before = read(accounts[:2])["accounts"][0]
    identity(0, loggedIn=False)
    calls.write_text("")
    row = usage.probe_account(accounts[0], turn=False, identity=False)
    assert row["ok"]
    assert ["auth", "status", "--json"] not in [json.loads(line) for line in calls.read_text().splitlines()]
    merged = usage._merge(before, row)
    assert merged["account_digest"] == before["account_digest"]
    assert merged["account_identity_at"] == before["account_identity_at"]
    assert merged["account_identity_ok"] is True


@pytest.mark.parametrize("failure", ["deadline", "timeout", "exception"])
def test_identity_skipped_or_failed_read_never_clears_confirmation(logins, monkeypatch, failure):
    from khipu import discover

    accounts, _, _, _, _ = logins
    before = read(accounts[:2])["accounts"][0]

    def run_once(*_args, **_kwargs):
        if failure == "deadline":
            pytest.fail("an expired deadline must not run auth status")
        if failure == "exception":
            raise OSError("synthetic read failure")
        return {"outcome": "timeout", "code": None, "out": b""}

    monkeypatch.setattr(discover, "run_once", run_once)
    if failure == "deadline":
        monkeypatch.setattr(usage, "_converse", lambda *_args, **_kwargs: [])
        monkeypatch.setattr(usage, "parse_claude", lambda _: {"ok": True})
    row = usage.probe_account(accounts[0], turn=False, timeout=0 if failure == "deadline" else 2)
    merged = usage._merge(before, row)
    assert merged["account_identity_ok"] is True
    assert merged["account_digest"] == before["account_digest"]
    assert merged["account_identity_at"] == before["account_identity_at"]


def paired_view(*, reverse=False, state="open"):
    entries = [{"key": "claude|/synthetic/first", "label": "Claude", "harness": "claude", "state": "open"},
               {"key": "claude|/synthetic/second", "label": "Claude second", "harness": "claude", "state": state}]
    order = list(reversed(entries)) if reverse else entries
    return {"record": True, "accounts": entries, "priority": {"entries": [{"account": e["key"]} for e in order]},
            "sources": {"usage": {"same_accounts": {e["key"]: [entries[1-i]["key"]] for i, e in enumerate(entries)}}}}


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("peer_state", ["open", "paused"])
def test_advice_and_json_never_offer_duplicate_room(reverse, peer_state):
    from khipu import routing_advice

    view = paired_view(reverse=reverse)
    first, second = [next(e for e in view["accounts"] if e["key"] == p["account"])
                     for p in view["priority"]["entries"]]
    first["state"] = peer_state
    now = datetime.now(timezone.utc)
    ranked = routing_advice.rank(view, now)
    assert [r["label"] for r in ranked] == [first["label"], second["label"]]
    duplicate = ranked[1]
    assert duplicate["verdict"] != "go"
    assert duplicate["same_account_as"] == first["label"]
    assert f"same account as {first['label']}" in duplicate["why_ranked"]
    direct = routing_advice.account_advice(view, second, now)
    assert direct["verdict"] != "go" and direct["same_account_as"] == first["label"]
    public = routing._public_account(second, now, view)
    assert public["same_account_as"] == first["label"]
    summary = routing.project_route_view({"accounts": [public]}, "summary")
    assert summary["accounts"][0]["same_account_as"] == first["label"]
    sources = routing._public_sources(view["sources"])
    expected = {routing.account_id(k): [routing.account_id(p) for p in peers]
                for k, peers in view["sources"]["usage"]["same_accounts"].items()}
    assert sources["usage"]["same_accounts"] == expected
    assert "/synthetic/" not in json.dumps([sources, public, ranked])


@pytest.mark.parametrize("state,held,word", [("paused", False, "PAUSED"), ("open", True, "NO JOBS"),
    ("limited", False, "LIMIT REACHED"), ("down", False, "DOWN"), ("unknown", False, "UNKNOWN")])
def test_duplicate_page_keeps_closed_state_and_note(state, held, word):
    view = paired_view(state=state)
    view["accounts"][1]["until"] = "2026-12-01T12:00:00Z"
    if held:
        view["holds"] = [{"scope": {"kind": "account", "account": view["accounts"][1]["key"]},
                          "owner": True, "text": "Keep this hold visible"}]
    page = routing.page(view, datetime.now(timezone.utc))
    line = next(line for line in page.splitlines() if line.startswith("2. "))
    assert line.startswith(f"2. Claude second: {word}")
    assert "Same account as Claude" in line
    if held:
        assert "Keep this hold visible" in line
    if state in ("paused", "limited"):
        assert "Dec 01 12:00Z" in line


@pytest.mark.parametrize("state", ["open", "paused"])
def test_signature_changes_when_same_account_appears_or_clears(state):
    view = paired_view(state=state)
    now = datetime.now(timezone.utc)
    same = view["sources"]["usage"].pop("same_accounts")
    before = routing_page.signature(view, now)
    view["sources"]["usage"]["same_accounts"] = same
    assert routing_page.signature(view, now) != before
    facts = routing_page.facts(view, now)["accounts"]
    assert facts[0]["same_as"] == [view["accounts"][1]["key"]]
    assert facts[1]["same_as"] == [view["accounts"][0]["key"]]
    assert facts[1]["word"] == ("SAME ACCOUNT" if state == "open" else "PAUSED")
    assert "SAME ACCOUNT" in routing_page.WORDS
    view["sources"]["usage"].pop("same_accounts")
    assert routing_page.signature(view, now) == before


def test_page_full_note_only_on_first_row_and_shortens_before_rules(monkeypatch):
    view = paired_view()
    view["rules"] = [{"text": "Keep every owner rule. " + "Standing wording. " * 30}]
    now = datetime.now(timezone.utc)
    page = routing.page(view, now)
    assert page.count("Sign one of them in to the other account.") == 1
    assert "2. Claude second: SAME ACCOUNT. Same account as Claude." in page
    compact = page.replace(" Sign one of them in to the other account.", "")
    monkeypatch.setattr(routing_page, "LIMIT", len(compact))
    fitted = routing.page(view, now)
    assert "Keep every owner rule." in fitted and "more rule lines" not in fitted
    assert "Sign one of them in" not in fitted
    assert "both folders are signed in to one account." in fitted
    assert len(fitted) <= len(compact)
