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
    (lambda s:s.replace("status: active","status: retired"), "frontmatter"),
    (lambda s:s.replace(f"last_updated: {TODAY}","last_updated: 2000-01-01"), "frontmatter"),
    (lambda s:s.replace("Lives at /tmp/khipu with 60 pages.", "Changed."), "protected old line"),
    (lambda s:s.replace("Sources: 1","Sources: 11"), "Sources invented"),
    (lambda s:"tiny", "70%"),
])
def test_check_refusals(change,expected):
    checks = wiki.check_draft("khipu",OLD,change(NEW),["1"],today=TODAY)
    assert not checks["passed"] and any(expected in e for e in checks["errors"])


def test_fact_truth_limitation_explicit():
    assert "factual truth" in wiki.check_draft("khipu",OLD,NEW,["1"],today=TODAY)["limitation"]


def test_wiki_role_inherits_and_legacy_desktop_round_trip_preserves_explicit():
    models.set_models_merge_role("synth", provider="claude-cli",model_id="haiku")
    # Unset, the wiki follows the summaries' provider but writes with Sonnet on the Claude login.
    assert models.wiki_settings()["provider"] == "claude-cli" and models.wiki_settings()["model_id"] == "sonnet"
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
        if "wiki_holds" in self.sql:
            return ("wiki_holds",) if self.migrated else (None,)
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
        return wiki.split_header(NEW)[1]
    monkeypatch.setattr(extract,"generate_wiki",provider)
    before=snapshot(memory)
    out=wiki.draft(sample=1,model_id="fake",out=tmp_path/"output")
    assert out["passed"] == 1 and len(seen)==1
    assert "SECRET TRANSCRIPT" not in seen[0]
    assert snapshot(memory)==before
    folder=next((tmp_path/"output").glob("01-*"))
    assert (folder/"old.md").read_text() == OLD
    assert {p.name for p in folder.iterdir()} == {"old.md","draft.md","checks.json","episodes.jsonl","used.json"}
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
    monkeypatch.setattr(extract,"generate_wiki",lambda *a,**k:wiki.split_header(NEW.replace("khipu","other").replace("Sources: 1","Sources: 2"))[1])
    out=wiki.draft(sample=2,out=tmp_path/"output")
    assert out["drafts"][0]["reason"] == "source"
    assert out["drafts"][0]["error"] == "ValueError"
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
    monkeypatch.setattr(wiki,"_hold_rows",lambda **k:[])
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
    assert "OR (e.id = ANY(%s) AND e.deleted_at IS NULL)" in sql
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
    monkeypatch.setattr(wiki,"_hold_rows",lambda **k:[])
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


@pytest.mark.parametrize("old,new", [
    ("# Khipu\nKeep /tmp/wiki 60.\n", "# Khipu\nKeep /tmp/wiki 60.\nSources: 1\n"),
    ("---\nstatus: seed\n# Khipu\nKeep 60.\n", "---\nstatus: seed\n# Khipu\nKeep 60.\nSources: 1\n"),
    ("status: stub\nlast_updated: 2020-01-01\n# Khipu\nKeep 60.\n", "status: stub\nlast_updated: 2026-11-12\n# Khipu\nKeep 60.\nSources: 1\n"),
    ("status: seedling\nlast_updated: 2020-01-01\n# Khipu\nKeep 60.\n", "status: seedling\nlast_updated: 2020-01-01\n# Khipu\nKeep 60.\nSources: 1\n"),
    (OLD.replace("last_updated: 2026-10-01",'last_updated: "2026-10-01"'), NEW.replace(f"last_updated: {TODAY}",f'last_updated: "{TODAY}"')),
    (OLD.replace("last_updated: 2026-10-01\n", ""), NEW),
    ("<!-- prefix -->\n"+OLD, "<!-- prefix -->\n"+NEW),
    (OLD.replace("status: seed", "status: seedling"), NEW.replace("status: active", "status: seedling")),
    (OLD.replace("status: seed", "status: stub"), NEW.replace("status: active", "status: stub")),
    (OLD.replace("status: seed", "status: seed  "), NEW.replace("status: active", "status: seed  ")),
    (OLD.replace("pages.", "pages.  "), NEW),
    (OLD+"Sources: 7, 8\n", NEW.replace("Sources: 1", "Sources: 1, 7, 8")),
    (OLD+"- Keep 123 apples.\n", NEW+"Was Keep 123 apples. until 2026-11-12\n"),
    (OLD+"1. Keep 123 apples.\n", NEW+"Was Keep 123 apples. until 2026-11-12\n"),
    ("# Khipu\nKeep /tmp/wiki 60.\n", "# Khipu\nKeep /tmp/wiki 60.\nNew: useful prose.\nSources: 1\n"),
])
def test_permitted_legacy_shapes(old,new):
    header, _, closed = wiki.split_header(old)
    new = wiki._updated_header(header, closed, TODAY) + wiki.split_header(new)[1]
    checks=wiki.check_draft("khipu",old,new,["1"],today=TODAY)
    assert checks["passed"], checks
    assert not wiki.check_draft("khipu",old,new.replace("60", "61"),["1"],today=TODAY)["passed"]


@pytest.mark.parametrize("old,new", [
    ("# Khipu\nKeep 60.\n", "---\nlast_updated: 2026-11-12\n---\n# Khipu\nKeep 60.\nSources: 1\n"),
    ("# Khipu\nKeep 60.\n", "last_updated: 2026-11-12\n# Khipu\nKeep 60.\nSources: 1\n"),
    ("---\nstatus: seed\n# Khipu\nKeep 60.\n", "---\nstatus: seed\n---\n# Khipu\nKeep 60.\nSources: 1\n"),
    ("status: stub\n# Khipu\nKeep 60.\n", "status: active\n# Khipu\nKeep 60.\nSources: 1\n"),
    ("status: seedling\n# Khipu\nKeep 60.\n", "status: active\n# Khipu\nKeep 60.\nSources: 1\n"),
    ("last_updated: 2020-01-01\nlast_updated: 2021-01-01\n# Khipu\nKeep 60.\n", "last_updated: 2026-11-12\nlast_updated: 2021-01-01\n# Khipu\nKeep 60.\nSources: 1\n"),
    ("# Khipu\nlast_updated: 2020-01-01\nKeep 60.\n", "# Khipu\nlast_updated: 2026-11-12\nKeep 60.\nSources: 1\n"),
    ("<!-- prefix -->\n"+OLD, NEW),
    (OLD.replace("status: seed", "status: archived"), NEW),
    (OLD+"Sources: 77\n", NEW+"77 appears elsewhere.\n"),
    (OLD, NEW.replace("Lives at /tmp/khipu with 60 pages.","Lives at /tmp/khipu with 61 pages.")),
    (OLD, NEW.replace("Lives at /tmp/khipu with 60 pages.","")),
    (OLD, "# Changed first line\n"+NEW),
    (OLD, NEW.replace(f"last_updated: {TODAY}", f"last_updated: {TODAY}\nlast_updated: {TODAY}")),
])
def test_refused_legacy_shapes(old,new):
    assert not wiki.check_draft("khipu",old,new,["1"],today=TODAY)["passed"]


def test_archived_prefix_shape_ineligible_even_with_active_catalog_status():
    archived="<!-- prefix -->\n"+OLD.replace("status: seed","status: archived")
    assert not assign([episode()],[page(body=archived,status="active")])["pages"]
    assert not assign([episode()],[page(status="archived")])["pages"]


@pytest.mark.parametrize("wrapper", [lambda s:s, lambda s:"```json\n"+s+"\n```", lambda s:"Classification follows.\n"+s+"\nDone."])
def test_gate_accepts_one_object_wrapped_or_bare(wrapper):
    candidates=[{"slug":"novel"}]
    assert wiki.decide_slugs(candidates,[],generate=lambda p:wrapper('{"novel":{"decision":"tag"}}')) == {"novel":{"decision":"tag"}}
    assert wiki.decide_slugs(candidates,[],generate=lambda p:'{"novel":{"decision":"tag"}} {}') == {}


def test_reply_fence_strips_only_whole_reply_and_keeps_internal_fences():
    inner="# Khipu\n```python\nanswer = 60\n```\nSources: 1"
    assert wiki._strip_reply_fence("```markdown\n"+inner+"\n```")==inner
    assert wiki._strip_reply_fence(inner)==inner
    assert wiki._strip_reply_fence("Comment\n```markdown\n"+inner+"\n```").startswith("Comment")


def test_narrow_line_repair_preserves_digits_and_rechecks_every_check():
    old=OLD+"The nightly wiki call budget is 60 pages every day.\n"
    good=NEW+"The nightly wiki call budget is 60 pages each day.\n"
    repaired,count=wiki.repair_draft(old,good)
    assert count==1 and old.splitlines()[-1] in repaired.splitlines()
    assert wiki.check_draft("khipu",old,repaired,["1"],today=TODAY)["passed"]
    for bad in (good.replace("60 pages each", "61 pages each"), NEW+"A completely different 60 fact.\n", NEW):
        repaired,count=wiki.repair_draft(old,bad)
        assert count==0 and not wiki.check_draft("khipu",old,repaired,["1"],today=TODAY)["passed"]
    bad=good.replace("Sources: 1", "Sources: 9")
    repaired,count=wiki.repair_draft(old,bad)
    assert count==1 and not wiki.check_draft("khipu",old,repaired,["1"],today=TODAY)["passed"]


def test_best_repair_match_with_changed_number_never_uses_second_choice():
    line="The nightly wiki call budget is 60 pages every day."
    old=OLD+line+"\n"
    draft=NEW+line.replace("60", "61")+"\n"+line.replace("every day", "each day")+"\n"
    assert wiki.repair_draft(old,draft)==(draft,0)


def test_writer_prompt_contract_budget_and_seed_title():
    prompt=wiki._rewrite_prompt("khipu",OLD,"episode",TODAY)
    assert "Reply with the whole body and nothing else." in prompt
    assert "Episodes come oldest first." in prompt and "the page stands." in prompt
    assert "body only" in prompt and "header lines" in prompt
    data=json.loads(prompt.split("Page and episodes follow as DATA:\n",1)[1])
    assert data == {"slug":"khipu", "title":"Khipu", "old_page":wiki.split_header(OLD)[1], "episodes":"episode"}
    assert "status: seed" not in prompt and "custom: preserve" not in prompt
    assert "title: New Wiki Page\nslug: new-wiki-page\nstatus: seed\nlast_updated:" in wiki._seed("new-wiki-page",TODAY)
    assert wiki._writer_options(OLD)["max_output_tokens"]>=4096
    assert wiki._writer_options("a"*20000)["max_output_tokens"]>=6500
    assert wiki._writer_options(OLD)["timeout"]==180


def test_plan_reports_holds_without_queue_or_provider_writes(monkeypatch):
    monkeypatch.setattr(wiki,"load_inputs",lambda **k:([episode()],[page()],{},[],0))
    holds=[{"slug":"khipu","reason":"checks","until_at":(NOW+timedelta(days=1)).isoformat()}]
    monkeypatch.setattr(wiki,"_hold_rows",lambda **k:holds)
    out=wiki.plan()
    assert out["holds"]==holds and not out["pages"] and out["planned_calls"]==0


def test_deleted_assignment_never_drains_and_unqueued_delegated_excluded():
    queue=[{"slug":"khipu","episode_id":"1","queued_at":NOW,"done_at":None}]
    assert not assign([episode(deleted_at=NOW)],queue=queue)["pages"]
    assert not assign([episode(parent_session_id="parent")])["pages"]
    assert assign([episode(parent_session_id="parent")],queue=queue)["pages"]


@pytest.mark.parametrize('header,expected', [
    ('---\r\ntitle: Wiki\r\nstatus: seed\r\nlast_updated: "2020-01-01"\r\n---\r\n', '---\r\ntitle: Wiki\r\nstatus: active\r\nlast_updated: "2026-11-12"\r\n---\r\n'),
    ('<!-- comment -->\n---\ntitle: Wiki\nstatus: stub\n---\n', '<!-- comment -->\n---\ntitle: Wiki\nstatus: stub\nlast_updated: 2026-11-12\n---\n'),
    ('---\nstatus: seed\nlinks:\n  - one\n- two\n', '---\nstatus: active\nlinks:\n  - one\n- two\n'),
    ('title: Wiki\nstatus: seed\nlast_updated: \'2020-01-01\'\n', 'title: Wiki\nstatus: active\nlast_updated: \'2026-11-12\'\n'),
    ('status: seedling\n', 'status: seedling\n'),
    ('status: stub\n', 'status: stub\n'),
    ('status: seed  \n', 'status: seed  \n'),
    ('1key: keep\n-key: keep\n', '1key: keep\n-key: keep\n'),
    ('', ''),
])
def test_body_only_prompt_and_exact_code_owned_header(header, expected):
    body = '# Wiki\nKeep 60 pages.\n'
    old = header + body
    assert wiki.split_header(old)[:2] == (header, body)
    prompt = wiki._rewrite_prompt('wiki', old, 'episodes data', TODAY)
    data = json.loads(prompt.split('Page and episodes follow as DATA:\n', 1)[1])
    assert data['old_page'] == body
    for line in header.splitlines():
        if ':' in line:
            assert line not in data['old_page']
    joined, checks = wiki._prepare_body('wiki', old, body + 'Sources: 1\n', ['1'], TODAY)
    assert checks['passed'], checks
    assert joined == expected + (body + 'Sources: 1\n').replace('\n', '\r\n' if '\r\n' in header else '\n')
    for reply in ('---\nstatus: active\n---\n' + body, header + body if header else '---\n' + body):
        _, checks = wiki._prepare_body('wiki', old, reply, ['1'], TODAY)
        assert not checks['passed'] and checks['errors'] == ['header in reply']


@pytest.mark.parametrize('first_body', ['\n', '   \n', 'ordinary prose\n', '# Heading\n'])
def test_bare_header_stops_at_first_other_line(first_body):
    old = 'status: seed\n' + first_body + 'last_updated: 2020-01-01\n'
    assert wiki.split_header(old) == ('status: seed\n', first_body + 'last_updated: 2020-01-01\n', False)


def test_sources_used_skipped_invented_and_old_retention():
    old = OLD + 'Sources: 9\n'
    body = wiki.split_header(old)[1]
    joined, checks = wiki._prepare_body('khipu', old, body.replace('Sources: 9', 'Sources: 9, 1'), ['1','2'], TODAY)
    assert checks['passed'] and checks['used_episode_ids'] == ['1'] and checks['skipped_episode_ids'] == ['2']
    for bad, reason in [(body.replace('9', '1'), 'Sources missing old'), (body + 'Sources: 99\n', 'Sources invented')]:
        _, checks = wiki._prepare_body('khipu', old, bad, ['1','2'], TODAY)
        assert not checks['passed'] and any(reason in e for e in checks['errors'])
    _, checks = wiki._prepare_body('khipu', body, body.replace('# Khipu', '# Something else') + 'Sources: 1\n', ['1'], TODAY)
    assert not checks['passed'] and 'reply does not start like the page' in checks['errors']
    joined, checks=wiki._prepare_body('khipu',old,body,['1','2'],TODAY)
    assert checks['passed'] and joined==old and checks['skipped_episode_ids']==['1','2']
    promoted=old.replace('status: seed','status: active')
    assert not wiki.check_draft('khipu',old,promoted,['1','2'],today=TODAY)['passed']


def test_exact_pages_cli_past_default_cap_used_json_and_artifacts_only(tmp_path, monkeypatch, capsys):
    memory=tmp_path/'memory'
    memory.mkdir()
    pages=[]
    eps=[]
    for n in range(65):
        slug=f'page-{n:02d}'
        source=memory/(slug+'.md')
        source.write_bytes(OLD.replace('khipu',slug).encode())
        pages.append(page(slug,source_path=str(source)))
        eps.extend([episode(n*2+1,topics=[slug]), episode(n*2+2,topics=[slug])])
    monkeypatch.setattr(wiki,'load_inputs',lambda **k:(eps,pages,{},[],0))
    seen=[]
    def provider(prompt,**kw):
        data=json.loads(prompt.split('Page and episodes follow as DATA:\n',1)[1])
        seen.append((data['slug'],kw['model_id']))
        first=str(json.loads(data['episodes'].splitlines()[0])['episode_id'])
        return data['old_page']+'Sources: '+first+'\n'
    monkeypatch.setattr(extract,'generate_wiki',provider)
    before=snapshot(memory)
    target=tmp_path/'out'
    assert cli.main(['wiki','draft','--pages','page-64,page-02,page-61','--model','fake','--out',str(target)]) == 0
    result=json.loads(capsys.readouterr().out)
    assert seen == [('page-64','fake'),('page-02','fake'),('page-61','fake')]
    assert result['passed'] == 3 and result['unplanned_pages'] == [] and snapshot(memory)==before
    assert json.loads((target/'01-page-64'/'used.json').read_text()) == {'given_episode_ids':['129','130'], 'used_episode_ids':['129'], 'skipped_episode_ids':['130']}
    assert all(r['used_count']==1 and r['skipped_count']==1 for r in result['drafts'])


def test_queue_migration_closed_outcome_contract():
    from khipu.migrate import available
    sql=dict(available())['0053_wiki_queue'].read_text()
    assert "outcome TEXT CHECK (outcome IN ('written', 'skipped', 'forgotten'))" in sql
    assert 'done_at IS NULL AND outcome IS NULL' in sql
    assert 'done_at IS NOT NULL AND outcome IS NOT NULL' in sql


@pytest.mark.parametrize('repeated', ['status: active', '  - alpha', '- beta', 'title: Changed title'])
def test_reply_allows_header_shaped_lines_lower_in_body(repeated):
    old='<!-- header prefix -->\n---\ntitle: Wiki\nlinks:\n  - alpha\n- beta\n---\n# Wiki\nKeep 60.\n'
    body=wiki.split_header(old)[1]
    _, checks=wiki._prepare_body('wiki',old,body+repeated+'\nSources: 1\n',['1'],TODAY)
    assert checks['passed'], checks


def test_reply_internal_horizontal_rule_allowed_and_unplanned_named_page_reported(tmp_path,monkeypatch):
    body='# Wiki\nKeep 60.\n'
    _, checks=wiki._prepare_body('wiki',body,body+'---\nSources: 1\n',['1'],TODAY)
    assert checks['passed']
    monkeypatch.setattr(wiki,'load_inputs',lambda **k:([],[page('wiki')],{},[],0))
    monkeypatch.setattr(extract,'generate_wiki',lambda *a,**k:pytest.fail('unplanned writer call'))
    out=wiki.draft(pages=['wiki'],out=tmp_path/'out')
    assert out['unplanned_pages']==['wiki'] and out['drafts']==[]


@pytest.mark.parametrize('tag', ['Markdown', 'text', 'CUSTOM'])
def test_reply_preface_guard_and_arbitrary_whole_fence(tag):
    old='---\ntitle: Wiki\n---\n\n# H\nKeep 5 facts.\n'
    body=wiki.split_header(old)[1]
    reply=body.strip()+'\n- **2026-11-12 (9):** Added fact.'
    _, checks=wiki._prepare_body('wiki',old,'Here is the updated page:\n\n'+reply,['9'],TODAY)
    assert not checks['passed'] and 'reply does not start like the page' in checks['errors']
    joined, checks=wiki._prepare_body('wiki',old,'```'+tag+'\n'+reply+'\n```',['9'],TODAY)
    assert checks['passed'] and '```' not in joined
    for bad in [reply+'\n```'+tag, '```'+tag+'\n'+reply]:
        _, checks=wiki._prepare_body('wiki',old,bad,['9'],TODAY)
        assert not checks['passed'] and 'reply starts or ends with a fence' in checks['errors']


def test_two_horizontal_rules_are_body_and_stay_in_writer_input():
    old='# Page\n\nIntro…\n\n---\n\n## A\nthing 5\n\n---\n\n## B…'
    assert wiki.split_header(old)==('',old,False)
    prompt=wiki._rewrite_prompt('wiki',old,'episode',TODAY)
    data=json.loads(prompt.split('Page and episodes follow as DATA:\n',1)[1])
    assert data['old_page']==old
    joined,checks=wiki._prepare_body('wiki',old,old+'\n- **2026-11-12 (9):** Added fact.',['9'],TODAY)
    assert checks['passed'] and not wiki.split_header(joined)[0]
    assert 'last_updated:' not in joined


@pytest.mark.parametrize('prefix,closed', [('',True), ('\n<!-- one-line -->\n',True), ('# Page\n',False), ('<!-- multi\nline -->\n',False)])
def test_closed_header_accepts_only_blank_or_single_line_comments_above_it(prefix,closed):
    text=prefix+'---\ntitle: Wiki\n---\n# Wiki\n'
    assert wiki.split_header(text)[2] is closed


@pytest.mark.parametrize('ending', ['\n','\r\n'])
@pytest.mark.parametrize('tail', ['', '\n'])
def test_join_keeps_leading_blanks_old_final_newline_and_line_endings(ending,tail):
    old=('---\nstatus: seed\n---\n\n \n# Wiki\nOld fact.'+tail).replace('\n',ending)
    reply='# Wiki\nOld fact.\n- **2026-11-12 (19):** Added fact.'
    joined, checks=wiki._prepare_body('wiki',old,reply,['19'],TODAY)
    assert checks['passed'], checks
    expected=('---\nstatus: active\nlast_updated: '+TODAY+'\n---\n\n \n'+reply+'\n\nSources: episodes 19'+tail).replace('\n',ending)
    assert joined==expected
    assert wiki.check_draft('wiki',old,joined,['19'],today=TODAY)['passed']


@pytest.mark.parametrize('header', ['---\nstatus: seed\n---', 'status: seed'])
def test_header_only_without_final_newline_separates_new_body(header):
    joined,checks=wiki._prepare_body('wiki',header,'# Wiki\n- **2026-11-12 (19):** Added fact.',['19'],TODAY)
    assert checks['passed'], checks
    assert '\n# Wiki\n' in joined and joined.endswith('\n')


def test_top_header_repetition_refused_but_identical_list_item_below_heading_allowed():
    old='title: Wiki\nstatus: seed\nlinks:\n- khipu\n\n# Wiki\nstatus: needs work\n- khipu\n'
    body=wiki.split_header(old)[1]
    _,checks=wiki._prepare_body('wiki',old,'title: Wiki\n'+body,['19'],TODAY)
    assert checks['errors']==['header in reply']
    _,checks=wiki._prepare_body('wiki',old,body+'- **2026-11-12 (19):** Added fact.',['19'],TODAY)
    assert checks['passed'],checks


@pytest.mark.parametrize('status', ['archived','superseded','retired','abandoned'])
def test_bare_status_ineligible_but_same_line_in_body_is_ordinary(status):
    assert not wiki._eligible(page('wiki',body='status: '+status+'\n\n# Wiki\n'))
    assert wiki._eligible(page('wiki',body='# Wiki\nstatus: '+status+'\n'))


def test_empty_last_updated_gets_space():
    assert wiki._updated_header('last_updated:\n',False,TODAY)=='last_updated: '+TODAY+'\n'


def test_held_new_pages_do_not_consume_ten_slots_for_queued_or_qualified_pages():
    eps=[episode(i*3+j,topics=[f'new-{i:02}']) for i in range(12) for j in range(3)]
    decisions={f'new-{i:02}':{'decision':'page'} for i in range(12)}
    for queued in (False,True):
        queue=[{'slug':e['topics'][0],'episode_id':e['id'],'queued_at':NOW,'done_at':None} for e in eps] if queued else []
        out=assign(eps,[],decisions=decisions,queue=queue,deferred_slugs={'new-00','new-01'})
        assert len(out['pages'])==10 and out['planned_calls']==10
        assert {p['slug'] for p in out['pages']}=={f'new-{i:02}' for i in range(2,12)}


def test_bullet_ids_only_new_lines_whole_numbers_and_code_appends_last_sources():
    old='# Wiki\nExisting episode 17 and 40.\nSources: 9\nOther text.\nSources: 8\n'
    reply=old+'- **2026-11-12 (117):** Longer number.\n- **2026-11-12 (41):** Fact.\n'
    joined,checks=wiki._prepare_body('wiki',old,reply,['17','40','41','117','2'],TODAY)
    assert checks['passed'] and checks['used_episode_ids']==['41','117']
    assert checks['skipped_episode_ids']==['17','40','2']
    assert 'Sources: 9\n' in joined and 'Sources: 8, 41, 117\n' in joined
    assert wiki._source_ids(joined)=={'9','8','41','117'}
    assert wiki.check_draft('wiki',old,joined,['17','40','41','117','2'],today=TODAY)['passed']
    _,checks=wiki._prepare_body('wiki',old,old+'New unsourced fact.',['17'],TODAY)
    assert not checks['passed'] and 'changed without a source' in checks['errors']


def test_writer_allowance_counts_utf8_page_and_episode_bytes_and_updated_prompt():
    import math
    old='é'*9000
    episodes='漢'*10000
    assert wiki._writer_options(old,episodes)['max_output_tokens']==math.ceil(1.3*len(old.encode())+.5*len(episodes.encode()))
    assert wiki._writer_options('x','y')['max_output_tokens']==8192
    prompt=wiki._rewrite_prompt('wiki',OLD,'episode',TODAY)
    assert 'Nothing before or after the page text.' in prompt
    assert "10. Put the episode's id in round brackets in every bullet you write from it, as in the bullet form below. Leave Sources lines as they are: they are kept elsewhere." in prompt


@pytest.mark.parametrize('ending', ['\n','\r\n'])
def test_blank_only_old_body_keeps_leading_blank_lines(ending):
    old=('---\nstatus: seed\n---\n\n \n\n').replace('\n',ending)
    reply='# Wiki\n- **2026-11-12 (19):** Added fact.'
    joined,checks=wiki._prepare_body('wiki',old,reply,['19'],TODAY)
    assert checks['passed'],checks
    assert ('---\n\n \n\n# Wiki\n').replace('\n',ending) in joined
    assert joined.endswith(ending)


def test_first_line_must_match_before_protected_line_repair():
    old='# Wiki heading (2026-11-12) - keep this exact heading.\nKeep 60 pages.\n'
    reply=old.replace('heading.', 'heading!')+'- **2026-11-12 (19):** Added fact.'
    _,checks=wiki._prepare_body('wiki',old,reply,['19'],TODAY)
    assert not checks['passed'] and 'reply does not start like the page' in checks['errors']


def test_new_page_reply_must_open_with_a_heading():
    old='---\ntitle: Tenant Query Guard\nslug: tenant-query-guard\nstatus: seed\nlast_updated: 2026-11-01\n---\n'
    cut='pins were changed and no assertions were weakened.\n- **2026-11-06 (19):** The guard test failed.'
    _,checks=wiki._prepare_body('tenant-query-guard',old,cut,['19'],TODAY)
    assert not checks['passed'] and 'new page does not start with a heading' in checks['errors']
    whole='# Tenant query guard\n\n- **2026-11-06 (19):** The guard test failed.'
    _,checks=wiki._prepare_body('tenant-query-guard',old,whole,['19'],TODAY)
    assert checks['passed'],checks


def test_writer_prompt_tells_a_new_page_to_open_with_a_heading():
    assert 'A page with no text yet starts with a "# " heading line' in wiki._rewrite_prompt('x','','','2026-11-12')


def test_heading_only_reply_on_a_page_with_no_text_is_all_skipped():
    old='---\ntitle: Memory Management\nslug: memory-management\nstatus: seed\nlast_updated: 2026-11-01\n---\n'
    joined,checks=wiki._prepare_body('memory-management',old,'# Memory Management\n',['19','20'],TODAY)
    assert checks['passed'] and joined==old
    assert checks['used_episode_ids']==[] and checks['skipped_episode_ids']==['19','20']
    # Prose with no episode id is not a skip: it stays refused, so the rows are retried.
    _,checks=wiki._prepare_body('memory-management',old,'# Memory Management\n\nSome fact with no source.\n',['19'],TODAY)
    assert not checks['passed'] and 'changed without a source' in checks['errors']


def test_episode_records_copied_into_the_reply_are_dropped_before_ids_are_counted():
    old='# Routing\n\nKeep 60 pages.\n\nSources: episodes 3\n'
    echo='{"date": "2026-11-05", "episode_id": "20", "summary": "Other work.", "decisions": [], "preferences": []}'
    reply='# Routing\n\nKeep 60 pages.\n'+echo+'\n\n- **2026-11-05 (19):** Muse is canceled, ending 11-13.\n\nSources: episodes 3\n'
    joined,checks=wiki._prepare_body('routing',old,reply,['19','20'],TODAY)
    assert checks['passed'],checks
    assert '"episode_id"' not in joined and 'Muse is canceled' in joined
    assert checks['used_episode_ids']==['19'] and checks['skipped_episode_ids']==['20']


def test_writer_prompt_forbids_copying_episode_records():
    assert 'Never copy an episode record into your reply.' in wiki._rewrite_prompt('x','','','2026-11-12')
