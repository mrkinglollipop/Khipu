"""One status row per Mac (`khipu macs --json`, table `mac_status`, app slice B3)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import psycopg
import pytest

from khipu import cli, db, health_five, macs, migrate
from tests.hermetic_pg import MIGRATIONS, hub, pg_cluster  # noqa: F401
from tests.test_health_five import NOW, report

ME, YOU = "a" * 32, "b" * 32
SQL = (MIGRATIONS / "0052_mac_status.sql").read_text(encoding="utf-8")


@pytest.fixture
def world(hub, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    conn = hub.connect()
    conn.execute(SQL)
    conn.commit()
    monkeypatch.setattr(db, "connect", lambda **kw: hub.connect())
    monkeypatch.setattr(macs, "local_mac_id", lambda: ME)
    monkeypatch.setattr(macs, "mac_label", lambda: "M1")
    monkeypatch.setattr(macs, "sign_ins", lambda: [{"harness": "claude", "label": "Work", "signed_in": True}])
    monkeypatch.setattr(macs, "version_stamp", lambda: "0.4.15 (aaaa1111)")
    monkeypatch.setattr(macs, "routing_version", lambda: "0123456789ab")
    monkeypatch.setattr("khipu.hub_reach.known_unreachable", lambda: None)
    return conn


def health_doc(**over) -> dict:
    return health_five.build(report(**over), now=NOW, mac="M1")


def put_other(name="M5", **over):
    row = {"name": name, "version": "0.4.14 (bbbb2222)", "saving": True,
           "health": {"checked_at": "2026-11-10T12:00:00Z", "state": "ok",
                      "areas": [{"key": "saving", "state": "ok"}], "routing_version": "0123456789ab"},
           "running": {"count": 2, "silent": []}, "signins": [{"harness": "codex", "label": "Codex", "signed_in": False}]}
    row.update(over)
    return macs.publish(row, mac=YOU)


def test_the_migration_is_additive_and_safe_to_run_twice(hub):  # noqa: F811
    conn = hub.connect()
    conn.execute(SQL)
    conn.execute(SQL)
    conn.commit()
    cols = [r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name='mac_status' ORDER BY ordinal_position")]
    assert cols == ["mac", "name", "version", "seen_at", "saving", "health", "running", "signins"]
    assert conn.execute("SELECT count(*) FROM schema_migrations WHERE version='0052_mac_status'").fetchone()[0] == 1
    assert "0052_mac_status" in [v for v, _ in migrate.available()]
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO mac_status(mac, health) VALUES ('x', '[1]'::jsonb)")


def test_the_projection_keeps_only_the_listed_fields():
    dirty = {"name": "M1", "version": "0.4.15 (aaaa1111)", "saving": True, "home": "/Users/matt", "prompt": "hello",
             "health": {"checked_at": "2026-11-10T12:00:00Z", "state": "warn", "headline": "text", "agent_note": "/Users/matt/x",
                        "areas": [{"key": "saving", "state": "ok", "line": "words", "path": "/Users/matt"},
                                  {"key": "bogus", "state": "ok"}, {"key": "jobs", "state": "great"}],
                        "routing_version": "0123456789ab", "free_gb": 80.04, "last_backup_at": "2026-11-10T03:00:00Z"},
             "running": {"count": 2, "silent": [{"name": "codex", "since": "2026-11-10T09:00:00Z", "cwd": "/x"},
                                                {"name": "/Users/matt/runner", "since": None}], "transcript": "secret"},
             "signins": [{"harness": "claude", "label": "Work", "signed_in": True, "home": "/Users/matt/.claude"},
                         {"harness": "claude", "label": "/Users/matt/.claude-2", "signed_in": True},
                         {"harness": "claude", "label": "~/.claude", "signed_in": True},
                         {"harness": "claude", "label": "Second", "signed_in": "yes"}]}
    row = macs.project(dirty)
    assert list(row) == list(macs.ROW_FIELDS)
    assert list(row["health"]) == list(macs.HEALTH_FIELDS)
    assert row["health"]["areas"] == [{"key": "saving", "state": "ok"}]
    assert row["health"]["free_gb"] == 80.0
    assert row["running"] == {"count": 2, "silent": [{"name": "codex", "since": "2026-11-10T09:00:00Z"}]}
    assert row["signins"] == [{"harness": "claude", "label": "Work", "signed_in": True}]
    blob = json.dumps(row)
    for leak in ("/Users", "~", "hello", "secret", "headline", "agent_note", "words"):
        assert leak not in blob
    assert macs.project("nonsense") == {k: None for k in macs.ROW_FIELDS}


def test_an_unlisted_field_is_dropped_on_write_and_again_on_read(world):
    macs.publish({"name": "M1", "version": "v", "saving": True, "extra": "x", "health": {"headline": "t", "checked_at": NOW.isoformat()}})
    world.execute("UPDATE mac_status SET running = '{\"count\": 1, \"silent\": [], \"cwd\": \"/Users/matt\"}', "
                  "health = '{\"checked_at\": \"2026-11-10T12:00:00Z\", \"login_home\": \"/Users/matt\"}' WHERE mac = %s", (ME,))
    world.commit()
    [row], how = macs.read_rows()
    assert how == "ok" and "/Users" not in json.dumps(row) and "cwd" not in row["running"] and "login_home" not in row["health"]


def test_two_macs_are_merged_this_one_first(world):
    macs.remember_health(health_doc())
    assert macs.publish(macs.local_row(health_doc()))["ok"]
    assert put_other()["ok"]
    out = macs.view(now=NOW)
    assert [m["name"] for m in out["macs"]] == ["M1", "M5"] and [m["this_mac"] for m in out["macs"]] == [True, False]
    me, other = out["macs"]
    assert me["version"] == "0.4.15 (aaaa1111)" and me["saving"] is True and me["signins"][0]["label"] == "Work"
    assert other["version"] == "0.4.14 (bbbb2222)" and other["stale"] is False and other["running"]["count"] == 2
    assert other["signins"] == [{"harness": "codex", "label": "Codex", "signed_in": False}]
    assert out["shared"] is True and "note" not in out and out["stale_after_s"] == 900


def test_a_row_older_than_fifteen_minutes_is_stale_never_current(world):
    put_other()
    world.execute("UPDATE mac_status SET seen_at = clock_timestamp() - interval '14 minutes' WHERE mac = %s", (YOU,))
    world.commit()
    assert macs.view()["macs"][1]["stale"] is False
    world.execute("UPDATE mac_status SET seen_at = clock_timestamp() - interval '16 minutes' WHERE mac = %s", (YOU,))
    world.commit()
    other = macs.view()["macs"][1]
    assert other["stale"] is True and other["age_s"] >= 16 * 60 and other["saving"] is True  # still shown, flagged
    health = health_five.build(report(), now=NOW, mac="M1", peers={"routing_version": "0123456789ab", "others": [other]})
    assert "same routing version" not in health["areas"][2]["line"] and health["macs"][1]["stale"] is True


def test_each_mac_writes_only_its_own_row(world):
    put_other()
    before = world.execute("SELECT seen_at, name FROM mac_status WHERE mac = %s", (YOU,)).fetchone()
    assert macs.publish(macs.local_row())["ok"] and macs.publish(macs.local_row())["ok"]
    world.commit()
    assert world.execute("SELECT seen_at, name FROM mac_status WHERE mac = %s", (YOU,)).fetchone() == before
    assert world.execute("SELECT count(*) FROM mac_status").fetchone()[0] == 2


def test_an_older_hub_without_the_table_shows_this_mac_only(world):
    world.execute("DROP TABLE mac_status")
    world.commit()
    out = macs.view()
    assert [m["name"] for m in out["macs"]] == ["M1"] and out["shared"] is False and "not on the hub yet" in out["note"]
    assert macs.publish(macs.local_row()) == {"ok": False, "why": "UndefinedTable"}


def test_no_other_row_is_this_mac_only_labelled(world):
    out = macs.view()
    assert [m["name"] for m in out["macs"]] == ["M1"] and "No other Mac has reported" in out["note"]


def test_a_hub_that_is_down_never_fails_health_or_macs(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(macs, "local_mac_id", lambda: ME)
    monkeypatch.setattr(macs, "mac_label", lambda: "M1")

    def down(**kw):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(db, "connect", down)
    monkeypatch.setattr("khipu.hub_reach.known_unreachable", lambda: None)
    monkeypatch.setattr(health_five, "run_doctor", lambda: (report(), ""))
    assert macs.publish(macs.local_row()) == {"ok": False, "why": "OperationalError"}
    assert cli.main(["health", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["headline"] == "Everything is working." and doc["macs"][0]["name"] == "M1" and len(doc["macs"]) == 1
    assert cli.main(["macs", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [m["name"] for m in out["macs"]] == ["M1"] and out["note"] == "The hub did not answer, so only this Mac is shown."


def test_health_publishes_this_mac_and_keeps_what_it_said_for_the_timer(world, monkeypatch, capsys):
    monkeypatch.setattr(health_five, "run_doctor", lambda: (report(outbox_ok=False, outbox={"dead": 1, "at_risk": True}), ""))
    assert cli.main(["health", "--json"]) == 0
    capsys.readouterr()
    [row], _ = macs.read_rows()
    assert row["mac"] == ME and row["health"]["state"] in ("warn", "err") and row["saving"] is False
    assert {a["key"] for a in row["health"]["areas"]} == set(macs.AREA_KEYS)
    # The timer has no doctor: it reports the last health it kept, with a fresh time.
    world.execute("DELETE FROM mac_status")
    world.commit()
    assert macs.publish()["ok"]
    [again], _ = macs.read_rows()
    assert again["saving"] is False and again["health"]["state"] == row["health"]["state"] and again["age_s"] < 60


def test_the_timer_with_no_health_yet_reports_what_it_knows(world):
    assert macs.publish()["ok"]
    [row], _ = macs.read_rows()
    assert row["saving"] is None and row["health"] is None and row["version"] == "0.4.15 (aaaa1111)"


def test_the_routing_row_says_whether_the_other_mac_holds_the_same_version():
    def routing_line(theirs, stale=False):
        other = {"name": "M5", "seen_at": "2026-11-10T11:59:00Z", "stale": stale, "health": {"routing_version": theirs}}
        d = health_five.build(report(), now=NOW, mac="M1", peers={"routing_version": "0123456789ab", "others": [other]})
        return d["areas"][2], d
    same, d = routing_line("0123456789ab")
    assert same["line"].endswith("M5 holds the same routing version.") and same["state"] == "ok"
    assert [m["name"] for m in d["macs"]] == ["M1", "M5"]
    differ, d = routing_line("ffffffffffff")
    assert differ["line"].endswith("M5 holds a different routing version right now.") and "ffffffffffff on M5" in d["agent_note"]
    for quiet in (routing_line(None)[0], routing_line("0123456789ab", stale=True)[0]):
        assert "routing version" not in quiet["line"]
    alone = health_five.build(report(), now=NOW, mac="M1")
    assert alone["macs"] == [] and alone["areas"][2]["line"] == "Your routing record is fresh on this Mac."


def test_the_mac_name_helper_lives_in_macs_and_health_uses_it(monkeypatch):
    monkeypatch.undo()
    assert health_five.mac_label() == macs.mac_label()
    assert macs.name_of("c" * 32, None) == "Your other Mac" and macs.name_of("c" * 32, "M5") == "M5"


def test_sign_ins_carry_no_path_and_read_only_the_cache(monkeypatch):
    from khipu import usage

    accounts = [{"harness": "claude", "label": "Work", "home": "/Users/matt/.claude"},
                {"harness": "codex", "label": "Codex", "home": "/Users/matt/.codex"}]
    cache = {usage._key(accounts[1]): {"ok": False, "error": "not_signed_in"}}
    monkeypatch.setattr(usage, "all_accounts", lambda: accounts)
    monkeypatch.setattr(usage, "_load_cache", lambda: cache)
    monkeypatch.setattr(usage, "probe_account", lambda *a, **k: pytest.fail("a probe was started"))
    got = macs.sign_ins()
    assert got == [{"harness": "claude", "label": "Work", "signed_in": True}, {"harness": "codex", "label": "Codex", "signed_in": False}]
    assert "/Users" not in json.dumps(macs.project({"signins": got}))


def test_macs_json_under_an_empty_home_is_this_mac_only(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(macs, "local_mac_id", lambda: None)
    assert cli.main(["macs", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["macs"]) == 1 and out["shared"] is False and "no routing identity" in out["note"]
