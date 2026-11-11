"""Synthetic CLI logins only: identity never leaves the probe in clear."""
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from khipu import config, routing, routing_page, routing_sources, usage


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
        assert usage.same_account_note(order[0]["label"]) in page
    assert settings["usage_accounts"] == accounts[:2]


@pytest.mark.parametrize("change", [{"email": "another-person@example.invalid"}, {"org": "synthetic-org-b"}])
def test_different_accounts_or_organizations_are_not_duplicates(logins, change):
    accounts, _, identity, _, _ = logins
    identity(1, **change)
    assert all("same_account_as" not in row for row in read(accounts[:2])["accounts"])


@pytest.mark.parametrize("failure", [{"mode": "fail"}, {"mode": "malformed"}, {"loggedIn": False}, {"orgId": None}])
def test_identity_failure_keeps_hash_without_a_mark_or_usage_error(logins, failure, caplog, capsys):
    accounts, _, identity, _, _ = logins
    old = read(accounts[:2])["accounts"][1]["account_digest"]
    identity(1, **failure)
    report = read(accounts[:2])
    assert all("same_account_as" not in row for row in report["accounts"])
    failed = report["accounts"][1]
    assert failed["account_digest"] == old
    assert failed["ok"] and failed["error"] is None and failed["note"] is None
    assert not failed["account_identity_ok"]
    assert "same_accounts" not in routing_sources.usage_evidence({usage._key(a) for a in accounts[:2]})[1]
    assert_private(report, caplog, capsys)


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
