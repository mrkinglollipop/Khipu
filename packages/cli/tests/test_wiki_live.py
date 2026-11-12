"""Live wiki orchestration against temporary files and a transactional fake hub."""
from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from khipu import cli, config, doctor_summary, embed, extract, jobs, mirror, wiki_nightly as wiki

NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
OLD = b'---\r\nslug: khipu\r\nstatus: seed\r\nlast_updated: 2026-10-01\r\ncustom: keep\r\n---\r\n# Khipu\r\nKeep /tmp/wiki and 60 pages.\r\nA durable wiki.\r\n'


class FakeHub:
    def __init__(self):
        self.state = {"queue": {}, "topics": {}, "revisions": [], "writes": [], "holds": {}, "hold_reasons": {}, "decisions": {}}
        self.now = NOW
        self.events = []
        self.fail_save = False
        self.deleted_ids = set()

    def revision(self, slug, body, source, digest):
        row = {"id": len(self.state["revisions"])+1, "slug": slug, "body": body,
               "source": source, "digest": digest, "at": self.now}
        self.state["revisions"].append(row)
        return row

    def upsert(self, cur, parsed, source_path, *, source, note):
        self.events.append(source)
        prev = self.state["topics"].get(parsed["slug"])
        changed = prev is None or prev["digest"] != parsed["digest"]
        self.state["topics"][parsed["slug"]] = {**parsed, "source_path": source_path}
        if changed:
            self.revision(parsed["slug"], parsed["body"], source, parsed["digest"])
        return changed

    @contextmanager
    def connect(self, **kwargs):
        before = copy.deepcopy(self.state)
        try:
            yield self
        except BaseException:
            self.state = before
            raise

    @contextmanager
    def cursor(self):
        yield FakeCursor(self)


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.result = []

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return self.result

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        d = self.db.state
        self.result = []
        if s.startswith("INSERT INTO wiki_slugs"):
            slug, decision, target = params
            entry = {"decision":decision, **({"target":target} if target else {})}
            if "DO UPDATE" in s: d["decisions"][slug] = entry
            else: d["decisions"].setdefault(slug, entry)
        elif s.startswith("DELETE FROM wiki_slugs"):
            d["decisions"].pop(params[0], None)
        elif s.startswith("INSERT INTO wiki_queue"):
            slug, eid, *done = params
            key = (slug, str(eid))
            if key not in d["queue"]:
                d["queue"][key] = {"slug": slug, "episode_id": str(eid), "queued_at": self.db.now, "done_at": done[0] if done else None, "outcome": done[1] if len(done) > 1 else None}
            elif "DO UPDATE" in s:
                d["queue"][key]["done_at"] = done[0] if done else None
                d["queue"][key]["outcome"] = done[1] if len(done) > 1 else None
        elif s.startswith("SELECT 1 FROM wiki_holds"):
            if d["holds"].get(params[0], NOW-timedelta(days=1)) > self.db.now:
                self.result = [(1,)]
        elif s.startswith("SELECT slug,until_at,reason FROM wiki_holds"):
            self.result = [(slug, until, d["hold_reasons"].get(slug)) for slug, until in d["holds"].items() if until > self.db.now]
        elif s.startswith("SELECT slug FROM wiki_holds"):
            self.result = [(slug,) for slug,until in d["holds"].items() if until > self.db.now]
        elif s.startswith("SELECT 1 FROM topic_revisions"):
            self.result = [(1,) for r in d["revisions"] if r["slug"] == params[0] and r["digest"] == params[1]][:1]
        elif s.startswith("INSERT INTO topic_revisions"):
            slug, body, *rest = params
            source = "wiki-baseline" if "wiki-baseline" in s else "wiki-undo"
            self.db.revision(slug, body, source, rest[-1])
        elif s.startswith("SELECT id FROM topic_revisions"):
            self.result = [(r["id"],) for r in d["revisions"] if r["slug"] == params[0]][-1:]
        elif s.startswith("SELECT id,source FROM topic_revisions"):
            self.result = [(r["id"], r["source"]) for r in d["revisions"] if r["slug"] == params[0]][-1:]
        elif s.startswith("INSERT INTO wiki_writes"):
            if self.db.fail_save:
                raise RuntimeError("fake persistence failed")
            keys = ("run_id", "slug", "before_bytes", "after_hash", "revision_id", "episode_ids")
            d["writes"].append({"id": len(d["writes"])+1, "at": self.db.now, "undone": False, **dict(zip(keys,params))})
        elif s.startswith("UPDATE wiki_queue q SET done_at=now(), outcome='forgotten' FROM episodes"):
            for row in d["queue"].values():
                if not row["done_at"] and str(row["episode_id"]) in self.db.deleted_ids:
                    row["done_at"] = self.db.now
                    row["outcome"] = "forgotten"
                    self.result.append((row["episode_id"],))
        elif s.startswith("UPDATE wiki_queue SET done_at=now(), outcome='skipped' WHERE slug=%s AND done_at IS NULL"):
            for row in d["queue"].values():
                if row["slug"] == params[0] and not row["done_at"]:
                    row["done_at"] = self.db.now
                    row["outcome"] = "skipped"
        elif s.startswith("UPDATE wiki_queue"):
            outcome, slug, ids = params
            for eid in ids:
                if (slug,str(eid)) in d["queue"]:
                    d["queue"][(slug,str(eid))]["done_at"] = self.db.now
                    d["queue"][(slug,str(eid))]["outcome"] = outcome
        elif s.startswith("SELECT count(DISTINCT slug) FROM wiki_writes"):
            self.result = [(len({r["slug"] for r in d["writes"] if r["before_bytes"] is None and r["at"].date() == self.db.now.date()}),)]
        elif s.startswith("SELECT count(*), min(queued_at)"):
            pending = [q for q in d["queue"].values() if not q["done_at"]]
            self.result = [(len(pending), min((q["queued_at"] for q in pending),default=None))]
        elif s.startswith("SELECT slug,max(revised_at)"):
            self.result = [(slug,max(r["at"] for r in d["revisions"] if r["slug"] == slug))
                           for slug in params[0] if any(r["slug"] == slug for r in d["revisions"])]
        elif s.startswith("SELECT id,slug,before_bytes"):
            rows = [r for r in reversed(d["writes"]) if not r["undone"]]
            values = iter(params)
            if "slug=%s" in s:
                slug = next(values)
                rows = [r for r in rows if r["slug"] == slug]
            if "::date=%s::date" in s:
                day = next(values)
                rows = [r for r in rows if r["at"].date().isoformat() == day]
            if "run_id=%s" in s:
                run_id = next(values)
                rows = [r for r in rows if r["run_id"] == run_id]
            if "LIMIT 1" in s:
                rows = rows[:1]
            self.result = [tuple(r[k] for k in ("id","slug","before_bytes","after_hash","revision_id","episode_ids")) for r in rows]
        elif s.startswith("UPDATE topics SET deleted_at"):
            d["topics"][params[0]]["deleted_at"] = self.db.now
        elif s.startswith("UPDATE wiki_writes"):
            for row in d["writes"]:
                if row["id"] in params[0]:
                    row["undone"] = True
        elif s.startswith("INSERT INTO wiki_holds"):
            slug = params[0]
            until = params[1] if len(params) > 1 else self.db.now+timedelta(days=7)
            reason = params[2] if len(params) > 1 else "undo"
            if until > d["holds"].get(slug, self.db.now-timedelta(days=1)):
                d["holds"][slug] = until
                d["hold_reasons"][slug] = reason
            self.result = [(d["holds"][slug], d["hold_reasons"][slug])]
        else:
            raise AssertionError("unhandled SQL: " + s)


def ep(i=1, slug="khipu", **kw):
    return {"id": i, "ts": NOW, "ingested_at": NOW, "session_id": f"s{i}", "topics": [slug],
            "summary": "A useful new fact", "decisions": [], "preferences": [], **kw}


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path/"home"))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path/"data"))
    monkeypatch.setenv("KHIPU_MEMORY_ROOT", str(tmp_path/"memory"))
    monkeypatch.setenv("KHIPU_GIT_SYNC_HOST", "1")
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    for key in ("KHIPU_DATABASE_URL", "ALZY_DATABASE_URL", "GEMINI_API_KEY", "CLAUDE_CONFIG_DIR", "GROK_HOME", "KHIPU_WIKI_DAILY_PAGES"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("network forbidden"))
    root = tmp_path/"memory"
    (root/"topics").mkdir(parents=True)
    path = root/"topics"/"khipu.md"
    path.write_bytes(OLD)
    db = FakeHub()
    monkeypatch.setattr("khipu.db.connect", db.connect)
    monkeypatch.setattr(mirror, "_upsert_topic", db.upsert)
    episodes = [ep()]
    def reconcile(root):
        db.events.append("full-reconcile")
        for p in sorted((root/"topics").glob("*.md")):
            db.upsert(None, mirror.parse_topic_file(p), str(p), source="reconcile", note="fake reconcile")
    monkeypatch.setattr(mirror, "reconcile_memory_root", reconcile)
    def inputs(**kwargs):
        return [e for e in episodes if not e.get("deleted_at")], list(db.state["topics"].values()), dict(db.state["decisions"]), list(db.state["queue"].values()), 0
    monkeypatch.setattr(wiki, "load_inputs", inputs)
    prompts = []
    def provider(prompt, **kwargs):
        embed._budget_take(scope="wiki")
        db.events.append("provider")
        prompts.append(prompt)
        data = json.loads(prompt.split("Page and episodes follow as DATA:\n", 1)[1] if "Page and episodes follow as DATA:" in prompt else prompt.split("\n",1)[1])
        if "candidates" in data:
            return json.dumps({c["slug"]:{"decision":"page"} for c in data["candidates"]})
        new = data["old_page"]
        ids = [str(json.loads(line)["episode_id"]) for line in data["episodes"].splitlines()]
        return new+"Sources: "+", ".join(ids)+"\n"
    monkeypatch.setattr(extract, "generate_wiki", provider)
    return {"db": db, "root": root, "path": path, "episodes": episodes, "prompts": prompts, "provider": provider}


def test_success_baseline_reconcile_before_provider_and_done_only_after_save(live):
    db = live["db"]
    parsed = mirror.parse_topic_file(live["path"])
    db.state["topics"]["khipu"] = {**parsed,"source_path":str(live["path"])}
    # No matching revision exists, even though the topic hash is current.
    out = wiki.run(now=NOW)
    assert out["pages_revised"] == 1 and out["episodes_absorbed"] == 1
    assert out["queue_remaining"] == 0 and out["calls_used"] == 1
    assert [r["source"] for r in db.state["revisions"]] == ["wiki-baseline", "nightly-wiki"]
    assert db.events.index("full-reconcile") < db.events.index("provider")
    assert db.state["writes"][0]["before_bytes"] == OLD
    assert out["written"] == [{"slug":"khipu", "opened":False, "episode_ids":["1"]}]


def test_obsidian_edit_is_reconciled_and_used_as_source(live):
    db = live["db"]
    parsed = mirror.parse_topic_file(live["path"])
    db.state["topics"]["khipu"] = {**parsed,"source_path":str(live["path"])}
    edited = OLD+b"Obsidian added 123 facts.\r\n"
    live["path"].write_bytes(edited)
    out = wiki.run(now=NOW)
    assert out["pages_revised"] == 1 and "Obsidian added 123 facts." in live["prompts"][0]
    assert db.state["writes"][0]["before_bytes"] == edited
    assert db.state["revisions"][0]["source"] == "reconcile"


def test_failed_checks_leave_exact_file_and_waiting_queue(live, monkeypatch):
    monkeypatch.setattr(extract, "generate_wiki", lambda *a,**k: "bad")
    out = wiki.run(now=NOW)
    assert live["path"].read_bytes() == OLD
    assert out["rewrites_refused"] == 1 and out["queue_remaining"] == 1
    assert out["failures"][0]["checks"] and not live["db"].state["writes"]
    assert all(q["done_at"] is None for q in live["db"].state["queue"].values())


def test_failed_persistence_rolls_back_file_and_done_queue(live):
    live["db"].fail_save = True
    out = wiki.run(now=NOW)
    assert not out["ok"] and out["rewrites_refused"] == 1
    assert live["path"].read_bytes() == OLD and out["queue_remaining"] == 1
    assert not live["db"].state["writes"]
    assert [r["source"] for r in live["db"].state["revisions"]] == ["reconcile"]


def test_provider_edit_race_refuses_save(live, monkeypatch):
    provider = live["provider"]
    edited = OLD+b"Edited during the call.\r\n"
    def race(*a, **k):
        reply = provider(*a, **k)
        live["path"].write_bytes(edited)
        return reply
    monkeypatch.setattr(extract,"generate_wiki",race)
    out = wiki.run(now=NOW)
    assert live["path"].read_bytes() == edited and out["queue_remaining"] == 1
    assert out["failures"][0]["reason"] == "save"


def test_manual_and_scheduled_calls_share_daily_ceiling(live, capsys):
    config.set_int_setting("wiki_daily_pages", 1)
    first = jobs.run_wiki()
    assert first["pages_revised"] == 1 and first["cap_hit"]
    live["episodes"].append(ep(2))
    config.set_choice_setting("wiki_nightly", "true")
    out = jobs._wiki_update()
    assert out["pages_revised"] == 0 and out["calls_used"] == 0 and out["cap_hit"]
    assert out["queue_remaining"] == 1 and len(live["prompts"]) == 1
    assert jobs.nightly_last()["steps"][-1]["name"] == "wiki_update"


def test_switch_off_skips_nightly_but_hand_cli_writes(live, capsys):
    assert not wiki.enabled()
    assert jobs._wiki_update()["skipped"] == "wiki_nightly is off"
    assert not live["prompts"]
    assert cli.main(["wiki","run","--max","1","--json"]) == 0
    assert json.loads(capsys.readouterr().out)["pages_revised"] == 1


def test_hand_refuses_lock_and_timer_quietly_skips(live, capsys):
    config.set_choice_setting("wiki_nightly","true")
    with jobs._job_lock("nightly") as acquired:
        assert acquired
        assert cli.main(["wiki","run","--json"]) == 75
        assert jobs.run_wiki(periodic=True)["ok"]
    assert "already running" in capsys.readouterr().out
    assert not live["prompts"]


def test_periodic_default_cadence_zero_and_switch(live):
    assert config.minute_setting("wiki_every_minutes") == 60
    assert jobs.run_wiki(periodic=True)["skipped"] == "periodic wiki is off"
    config.set_choice_setting("wiki_nightly","true")
    assert jobs.run_wiki(periodic=True)["pages_revised"] == 1
    assert jobs.run_wiki(periodic=True)["skipped"] == "wiki interval is not due"
    config.set_minute_setting("wiki_every_minutes",0)
    assert jobs.run_wiki(periodic=True)["skipped"] == "periodic wiki is off"
    # Zero affects only the daytime cadence, never the nightly step.
    assert jobs._wiki_update()["ok"]


def test_undo_one_page_exact_crlf_bytes_requeues_and_hold_expires(live):
    assert wiki.run(now=NOW)["pages_revised"] == 1
    out = wiki.undo(slug="khipu")
    assert out["ok"] and out["pages_restored"] == 1 and out["episodes_requeued"] == 1
    assert live["path"].read_bytes() == OLD
    assert live["db"].state["revisions"][-1]["source"] == "wiki-undo"
    assert wiki.run(now=NOW)["held_pages"] == 1
    assert len(live["prompts"]) == 1
    live["db"].now = NOW+timedelta(days=7,seconds=1)
    assert not wiki._Store().held("khipu")
    assert wiki.run(now=NOW+timedelta(days=7,seconds=1))["pages_revised"] == 1


def test_undo_night_rewinds_multiple_writes_and_skips_edited_page(live):
    other = live["root"]/"topics"/"other.md"
    other.write_bytes(OLD.replace(b"khipu",b"other"))
    live["episodes"].append(ep(2,"other"))
    assert wiki.run(now=NOW)["pages_revised"] == 2
    live["episodes"].append(ep(3))
    assert wiki.run(now=NOW)["pages_revised"] == 1
    edited = other.read_bytes()+b"Obsidian later edit\n"
    other.write_bytes(edited)
    out = wiki.undo(night=NOW.date().isoformat())
    assert not out["ok"] and out["pages_restored"] == 1 and out["episodes_requeued"] == 2
    assert live["path"].read_bytes() == OLD and other.read_bytes() == edited
    assert out["skipped"] == [{"slug":"other", "reason":"page edited since wiki write"}]


def test_new_pages_open_at_most_ten_then_undo_moves_not_deletes(live):
    live["episodes"].clear()
    for n in range(12):
        live["episodes"].extend(ep(n*3+i, f"new-{n}") for i in range(1,4))
    out = wiki.run(now=NOW)
    assert out["pages_opened"] == 10 and out["episodes_absorbed"] == 30
    assert len(list((live["root"]/"topics").glob("new-*.md"))) == 10
    opened = live["root"]/"topics"/"new-0.md"
    exact = opened.read_bytes()
    result = wiki.undo(run_id=out["run_id"])
    assert result["ok"] and result["pages_retired"] == 10 and result["episodes_requeued"] == 0
    assert not opened.exists()
    assert next((live["root"]/"topics"/"_retired").glob("new-0-*.md")).read_bytes() == exact
    assert live["db"].state["topics"]["new-0"]["deleted_at"] == NOW
    assert live["db"].state["decisions"]["new-0"] == {"decision":"page"}


def test_undo_refuses_absent_exact_provenance_or_later_hub_revision(live):
    assert not wiki.undo(slug="khipu")["ok"]
    wiki.run(now=NOW)
    after = live["path"].read_bytes()
    live["db"].revision("khipu", "elsewhere", "reconcile", "other-hash")
    out = wiki.undo(slug="khipu")
    assert out["skipped"][0]["reason"] == "page revised since wiki write"
    assert live["path"].read_bytes() == after
    assert not live["db"].state["holds"]


def test_report_counts_unique_episodes_for_two_pages_and_queue_age(live):
    other = live["root"]/"topics"/"other.md"
    other.write_bytes(OLD.replace(b"khipu",b"other"))
    live["episodes"][0]["topics"] = ["khipu","other"]
    out = wiki.run(now=NOW)
    assert out["pages_revised"] == 2 and out["episodes_absorbed"] == 1
    assert len(live["db"].state["writes"]) == 2
    live["db"].state["queue"][("other","2")] = {"slug":"other", "episode_id":"2", "queued_at":NOW-timedelta(days=4), "done_at":None}
    assert wiki._Store().queue_stats(NOW) == {"queue_remaining":1,"queue_oldest_age_days":4}


def test_report_search_page_age_uses_recent_top_topic_hits(live):
    from khipu.query_log import log_path
    wiki.run(now=NOW)
    for revision in live["db"].state["revisions"]:
        revision["at"] = NOW-timedelta(days=9)
    log_path().write_text(json.dumps({"ts":NOW.isoformat(), "top":[{"kind":"topic","id":"khipu"},{"kind":"episode","id":"1"}]})+"\n")
    assert wiki._Store().search_ages(NOW) == {"search_page_age_count":1, "search_page_age_median_days":9, "search_page_age_p90_days":9}


def test_doctor_one_wiki_line_only_enabled_stalled_and_has_queue(live):
    jobs._write_wiki_last({"pages_revised":0,"pages_opened":0,"queue_remaining":2})
    assert not any(s.startswith("Wiki:") for s in doctor_summary.health_summary({}))
    config.set_choice_setting("wiki_nightly","true")
    assert sum(s.startswith("Wiki:") for s in doctor_summary.health_summary({})) == 1
    for counts in ({"pages_revised":1,"queue_remaining":2}, {"pages_opened":1,"queue_remaining":2}, {"pages_revised":0,"queue_remaining":0}):
        jobs._write_wiki_last(counts)
        assert not any(s.startswith("Wiki:") for s in doctor_summary.health_summary({}))


def test_not_hub_memory_host_never_calls_provider_or_db(live, monkeypatch):
    monkeypatch.setenv("KHIPU_GIT_SYNC_HOST","0")
    monkeypatch.setattr(wiki,"load_inputs",lambda **k:pytest.fail("not host"))
    assert wiki.run()["skipped"] == "not the hub/memory nightly Mac"
    assert not live["prompts"]


def test_held_priority_page_does_not_starve_other_page_at_max_one(live):
    other = live["root"]/"topics"/"other.md"
    other.write_bytes(OLD.replace(b"khipu",b"other"))
    live["episodes"].append(ep(2,"other"))
    live["db"].state["holds"]["khipu"] = NOW+timedelta(days=7)
    live["db"].state["queue"][("khipu","1")] = {"slug":"khipu", "episode_id":"1", "queued_at":NOW-timedelta(days=4), "done_at":None}
    out = wiki.run(max_pages=1,now=NOW)
    assert out["pages_revised"] == 1 and out["written"][0]["slug"] == "other"
    assert out["held_pages"] == 1 and live["path"].read_bytes() == OLD
    assert out["queue_remaining"] == 1


def test_wiki_setup_failure_does_not_stop_later_nightly_steps(live, monkeypatch):
    events = []
    def broken():
        raise RuntimeError("broken config")
    monkeypatch.setattr(wiki,"enabled",broken)
    for name in ("_drain_sessions", "_capture_export", "_embed_backfill", "_summary_kind_backfill", "_prune_query_cache",
                 "_prune_routing_ledgers", "_storage_sweep", "_provenance_nightly", "_fact_changes_prune",
                 "_ledger_nightly", "_receipt_prune", "_project_renames_sweep", "_recall_eval_step", "_refresh_snapshot"):
        monkeypatch.setattr(jobs,name,lambda n=name:(events.append(n) or {"ok":True}))
    monkeypatch.setattr(jobs,"_run_script",lambda *a,**k:(events.append("consolidate") or 0))
    assert jobs._run_nightly_locked() == 0
    receipt = jobs.nightly_last()
    steps = {s["name"]:s for s in receipt["steps"]}
    assert not steps["wiki_update"]["ok"] and steps["wiki_update"]["error"] == "RuntimeError"
    names = [s["name"] for s in receipt["steps"]]
    assert names.index("capture_export") < names.index("wiki_update") < names.index("consolidate_nightly")
    assert "consolidate" in events and events[-1] == "_refresh_snapshot"


def test_existing_replica_timer_reaches_wiki_even_when_replica_stage_fails(live, monkeypatch, capsys):
    from khipu import macs, owner_background, owner_direction, replica_heal, routing, storage
    monkeypatch.setattr(storage,"sweep_if_due",lambda:{"ok":True})
    monkeypatch.setattr(replica_heal,"run",lambda:{"ok":False,"error":"fake replica unavailable"})
    monkeypatch.setattr(owner_direction,"timer",lambda:{"ok":True})
    monkeypatch.setattr(owner_background,"heal",lambda:0)
    monkeypatch.setattr(routing,"poll_refold",lambda:{"ok":True})
    monkeypatch.setattr(macs,"refresh_running",lambda:[])
    monkeypatch.setattr(macs,"publish",lambda:{"ok":True})
    seen = []
    def timer(**kwargs):
        seen.append(kwargs)
        return {"ok":False,"error":"fake wiki failure"}
    monkeypatch.setattr(jobs,"run_wiki",timer)
    assert cli.main(["snapshot","heal"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["replica_heal"]["error"] == "fake replica unavailable"
    assert seen == [{"periodic":True}] and out["wiki_update"]["error"] == "fake wiki failure"


def test_config_cli_registers_switch_and_interval(live, capsys):
    assert cli.main(["config","--set","wiki_nightly","true"]) == 0
    assert wiki.enabled()
    assert cli.main(["config","--set","wiki_every_minutes","0"]) == 0
    assert config.minute_setting("wiki_every_minutes") == 0
    assert cli.main(["config","--set","wiki_every_minutes","-1"]) == 2


def test_wiki_plain_cli_names_written_and_refused_pages(live, capsys, monkeypatch):
    assert cli.main(["wiki","run","--max","1"]) == 0
    text = capsys.readouterr().out
    assert "khipu: written (1 episodes)" in text and "Wiki: 1 revised" in text
    assert 'khipu: 1 used, 0 skipped' in text and '1 episodes absorbed, 0 skipped' in text
    live["episodes"].append(ep(2))
    monkeypatch.setattr(extract,"generate_wiki",lambda *a,**k:json.dumps({"page":"bad"}))
    assert cli.main(["wiki","run"]) == 0
    assert "khipu: refused: draft checks failed" in capsys.readouterr().out


def test_provider_failure_keeps_page_and_queue_then_continues_next_page(live, monkeypatch):
    other = live["root"]/"topics"/"other.md"
    other.write_bytes(OLD.replace(b"khipu",b"other"))
    live["episodes"].append(ep(2,"other"))
    provider = live["provider"]
    def partly_fails(prompt, **kwargs):
        if '"slug": "khipu"' in prompt:
            embed._budget_take(scope="wiki")
            raise RuntimeError("fake login exhausted")
        return provider(prompt,**kwargs)
    monkeypatch.setattr(extract,"generate_wiki",partly_fails)
    out = wiki.run(now=NOW)
    assert not out["ok"] and out["pages_revised"] == 1
    assert out["written"][0]["slug"] == "other"
    assert out["rewrites_refused"] == 1 and out["calls_used"] == 2
    assert out["queue_remaining"] == 1 and live["path"].read_bytes() == OLD
    assert live["db"].state["queue"][("khipu","1")]["done_at"] is None
    assert out["failures"] == [{"slug":"khipu","stage":"provider","reason":"provider","error":"RuntimeError"}]


def test_opening_limit_is_per_run_with_shared_daily_call_ceiling(live):
    live["episodes"].clear()
    for n in range(12):
        live["episodes"].extend(ep(n*3+i, f"new-{n}") for i in range(1,4))
    first = jobs.run_wiki()
    assert first["pages_opened"] == 10
    second = jobs._wiki_update(force=True)
    assert second["pages_opened"] == 2 and second["calls_used"] == 2
    assert not second["cap_hit"] and second["queue_remaining"] == 0
    assert embed.budget_status("wiki")["calls"] == 13


def test_pending_new_page_survives_original_opening_window(live):
    live["episodes"].clear()
    live["episodes"].extend(ep(i,"new-page",ts=NOW-timedelta(days=9)) for i in range(1,4))
    for item in live["episodes"]:
        live["db"].state["queue"][("new-page",str(item["id"]))] = {
            "slug":"new-page", "episode_id":str(item["id"]), "queued_at":NOW-timedelta(days=8), "done_at":None}
    live["db"].state["decisions"]["new-page"] = {"decision":"page"}
    out = wiki.run(now=NOW)
    assert out["pages_opened"] == 1 and out["episodes_absorbed"] == 3
    assert out["queue_remaining"] == 0


def test_migration_exact_source_provenance_is_additive_and_not_applied():
    from khipu.migrate import available
    sql = dict(available())["0054_wiki_writes"].read_text()
    assert "reason TEXT" in sql
    assert "before_bytes BYTEA" in sql and "episode_ids BIGINT[]" in sql
    assert "REFERENCES topic_revisions(id)" in sql and "wiki_holds" in sql
    assert "lock_timeout = '5s'" in sql
    assert not any(text in sql.upper() for text in ("UPDATE TOPICS", "DELETE FROM", "DROP TABLE"))


def test_repeated_same_inputs_write_nothing_second_time(live):
    first=wiki.run(now=NOW)
    assert first["pages_revised"]==1
    before=live["path"].read_bytes()
    second=wiki.run(now=NOW)
    assert second["pages_revised"]==0 and second["calls_used"]==0
    assert second["episodes_absorbed"]==0 and second["queue_remaining"]==0
    assert live["path"].read_bytes()==before and len(live["db"].state["writes"])==1


def test_gate_decisions_persist_once_and_share_budget_with_writes(live):
    live["episodes"].extend(ep(i,"novel") for i in range(2,5))
    config.set_int_setting("wiki_daily_pages",1)
    first=wiki.run(now=NOW)
    assert first["ok"] and first["calls_used"]==1 and first["cap_hit"]
    assert not first["written"] and first["queue_remaining"]==4
    assert live["db"].state["decisions"]=={"novel":{"decision":"page"}}
    config.set_int_setting("wiki_daily_pages",10)
    second=wiki.run(now=NOW)
    assert second["pages_opened"]==1 and second["pages_revised"]==1
    assert sum('"candidates"' in p for p in live["prompts"])==1
    assert embed.budget_status("wiki")["calls"]==3


def test_hourly_never_gates_or_opens_pending_page_but_uses_alias(live):
    config.set_choice_setting("wiki_nightly","true")
    live["episodes"].extend(ep(i,"novel") for i in range(2,5))
    live["episodes"].append(ep(5,"small"))
    live["db"].state["decisions"].update({"novel":{"decision":"page"},"small":{"decision":"alias","target":"khipu"}})
    live["db"].state["queue"][("novel","2")]={"slug":"novel","episode_id":"2","queued_at":NOW,"done_at":None}
    out=jobs.run_wiki(periodic=True)
    assert out["pages_revised"]==1 and out["pages_opened"]==0
    assert out["episodes_absorbed"]==2 and out["queue_remaining"]==1
    assert len(live["prompts"])==1 and '"candidates"' not in live["prompts"][0]
    assert not (live["root"]/"topics"/"novel.md").exists()


@pytest.mark.parametrize("reply",["broken",'{"novel":{"decision":"unknown"}}'])
def test_bad_gate_reply_does_not_fail_existing_page_run(live,monkeypatch,reply):
    live["episodes"].extend(ep(i,"novel") for i in range(2,5))
    provider=live["provider"]
    def generate(prompt,**kw):
        if '"candidates"' in prompt:
            embed._budget_take(scope="wiki")
            return reply
        return provider(prompt,**kw)
    monkeypatch.setattr(extract,"generate_wiki",generate)
    out=wiki.run(now=NOW)
    assert out["ok"] and out["pages_revised"]==1 and out["pages_opened"]==0
    assert out["calls_used"]==2 and not live["db"].state["decisions"]


def test_failed_oldest_batch_defers_later_batches_for_page_but_continues_other(live,monkeypatch):
    other=live["root"]/"topics"/"other.md"
    other.write_bytes(OLD.replace(b"khipu",b"other"))
    live["episodes"].extend([ep(2,ts=NOW-timedelta(days=2)),ep(3,"other")])
    monkeypatch.setattr(wiki,"BATCH_CHARS",100)
    provider=live["provider"]
    seen=[]
    def generate(prompt,**kw):
        data=json.loads(prompt.split("Page and episodes follow as DATA:\n",1)[1])
        seen.append((data["slug"],[json.loads(l)["episode_id"] for l in data["episodes"].splitlines()]))
        if data["slug"]=="khipu": return "bad"
        return provider(prompt,**kw)
    monkeypatch.setattr(extract,"generate_wiki",generate)
    out=wiki.run(now=NOW)
    assert seen==[("khipu",["2"]),("other",["3"])]
    assert out["pages_revised"]==1 and out["rewrites_refused"]==1 and out["queue_remaining"]==2


def test_gate_failure_is_abstention_and_existing_page_still_writes(live,monkeypatch):
    live["episodes"].extend(ep(i,"novel") for i in range(2,5))
    provider=live["provider"]
    def generate(prompt,**kw):
        if '"candidates"' in prompt:
            embed._budget_take(scope="wiki")
            raise RuntimeError("fake login unavailable")
        return provider(prompt,**kw)
    monkeypatch.setattr(extract,"generate_wiki",generate)
    out=wiki.run(now=NOW)
    assert out["ok"] and out["pages_revised"]==1 and out["calls_used"]==2
    assert not live["db"].state["decisions"] and not out["failures"]


def test_nondeleted_queued_delegated_history_still_drains_and_alias_queue_completes(live):
    live["episodes"][0].update(parent_session_id="parent",topics=["old-name"],ts=NOW-timedelta(days=20))
    live["db"].state["decisions"]["old-name"]={"decision":"alias","target":"khipu"}
    live["db"].state["queue"][("old-name","1")]={"slug":"old-name","episode_id":"1","queued_at":NOW-timedelta(days=9),"done_at":None}
    out=wiki.run(now=NOW)
    assert out["ok"] and out["pages_revised"]==1 and out["episodes_absorbed"]==1
    assert out["queue_remaining"]==0 and live["db"].state["queue"][("old-name","1")]["done_at"]==NOW


def test_deleted_waiting_queue_drained_without_provider_or_page_write(live):
    live["episodes"][0]["deleted_at"]=NOW
    live["db"].deleted_ids.add("1")
    live["db"].state["queue"][("khipu","1")]={"slug":"khipu","episode_id":"1","queued_at":NOW,"done_at":None}
    out=wiki.run(now=NOW)
    assert out["deleted_queue_drained"]==1 and out["queue_remaining"]==0
    assert out["calls_used"]==0 and not live["prompts"] and live["path"].read_bytes()==OLD
    assert not live["db"].state["writes"]
    assert live['db'].state['queue'][('khipu','1')]['outcome']=='forgotten'


def test_check_hold_skips_second_run_and_expires_without_starving_other(live,monkeypatch):
    provider=live["provider"]
    monkeypatch.setattr(extract,"generate_wiki",lambda *a,**k:"bad")
    first=wiki.run(now=NOW)
    assert first["holds"]==[{"slug":"khipu","reason":"checks","until_at":(NOW+timedelta(hours=24)).isoformat()}]
    monkeypatch.setattr(extract,"generate_wiki",provider)
    other=live["root"]/"topics"/"other.md"
    other.write_bytes(OLD.replace(b"khipu",b"other"))
    live["episodes"].append(ep(2,"other"))
    second=wiki.run(now=NOW,max_pages=1)
    assert second["held_pages"]==1 and second["written"][0]["slug"]=="other"
    live["db"].now=NOW+timedelta(hours=24,seconds=1)
    third=wiki.run(now=live["db"].now)
    assert third["pages_revised"]==1 and third["written"][0]["slug"]=="khipu"


@pytest.mark.parametrize("stage",["source","provider","response","save"])
def test_stage_failures_hold_two_hours_and_do_not_log_raw_errors(live,monkeypatch,stage):
    secret="DO NOT LOG RAW ANSWER OR ERROR"
    if stage=="source":
        def fail(*a,**k): raise ValueError(secret)
        monkeypatch.setattr(wiki._Store,"baseline",fail)
    elif stage=="provider":
        def fail(*a,**k): raise RuntimeError(secret)
        monkeypatch.setattr(extract,"generate_wiki",fail)
    elif stage=="response": monkeypatch.setattr(extract,"generate_wiki",lambda *a,**k:{"page":secret})
    else:
        def fail(*a,**k): raise ValueError(secret)
        monkeypatch.setattr(wiki._Store,"save",fail)
    out=wiki.run(now=NOW)
    assert out["holds"]==[{"slug":"khipu","reason":stage,"until_at":(NOW+timedelta(hours=2)).isoformat()}]
    assert out["failures"][0]["stage"]==stage and secret not in json.dumps(out)
    assert not live["db"].state["writes"] and out["queue_remaining"]==1
    second=wiki.run(now=NOW)
    assert second["held_pages"]==1 and second["calls_used"]==0


def test_holds_never_shorten_existing_undo_or_refusal(live):
    store=wiki._Store()
    first=store.hold("khipu","undo",168,NOW)
    assert store.hold("khipu","checks",24,NOW)==first
    assert store.hold("khipu","provider",2,NOW)==first
    assert live["db"].state["hold_reasons"]["khipu"]=="undo"


def test_truncation_leaves_bytes_queue_and_reports_reason(live,monkeypatch):
    def truncated(*a,**k): raise extract.ProviderTruncated("secret answer")
    monkeypatch.setattr(extract,"generate_wiki",truncated)
    out=wiki.run(now=NOW)
    assert out["failures"][0]["reason"]=="truncated" and out["holds"][0]["reason"]=="truncated"
    assert out["queue_remaining"]==1 and live["path"].read_bytes()==OLD
    assert "secret answer" not in json.dumps(out)


def test_live_repairs_exact_line_and_reports_count_before_save(live,monkeypatch):
    provider=live["provider"]
    def changed(*a,**k): return provider(*a,**k).replace("Keep /tmp/wiki and 60 pages.","Keep /tmp/wiki and 60 pages!")
    monkeypatch.setattr(extract,"generate_wiki",changed)
    out=wiki.run(now=NOW)
    assert out["pages_revised"]==1 and out["repaired_protected_lines"]==1
    assert "Keep /tmp/wiki and 60 pages." in live["path"].read_text()


def test_periodic_limits_six_and_180_seconds_and_finishes_inflight(live,monkeypatch):
    config.set_choice_setting("wiki_nightly","true")
    for n in range(2,10):
        (live["root"]/"topics"/f"page-{n}.md").write_bytes(OLD.replace(b"khipu",f"page-{n}".encode()))
        live["episodes"].append(ep(n,f"page-{n}"))
    out=jobs.run_wiki(periodic=True,max_pages=40)
    assert out["pages_revised"]==6 and len(live["prompts"])==6
    # Fresh run: only one provider finishes, crossing the elapsed-time bound.
    for q in live["db"].state["queue"].values(): q["done_at"]=None
    elapsed=[0]
    monkeypatch.setattr(wiki.time,"monotonic",lambda:elapsed[0])
    provider=live["provider"]
    def slow(*a,**k):
        response=provider(*a,**k)
        elapsed[0]=181
        return response
    monkeypatch.setattr(extract,"generate_wiki",slow)
    bounded=wiki.run(now=NOW,max_pages=6,allow_open=False,time_budget_s=180)
    assert bounded["pages_revised"]==1 and bounded["time_budget_exhausted"]
    assert bounded["queue_remaining"]==8


def test_undo_opened_page_completes_even_unabsorbed_pending_rows(live):
    live["episodes"].extend(ep(i,"novel") for i in range(2,5))
    out=wiki.run(now=NOW)
    live["db"].state["queue"][("novel","999")]={"slug":"novel","episode_id":"999","queued_at":NOW,"done_at":None}
    result=wiki.undo(slug="novel")
    assert result["pages_retired"]==1 and result["episodes_requeued"]==0
    assert all(q["done_at"] for q in live["db"].state["queue"].values())
    assert live["db"].state["hold_reasons"]["novel"]=="undo"
    assert wiki.run(now=NOW)["queue_remaining"]==0
    assert live['db'].state['queue'][('novel','999')]['outcome']=='skipped'


def test_slug_cli_read_set_alias_tag_page_clear_and_target_validation(live,capsys):
    assert cli.main(["wiki","slug","unknown","--json"])==0
    assert json.loads(capsys.readouterr().out)["decision"] is None
    for choice in ("page","tag"):
        assert cli.main(["wiki","slug","unknown","--set",choice,"--json"])==0
        assert json.loads(capsys.readouterr().out)["decision"]=={"decision":choice}
    assert cli.main(["wiki","slug","unknown","--set","alias","--target","khipu","--json"])==2 # not reconciled yet
    capsys.readouterr()
    wiki.run(now=NOW)
    for target in ("missing","note:foo"):
        assert cli.main(["wiki","slug","unknown","--set","alias","--target",target,"--json"])==2
        capsys.readouterr()
    live["db"].state["topics"]["archived"]={"slug":"archived","body":"","status":"archived"}
    assert cli.main(["wiki","slug","unknown","--set","alias","--target","archived","--json"])==2
    capsys.readouterr()
    assert cli.main(["wiki","slug","unknown","--set","alias","--target","khipu","--json"])==0
    assert json.loads(capsys.readouterr().out)["decision"]=={"decision":"alias","target":"khipu"}
    assert cli.main(["wiki","slug","unknown","--set","alias","--json"])==2
    capsys.readouterr()
    assert cli.main(["wiki","slug","unknown","--clear","--json"])==0
    assert json.loads(capsys.readouterr().out)["decision"] is None
    assert "unknown" not in live["db"].state["decisions"]
    assert cli.main(["wiki","slug","note:foo","--json"])==2
    capsys.readouterr()


def test_run_reports_gate_decisions_stored_in_that_run(live,capsys):
    live["episodes"].extend(ep(i,"novel") for i in range(2,5))
    live["db"].state["decisions"]["existing-tag"]={"decision":"tag"}
    assert cli.main(["wiki","run"])==0
    text=capsys.readouterr().out
    assert 'Stored slug decisions: {"novel": {"decision": "page"}}' in text
    assert wiki.run(now=NOW)["slug_decisions"]=={}


def test_archived_prefixed_source_never_reaches_provider(live):
    live["path"].write_bytes(b"<!-- prefix -->\n"+OLD.replace(b"status: seed",b"status: archived"))
    out=wiki.run(now=NOW)
    assert out["calls_used"]==0 and not live["prompts"] and not out["written"]


def test_partial_skips_transactional_outcomes_report_and_undo_written_only(live,monkeypatch):
    live['episodes'].append(ep(2))
    provider=live['provider']
    monkeypatch.setattr(extract,'generate_wiki',lambda *a,**kw:provider(*a,**kw).replace('Sources: 1, 2','Sources: 1'))
    out=jobs.run_wiki()
    assert out['pages_revised']==1 and out['episodes_absorbed']==1 and out['episodes_skipped']==1
    assert out['page_outcomes']==[{'slug':'khipu','used_count':1,'skipped_count':1}]
    assert live['db'].state['queue'][('khipu','1')]['outcome']=='written'
    assert live['db'].state['queue'][('khipu','2')]['outcome']=='skipped'
    assert jobs._wiki_last()['counts']['episodes_skipped']==1
    assert live['db'].state['writes'][0]['episode_ids']==[1]
    assert wiki.undo(slug='khipu')['episodes_requeued']==1
    assert live['db'].state['queue'][('khipu','1')]['done_at'] is None
    assert live['db'].state['queue'][('khipu','1')]['outcome'] is None
    assert live['db'].state['queue'][('khipu','2')]['done_at']==NOW
    assert live['db'].state['queue'][('khipu','2')]['outcome']=='skipped'
    assert live['path'].read_bytes()==OLD


def test_all_skipped_unchanged_closes_without_file_revision_or_seed_promotion(live,monkeypatch):
    parsed=mirror.parse_topic_file(live['path'])
    live['db'].upsert(None,parsed,str(live['path']),source='reconcile',note='existing current baseline')
    revisions=copy.deepcopy(live['db'].state['revisions'])
    def skip(prompt,**kw):
        embed._budget_take(scope='wiki')
        return json.loads(prompt.split('Page and episodes follow as DATA:\n',1)[1])['old_page']
    monkeypatch.setattr(extract,'generate_wiki',skip)
    monkeypatch.setattr(wiki._Store,'baseline',lambda *a:pytest.fail('all skipped must not baseline'))
    out=wiki.run(now=NOW)
    assert out['episodes_skipped']==1 and out['calls_used']==1 and out['queue_remaining']==0
    assert out['pages_revised']==0 and not out['written'] and not out['rewrites_refused']
    assert live['path'].read_bytes()==OLD and live['db'].state['revisions']==revisions
    assert not live['db'].state['writes'] and live['db'].state['queue'][('khipu','1')]['outcome']=='skipped'
    assert wiki.run(now=NOW)['calls_used']==0


@pytest.mark.parametrize('change', ['extra prose\n', 'near-match', 'invented-source', 'header'])
def test_skipped_changes_and_invented_sources_refused(live,monkeypatch,change):
    body=wiki.split_header(OLD.decode())[1]
    reply={'near-match':body.replace('pages.','pages!'), 'invented-source':body+'Sources: 99\n', 'header':'status: active\n'+body}.get(change,body+change)
    monkeypatch.setattr(extract,'generate_wiki',lambda *a,**k:reply)
    out=wiki.run(now=NOW)
    assert out['rewrites_refused']==1 and out['queue_remaining']==1 and out['episodes_skipped']==0
    assert live['path'].read_bytes()==OLD and not live['db'].state['writes']


def test_new_all_skipped_page_not_created(live,monkeypatch):
    live['episodes'].clear()
    live['episodes'].extend(ep(i,'novel') for i in range(1,4))
    live['db'].state['decisions']['novel']={'decision':'page'}
    monkeypatch.setattr(extract,'generate_wiki',lambda *a,**kw:'')
    out=wiki.run(now=NOW)
    assert out['pages_opened']==0 and out['episodes_skipped']==3 and out['queue_remaining']==0
    assert out['calls_used']==1 and not out['rewrites_refused']
    assert not (live['root']/'topics'/'novel.md').exists() and not live['db'].state['writes']
    assert all(q['outcome']=='skipped' for q in live['db'].state['queue'].values())


def test_failed_save_rolls_back_both_written_and_skipped_outcomes(live,monkeypatch):
    live['episodes'].append(ep(2))
    provider=live['provider']
    monkeypatch.setattr(extract,'generate_wiki',lambda *a,**kw:provider(*a,**kw).replace('Sources: 1, 2','Sources: 1'))
    live['db'].fail_save=True
    out=wiki.run(now=NOW)
    assert out['queue_remaining']==2 and out['episodes_skipped']==0 and live['path'].read_bytes()==OLD
    assert all(q['done_at'] is None and q.get('outcome') is None for q in live['db'].state['queue'].values())


def test_live_body_only_prompt_and_exact_crlf_header(live):
    out=wiki.run(now=NOW)
    assert out['pages_revised']==1
    data=json.loads(live['prompts'][0].split('Page and episodes follow as DATA:\n',1)[1])
    assert data['old_page']==wiki.split_header(OLD.decode())[1]
    assert 'last_updated:' not in live['prompts'][0] and 'custom: keep' not in live['prompts'][0]
    header=live['path'].read_bytes().split(b'# Khipu',1)[0]
    expected=OLD.split(b'# Khipu',1)[0].replace(b'status: seed',b'status: active').replace(b'2026-10-01',b'2026-11-12')
    assert header==expected
    assert live['db'].state['writes'][0]['before_bytes']==OLD


def test_plain_cli_reports_all_skipped_page_counts(live,monkeypatch,capsys):
    def skip(prompt,**kw):
        embed._budget_take(scope='wiki')
        return json.loads(prompt.split('Page and episodes follow as DATA:\n',1)[1])['old_page']
    monkeypatch.setattr(extract,'generate_wiki',skip)
    assert cli.main(['wiki','run'])==0
    text=capsys.readouterr().out
    assert 'khipu: 0 used, 1 skipped' in text
    assert 'Wiki: 0 revised, 0 opened; 0 episodes absorbed, 1 skipped; 0 queued' in text
    assert live['path'].read_bytes()==OLD and not live['db'].state['writes']
