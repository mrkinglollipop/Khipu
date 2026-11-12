"""Hermetic draft groundwork: no provider, hub, actual memory or home access."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from khipu import cli, config, embed, extract, models, wiki_nightly as wiki

NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
TODAY = NOW.date().isoformat()
OLD = f"---\nslug: khipu\ntitle: Khipu\nstatus: seed\nlast_updated: 2026-10-01\ncustom: preserve\n---\n# Khipu\nLives at /tmp/khipu with 60 pages.\nA durable wiki.\n"
NEW = OLD.replace("status: seed", "status: active").replace("last_updated: 2026-10-01", f"last_updated: {TODAY}") + "Sources: 1\n"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KHIPU_MEMORY_ROOT", str(tmp_path / "memory"))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    for key in ("KHIPU_DATABASE_URL", "ALZY_DATABASE_URL", "GEMINI_API_KEY", "CLAUDE_CONFIG_DIR", "GROK_HOME", "KHIPU_EXTRACT_MODEL", "KHIPU_WIKI_DAILY_PAGES"):
        monkeypatch.delenv(key, raising=False)
    # Any unmocked provider/DB use is a test failure, never a network attempt.
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr("khipu.db.connect", lambda **k: pytest.fail("hub forbidden"))


def episode(i=1, topics=None, days=0, session=None, **extra):
    return {"id": i, "ts": NOW-timedelta(days=days), "ingested_at": NOW-timedelta(days=days),
            "session_id": session or f"s{i}", "summary": "A summary", "topics": topics or ["khipu"],
            "decisions": ["keep wiki"], "preferences": ["short prose"], **extra}


def page(slug="khipu", **extra):
    return {"slug": slug, "body": "", "status": "active", "frontmatter": {}, **extra}


def assign(eps, pages=None, **kwargs):
    return wiki.assign(eps, [page()] if pages is None else pages, now=NOW, **kwargs)


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


def test_exact_max_two_and_alias_reasons():
    out = assign([episode(topics=["khipu", "two", "three"])], [page(s) for s in ["khipu", "two", "three"]])
    assert [r["slug"] for r in out["assignments"]] == ["khipu", "two"]
    assert all(r["reason"] == "exact" for r in out["assignments"])
    out = assign([episode(topics=["khipu-small"])], decisions={"khipu-small":{"decision":"alias","target":"khipu"}})
    assert out["assignments"] == [{"slug":"khipu","episode_id":"1","reason":"alias"}]

def test_skips_retired_notes_deleted_and_superseded_redirects():
    pages = [page(), page("old", status="superseded", superseded_by="khipu"),
             page("retired", status="retired"), page("note:foo"), page("gone", deleted_at=NOW)]
    out = assign([episode(topics=["old", "retired", "note:foo", "gone"])], pages)
    assert out["assignments"] == [{"slug":"khipu","episode_id":"1","reason":"redirect"}]
    assert assign([episode(topics=["none"])])["unmatched"] == ["1"]

def test_redirect_body_convention_chains_and_cycles():
    pages = [page(), page("old", status="superseded", body="Replaced by [[khipu]]"),
             page("cycle", status="superseded", superseded_by="cycle")]
    assert assign([episode(topics=["old"])], pages)["assignments"][0]["slug"] == "khipu"
    assert not assign([episode(topics=["cycle"])], pages)["assignments"]


def test_named_slug_assigns_immediately_without_vector_data():
    assert assign([episode()])["assignments"] == [{"slug":"khipu","episode_id":"1","reason":"exact"}]
    # A semantically similar summary alone cannot supply a named page.
    out = assign([episode(topics=["unrelated"], summary="Khipu wiki work")])
    assert out["unmatched"] == ["1"] and not out["pages"]
    assert not {"blocked", "threshold", "no_vector_count", "waiting_for_vectors", "vector_coverage_by_day"} & out.keys()


def test_open_requires_approval_three_episodes_two_sessions_seven_days():
    eps = [episode(i,topics=["novel"],session="a" if i<3 else "b") for i in range(1,4)]
    decisions = {"novel":{"decision":"page"}}
    assert not assign(eps)["opens"]
    assert assign(eps,decisions=decisions)["opens"] == [{"slug":"novel","episode_ids":["1","2","3"]}]
    assert not assign(eps[:2],decisions=decisions)["opens"]
    assert not assign([dict(e,session_id="one") for e in eps],decisions=decisions)["opens"]
    assert not assign([dict(e,ts=NOW-timedelta(days=8)) for e in eps],decisions=decisions)["opens"]
    assert not assign(eps,decisions={"novel":{"decision":"tag"}})["opens"]

def test_open_cap_ten():
    eps = [episode(i*3+j,topics=[f"new-{i}"]) for i in range(12) for j in range(3)]
    assert len(assign(eps,decisions={f"new-{i}":{"decision":"page"} for i in range(12)})["opens"]) == 10


def test_queue_priorities_completed_and_cap():
    eps = [episode(i, topics=[f"page-{i}"]) for i in range(70)]
    pages = [page(f"page-{i}") for i in range(70)]
    q = [{"slug":"page-69", "episode_id":69,"queued_at":NOW-timedelta(days=3), "done_at":None},
         {"slug":"page-0", "episode_id":0,"queued_at":NOW,"done_at":NOW}]
    out = assign(eps,pages,queue=q)
    assert len(out["pages"]) == 60
    assert out["pages"][0]["slug"] == "page-69"
    assert "page-0" not in [r["slug"] for r in out["pages"]]
    assert out["calls_waiting"] == 9


def test_most_waiting_then_episode_batches_each_count(monkeypatch):
    eps = [episode(i,topics=["khipu" if i<4 else "other"]) for i in range(1,5)]
    assert assign(eps,[page(),page("other")])["pages"][0]["slug"] == "khipu"
    monkeypatch.setattr(wiki,"BATCH_CHARS",100)
    out = assign(eps,[page(),page("other")],cap=2)
    assert len(out["pages"]) == 2 and out["calls_waiting"] == 2


def test_checks_good_draft_and_dated_was():
    assert wiki.check_draft("khipu",OLD,NEW,["1"],today=TODAY)["passed"]
    line = "Lives at /tmp/khipu with 60 pages."
    changed = NEW.replace(line, f"Was {line} until 2026-11-12; now elsewhere.")
    assert wiki.check_draft("khipu",OLD,changed,["1"],today=TODAY)["passed"]


@pytest.mark.parametrize("change,expected", [
    (lambda s:s.replace("slug: khipu","slug: other"), "frontmatter changed"),
    (lambda s:s.replace("custom: preserve","custom: gone"), "frontmatter changed"),
    (lambda s:s.replace("status: active","status: retired"), "status must"),
    (lambda s:s.replace(f"last_updated: {TODAY}","last_updated: 2000-01-01"), "last_updated"),
    (lambda s:s.replace("Lives at /tmp/khipu with 60 pages.", "Changed."), "protected old line"),
    (lambda s:s.replace("Sources: 1","Sources: 11"), "Sources missing"),
    (lambda s:"tiny", "70%"),
])
def test_check_refusals(change,expected):
    checks = wiki.check_draft("khipu",OLD,change(NEW),["1"],today=TODAY)
    assert not checks["passed"] and any(expected in e for e in checks["errors"])


def test_fact_truth_limitation_explicit():
    assert "factual truth" in wiki.check_draft("khipu",OLD,NEW,["1"],today=TODAY)["limitation"]


def test_wiki_role_inherits_and_legacy_desktop_round_trip_preserves_explicit():
    models.set_models_merge_role("synth", provider="claude-cli",model_id="haiku")
    assert models.wiki_settings()["model_id"] == "haiku"
    legacy = {r:models.show_models()[r] for r in models.LEGACY_ROLES}
    models.set_models_replace(legacy)
    assert "wiki" not in config.load_config()["models"]
    models.set_models_merge_role("synth",provider="claude-cli",model_id="sonnet")
    assert models.wiki_settings()["model_id"] == "sonnet"
    models.set_models_merge_role("wiki",provider="claude-cli",model_id="haiku")
    models.set_models_replace(legacy)
    assert models.wiki_settings()["model_id"] == "haiku"
    assert config.load_config()["models"]["wiki"]["provider"] == "claude-cli"


def test_wiki_budget_default_lift_and_synth_unchanged():
    assert embed._budget_cap("wiki") == (60,"default")
    config.set_int_setting("wiki_daily_pages",1)
    embed._budget_take(scope="wiki")
    with pytest.raises(embed.BudgetExhausted):
        embed._budget_take(scope="wiki")
    assert embed.budget_status("wiki")["calls"] == 1
    assert embed.budget_status("synth")["calls"] == 0
    assert "wiki" in cli.BUDGET_LIFT_SCOPES
    assert cli.main(["config","--set","wiki_daily_pages","3"]) == 0
    assert embed._budget_cap("wiki") == (3,"config")


def test_fake_adapter_model_override_budget_and_login_exhaustion_no_fallback(monkeypatch):
    models.set_models_merge_role("wiki",provider="claude-cli",model_id="haiku")
    monkeypatch.setenv("KHIPU_EXTRACT_MODEL","wrong-model")
    seen=[]
    def provider(prompt,**kwargs):
        extract._synth_budget_take()
        seen.append(kwargs)
        extract._record_usage({"usage":{"input_tokens":10,"output_tokens":5}},provider="claude-cli",model_id=kwargs["model_id"],caller=kwargs["caller"])
        return '{"page":"draft"}'
    monkeypatch.setattr(extract,"_generate_claude_cli",provider)
    assert extract.generate_wiki("safe",model_id="sonnet") == '{"page":"draft"}'
    assert seen[0]["model_id"] == "sonnet" and seen[0]["retries"] == 0
    assert embed.budget_status("wiki")["calls"] == 1
    assert embed.budget_status("synth")["calls"] == 0
    assert not extract.synth_usage_path().exists()
    assert extract.synth_usage_path().with_name("wiki-usage.jsonl").exists()
    def exhausted(*a,**k): raise RuntimeError("login exhausted")
    monkeypatch.setattr(extract,"_generate_claude_cli",exhausted)
    monkeypatch.setattr(extract,"_generate_gemini",lambda *a,**k:pytest.fail("paid fallback"))
    with pytest.raises(RuntimeError,match="login exhausted"):
        extract.generate_wiki("safe")


class Cursor:
    def __init__(self, migrated=False):
        self.migrated=migrated
        self.statements=[]
    def __enter__(self): return self
    def __exit__(self,*args): return False
    def execute(self,sql,params=None):
        self.statements.append((sql,params))
        assert sql.lstrip().upper().startswith(("SELECT","SET TRANSACTION READ ONLY"))
        self.sql=sql
    def fetchone(self):
        if "to_regclass" in self.sql:
            return ("wiki_queue", "wiki_slugs") if self.migrated else (None, None)
        return (0,)
    def fetchall(self):
        if "FROM topics" in self.sql: return [("khipu","body","active",{},None,None,None)]
        if "FROM episodes e" in self.sql:
            return [(1,NOW,"s1","summary",["khipu"],[],[],NOW,None,None)]
        return []


@pytest.mark.parametrize("migrated",[False,True])
def test_plan_read_only_before_after_migration_and_cli_no_home_changes(tmp_path,monkeypatch,migrated,capsys):
    cur=Cursor(migrated)
    class Conn:
        def __enter__(self): return self
        def __exit__(self,*args): return False
        def cursor(self): return cur
    def connect(**kwargs):
        assert kwargs["record_reachability"] is False
        return Conn()
    monkeypatch.setattr("khipu.db.connect",connect)
    # Pending account recovery would mutate config absent read_only_config.
    config.config_file().parent.mkdir(parents=True)
    config.config_file().write_text(json.dumps({"account_change_open":{"test":True}}))
    monkeypatch.setattr(config,"_settle_account_change",lambda *a:pytest.fail("config recovery"))
    before=snapshot(tmp_path)
    assert cli.main(["wiki","plan","--json","--since","30"]) == 0
    out=json.loads(capsys.readouterr().out)
    assert out["read_only"] and out["pages"][0]["slug"] == "khipu"
    assert snapshot(tmp_path)==before
    assert cur.statements[0][0] == "SET TRANSACTION READ ONLY"


def test_draft_artifacts_exact_old_no_transcript_no_page_or_queue_writes(tmp_path,monkeypatch):
    memory=tmp_path/"memory"
    memory.mkdir()
    source=memory/"khipu.md"
    source.write_text(OLD)
    eps=[episode(raw="SECRET TRANSCRIPT",transcript="SECRET TRANSCRIPT",verbatim="SECRET TRANSCRIPT")]
    monkeypatch.setattr(wiki,"load_inputs",lambda **k:(eps,[page(source_path=str(source))],{},[],0))
    seen=[]
    def provider(prompt,**kwargs):
        seen.append(prompt)
        return json.dumps({"page":NEW.replace(TODAY,datetime.now(timezone.utc).date().isoformat())})
    monkeypatch.setattr(extract,"generate_wiki",provider)
    before=snapshot(memory)
    out=wiki.draft(sample=1,model_id="fake",out=tmp_path/"output")
    assert out["passed"] == 1 and len(seen)==1
    assert "SECRET TRANSCRIPT" not in seen[0]
    assert snapshot(memory)==before
    folder=next((tmp_path/"output").glob("01-*"))
    assert (folder/"old.md").read_text() == OLD
    assert {p.name for p in folder.iterdir()} == {"old.md","draft.md","checks.json","episodes.jsonl"}
    assert "SECRET TRANSCRIPT" not in (folder/"episodes.jsonl").read_text()


def test_draft_memory_and_symlink_destinations_rejected_before_provider(tmp_path,monkeypatch):
    memory=tmp_path/"memory"
    memory.mkdir()
    alias=tmp_path/"alias"
    alias.symlink_to(memory,target_is_directory=True)
    monkeypatch.setattr(wiki,"load_inputs",lambda **k:pytest.fail("must reject before planning"))
    for out in (memory,memory/"inside",alias/"inside"):
        with pytest.raises(ValueError,match="outside memory_root"):
            wiki.draft(sample=1,out=out)


def test_draft_gate_artifacts_never_persist_decisions(tmp_path,monkeypatch):
    eps = [episode(i,topics=["novel"],raw="SECRET",transcript="SECRET") for i in range(1,4)]
    monkeypatch.setattr(wiki,"load_inputs",lambda **k:(eps,[],{},[],7))
    seen=[]
    def provider(prompt,**kw):
        seen.append(prompt)
        return json.dumps({"novel":{"decision":"tag"}})
    monkeypatch.setattr(extract,"generate_wiki",provider)
    monkeypatch.setattr(wiki._Store,"decide",lambda *a:pytest.fail("draft persisted decisions"))
    out=wiki.draft(sample=1,out=tmp_path/"output")
    assert len(seen)==1 and "SECRET" not in seen[0]
    assert out["plan"]["slug_decisions"] == {"novel":{"decision":"tag"}}
    assert out["plan"]["delegated_excluded"] == 7 and not out["drafts"]
    assert json.loads((tmp_path/"output"/"slug-decisions.json").read_text()) == {"novel":{"decision":"tag"}}

def test_migration_discovery_shape():
    from khipu.migrate import migrations_dir
    # Discovery is file-only; no migration is applied by these tests.
    files=list(migrations_dir().glob("*.sql"))
    assert any("0053_wiki_queue" in str(p) for p in files)


def test_opens_never_exceed_two_pages_per_episode():
    eps=[episode(i,topics=["new-a","new-b","new-c"]) for i in range(1,4)]
    out=assign(eps,decisions={s:{"decision":"page"} for s in ["new-a","new-b","new-c"]})
    assert len(out["opens"]) == 2
    counts={str(e["id"]):0 for e in eps}
    for p in out["pages"]:
        for e in p["episodes"]: counts[e["episode_id"]]+=1
    assert max(counts.values()) == 2


def test_queued_assignments_share_two_page_cap_and_keep_alias_reason():
    eps=[episode(topics=["b","c"])]
    pages=[page(s) for s in ["a","b","c"]]
    q=[{"slug":"alias-a","episode_id":1,"queued_at":NOW-timedelta(days=3),"done_at":None}]
    out=assign(eps,pages,queue=q,decisions={"alias-a":{"decision":"alias","target":"a"}})
    assert len(out["pages"]) == 2
    assert out["pages"][0]["episodes"][0]["reason"] == "alias"

def test_draft_reports_missing_source_and_continues(tmp_path,monkeypatch):
    eps=[episode(1),episode(2,topics=["other"])]
    source=tmp_path/"other.md"
    source.write_text(OLD.replace("khipu","other"))
    monkeypatch.setattr(wiki,"load_inputs",lambda **k:(eps,[page(),page("other",source_path=str(source))],{},[],0))
    monkeypatch.setattr(extract,"generate_wiki",lambda *a,**k:json.dumps({"page":NEW.replace("khipu","other").replace("Sources: 1","Sources: 2").replace(TODAY,datetime.now(timezone.utc).date().isoformat())}))
    out=wiki.draft(sample=2,out=tmp_path/"output")
    assert out["drafts"][0]["reason"] == "source file unavailable; cannot validate frontmatter"
    assert len(out["drafts"]) == 2 and out["drafts"][1]["passed"]


def test_draft_sample_counts_distinct_pages(tmp_path,monkeypatch):
    eps=[episode(i) for i in range(3)]
    source=tmp_path/"source.md"
    source.write_text(OLD)
    monkeypatch.setattr(wiki,"BATCH_CHARS",100)
    monkeypatch.setattr(wiki,"load_inputs",lambda **k:(eps,[page(source_path=str(source))],{},[],0))
    seen=[]
    monkeypatch.setattr(extract,"generate_wiki",lambda *a,**k:(seen.append(1) or json.dumps({"page":NEW})))
    out=wiki.draft(sample=3,out=tmp_path/"output")
    assert len(seen)==1 and len(out["drafts"])==1
    assert "additional batches remain" in out["sampling"]


def test_episode_order_uses_timestamp_then_numeric_id_even_queued():
    eps=[episode("10",days=1),episode("2",days=1),episode("1",days=4),episode("3")]
    queue=[{"slug":"khipu","episode_id":"3","queued_at":NOW-timedelta(days=5),"done_at":None}]
    out=assign(eps,queue=queue)
    assert [r["episode_id"] for r in out["pages"][0]["episodes"]] == ["1","2","10","3"]

def test_check_preserves_wiki_links_and_relative_file_paths():
    for line in ("See [[accounts]].", "Read wiki_nightly.py."):
        old=OLD+line+"\n"
        checks=wiki.check_draft("khipu",old,NEW,["1"],today=TODAY)
        assert not checks["passed"]
        assert not checks["checks"]["protected_lines"]["passed"]


def test_cli_wiki_budget_lift_is_independent_and_configured(monkeypatch,capsys):
    monkeypatch.setattr("khipu.session_capture._log",lambda *a:None)
    assert cli.main(["budget","lift","wiki","--cap","70"]) == 0
    out=json.loads(capsys.readouterr().out)
    assert out["wiki_daily_pages"] == 70 and out["budget"]["cap"] == 70
    assert embed._budget_cap("synth")[0] != 70


def test_migration_is_additive_registered_and_bounded():
    from khipu.migrate import available
    path=dict(available())["0053_wiki_queue"]
    sql=path.read_text()
    assert "CREATE TABLE IF NOT EXISTS wiki_queue" in sql
    assert "PRIMARY KEY (slug, episode_id)" in sql
    assert "REFERENCES episodes(id)" in sql and "done_at TIMESTAMPTZ" in sql
    assert "lock_timeout = '5s'" in sql and "ON CONFLICT (version) DO NOTHING" in sql
    assert not any(word in sql.upper() for word in ("UPDATE TOPICS", "INSERT INTO TOPICS", "DELETE FROM", "DROP TABLE"))


def test_approved_slug_opens_alongside_exact_page_with_free_second_slot():
    eps=[episode(i,topics=["khipu","novel"],session="a" if i<3 else "b") for i in range(1,4)]
    decisions={"novel":{"decision":"page"}}
    out=assign(eps,decisions=decisions)
    assert out["opens"] == [{"slug":"novel","episode_ids":["1","2","3"]}]
    for eid in ("1","2","3"):
        assigned=[r for r in out["assignments"] if r["episode_id"] == eid]
        assert {(r["slug"],r["reason"]) for r in assigned} == {("khipu","exact"),("novel","open")}
    full=[dict(e,topics=["khipu","other","novel"]) for e in eps]
    assert not assign(full,[page(),page("other")],decisions=decisions)["opens"]

def test_candidates_order_sessions_then_episodes_then_slug_and_reserve_all_pages():
    eps=[]
    for slug, count, sessions in [("busy",6,2),("diverse",4,4),("alpha",3,2),("zeta",3,2),("retired",3,3),("tag",3,3)]:
        eps.extend(episode(100*len(eps)+i,topics=[slug],session=f"s{i%sessions}") for i in range(count))
    candidates=wiki.slug_candidates(eps,[page("retired",status="retired")],{"tag":{"decision":"tag"}},now=NOW)
    assert [c["slug"] for c in candidates] == ["diverse","busy","alpha","zeta"]
    assert all(c["session_count"]>=2 and len(c["recent_episodes"])==2 for c in candidates)
    assert not wiki.slug_candidates([dict(e,parent_session_id="parent") for e in eps],[],{},now=NOW)


def test_gate_one_call_first_thirty_allowlisted_examples_and_strict_replies():
    eps=[episode(i,topics=[f"new-{n}" for n in range(31)],raw="SECRET",transcript="SECRET") for i in (2,10,11)]
    candidates=wiki.slug_candidates(eps,[],{},now=NOW)
    pages=[page("a",frontmatter={"title":"The A page"}),page("retired",status="retired")]
    seen=[]
    def generate(prompt):
        seen.append(prompt)
        return json.dumps({"new-0":{"decision":"page"},"new-1":{"decision":"tag"},
            "new-10":{"decision":"alias","target":"a"},"new-11":{"decision":"alias","target":"retired"},
            "new-12":{"decision":"alias","target":"unknown"},"new-13":{"decision":[]},
            "new-14":{"decision":{}},"unknown":{"decision":"page"},"new-9":{"decision":"page"}})
    out=wiki.decide_slugs(candidates,pages,generate=generate)
    assert out == {"new-0":{"decision":"page"},"new-1":{"decision":"tag"},"new-10":{"decision":"alias","target":"a"}}
    assert len(seen)==1 and "SECRET" not in seen[0]
    data=json.loads(seen[0].split("\n",1)[1])
    assert len(data["candidates"])==30
    assert [r["episode_id"] for r in data["candidates"][0]["recent_episodes"]]==["11","10"]
    assert data["eligible_pages"] == [{"slug":"a","title":"The A page"}]
    assert set(data["candidates"][0]["recent_episodes"][0]) == {"date","episode_id","summary","decisions","preferences"}
    for reply in ("broken", "[]", '{"new-0":{"decision":"unknown"}}'):
        assert wiki.decide_slugs(candidates,pages,generate=lambda p:reply)=={}
    def broken(prompt): raise RuntimeError("fake exhausted")
    assert wiki.decide_slugs(candidates,pages,generate=broken)=={}


def test_plan_shows_would_ask_and_stored_decisions_without_provider_or_writes(monkeypatch):
    eps=[episode(i,topics=["novel","known"]) for i in range(1,4)]
    decisions={"known":{"decision":"tag"}}
    monkeypatch.setattr(wiki,"load_inputs",lambda **k:(eps,[],decisions,[],4))
    monkeypatch.setattr(extract,"generate_wiki",lambda *a,**k:pytest.fail("plan provider"))
    out=wiki.plan()
    assert [c["slug"] for c in out["slug_candidates"]]==["novel"]
    assert out["slug_decisions"]==decisions and out["delegated_excluded"]==4


def test_input_query_excludes_recent_delegated_deleted_but_keeps_waiting_queued(monkeypatch):
    class InputsCursor(Cursor):
        def __init__(self): super().__init__(True)
        def fetchall(self):
            if "FROM wiki_queue" in self.sql:
                return [("khipu",7,NOW,None),("khipu",8,NOW,NOW)]
            if "FROM wiki_slugs" in self.sql: return [("generic","tag",None)]
            return super().fetchall()
        def fetchone(self):
            if "count(*)" in self.sql: return (12,)
            return super().fetchone()
    cur=InputsCursor()
    class Conn:
        def __enter__(self): return self
        def __exit__(self,*a): return False
        def cursor(self): return cur
    monkeypatch.setattr("khipu.db.connect",lambda **k:Conn())
    episodes,pages,decisions,queue,excluded=wiki.load_inputs()
    assert excluded==12 and decisions=={"generic":{"decision":"tag"}}
    sql,params=next((s,p) for s,p in cur.statements if "SELECT e.id" in s)
    # 7 still waits and is loaded whatever its age; 8 is written and is not pulled back in
    assert params==(8,[7])
    assert "AND e.parent_session_id IS NULL AND e.deleted_at IS NULL)" in sql
    assert "OR e.id = ANY(%s)" in sql
    assert all("embedding" not in s and "has_vector" not in s for s,p in cur.statements)


def test_migration_slug_decisions_shape():
    from khipu.migrate import available
    sql=dict(available())["0053_wiki_queue"].read_text()
    assert "CREATE TABLE IF NOT EXISTS wiki_slugs" in sql and "slug TEXT PRIMARY KEY" in sql
    assert "CHECK (decision IN ('page', 'tag', 'alias'))" in sql
    assert "decision = 'alias' AND target IS NOT NULL" in sql
    assert "decision IN ('page', 'tag') AND target IS NULL" in sql
    assert "decided_at TIMESTAMPTZ NOT NULL DEFAULT now()" in sql


def test_plain_plan_has_candidates_decisions_counts_without_vector_fields(monkeypatch,capsys):
    eps=[episode(i,topics=["novel","alias"]) for i in range(1,4)]
    decisions={"alias":{"decision":"alias","target":"khipu"}}
    monkeypatch.setattr(wiki,"load_inputs",lambda **k:(eps,[page()],decisions,[],9))
    assert cli.main(["wiki","plan"])==0
    text=capsys.readouterr().out
    assert "9 delegated episodes excluded" in text and "Would ask: novel" in text
    assert '"alias": {"decision": "alias", "target": "khipu"}' in text and "1: alias" in text
    assert "vector" not in text and "distance" not in text and "BLOCKED" not in text


def test_cli_defaults_and_since_forwarded_to_draft_and_run(monkeypatch,capsys,tmp_path):
    from khipu import jobs
    seen=[]
    monkeypatch.setattr(wiki,"draft",lambda **kw:(seen.append(kw) or {}))
    monkeypatch.setattr(jobs,"run_wiki",lambda **kw:(seen.append(kw) or {"ok":True}))
    assert cli.main(["wiki","draft","--sample","1","--out",str(tmp_path/"out")])==0
    assert cli.main(["wiki","run","--json"])==0
    assert seen[0]["since"]==8 and seen[1]["since"]==8
    assert cli.main(["wiki","run","--json","--since","4"])==0
    assert seen[2]["since"]==4


def test_a_page_gets_one_write_a_run_oldest_first_and_the_rest_waits(monkeypatch):
    eps = [episode(i, topics=["khipu"]) for i in range(1, 6)] + [episode(9, topics=["other"])]
    monkeypatch.setattr(wiki, "BATCH_CHARS", 100)
    out = assign(eps, [page(), page("other")])
    assert [w["slug"] for w in out["pages"]] == ["khipu", "other"]
    khipu = out["pages"][0]
    assert [r["episode_id"] for r in khipu["episodes"]] == ["1"] and khipu["batches_waiting"] == 4
    assert out["calls_waiting"] == 4
    # every pair is still assigned, so the caller queues what was not written
    assert sum(r["slug"] == "khipu" for r in out["assignments"]) == 5
