"""khipu.work, Work view phase 3 (docs/plans/2026-10-15-work-view.md, Part B):
the summary-file reader and its source states, the read model, Waiting on you,
the settings, the doctor block and `khipu work`.

Hermetic and synthetic: every summary file is written under tmp_path, the
ledger, commitments and routing record are passed in (or the readers are
monkeypatched), and nothing connects to a hub or database.
"""
from __future__ import annotations

import io
import json
import os
import shutil
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from khipu import safe_read, work

NOW = datetime(2026, 10, 15, 20, 0, 0, tzinfo=timezone.utc)
KEY = "sk-ant-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4"
ACCT_KEY = "claude|/logins/claude-second"
ACCT_ID = "acct-0123456789ab"


def _t(seconds_ago: float = 0) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    (tmp_path / "data").mkdir()
    return tmp_path


def job(i="job-1", **over):
    base = {"id": i, "project": "atlas", "state": "running", "kind": "fix", "started_at": _t(600),
            "updated_at": _t(30), "summary": "Fixing the two review findings", "account": "Claude second",
            "lane": "secondary", "now": [{"at": _t(30), "text": "Running the full test suite"}]}
    base.update(over)
    return base


def pr(i="41", **over):
    base = {"id": i, "project": "atlas", "title": "Sign-in remembers the last account", "stage": "fixing",
            "stage_at": _t(120), "link": "https://github.com/example/atlas/pull/41?token=abc#frag"}
    base.update(over)
    return base


def train(i="2.4.0", **over):
    base = {"id": i, "project": "atlas", "name": i, "state": "testing", "updated_at": _t(60),
            "prs": ["41"], "runs": [{"name": "Unit tests", "state": "passed", "at": _t(90)}]}
    base.update(over)
    return base


def summary(**over):
    doc = {"format": "khipu-work-summary", "version": 1, "written_at": _t(10),
           "project": {"id": "atlas", "name": "Atlas"}, "runner": {"name": "lanes", "interval_s": 20},
           "jobs": [job()], "prs": [pr()], "trains": [train()]}
    doc.update(over)
    return doc


def put(tmp_path, doc, name="work-summary.json") -> Path:
    path = tmp_path / "runner" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    return path


def entry(path, **over):
    base = {"project": "atlas", "name": "Atlas", "file": str(path), "on": True, "show_now": True}
    base.update(over)
    return base


def cfg(**over):
    out = work.settings({})
    out.update(over)
    return out


def read(path, now=NOW, **over):
    return work.read_source(entry(path), cfg(**over), now)


VOCAB = {"state": [["VERIFIED", "done"], ["MERGED-UNVERIFIED", "open"], ["BUILT-UNMERGED", "open"],
                   ["IN FLIGHT", "open"], ["QUEUED", "open"], ["NOT STARTED", "open"],
                   ["NEEDS OWNER", "needs"], ["DROPPED", "dropped"], ["DONE", "done"]],
         "kind": ["goal", "slice", "review", "loose-end", "needs-owner"]}


def row(rid, title, state="IN FLIGHT", kind="slice", parent="", seq=1, refs=None, ago=3600, **over):
    base = {"row_id": rid, "title": title, "kind": kind, "parent": parent, "state": state, "owner": "none",
            "evidence": "", "link": "", "refs": refs or {}, "seq": seq, "updated_at": _t(ago)}
    base.update(over)
    return base


def ledger_data(rows, project="atlas", entered=None):
    return {"projects": {project: {"name": "Atlas", "hidden": False, "vocab": VOCAB, "rows": rows,
                                   "entered": entered or {}}}, "offline": False, "as_of": None, "error": None}


NO_LEDGER = {"projects": {}, "offline": False, "as_of": None, "error": None}
NO_OWED = {"items": [], "error": None}
ROUTING = {"accounts": [{"id": ACCT_ID, "label": "Claude second", "state": "open"}], "rules": [], "holds": [],
           "lanes": {"lanes": {"secondary": {"id": ACCT_ID, "label": "Claude second"}}},
           "labels": {"claude second": {"id": ACCT_ID, "label": "Claude second"}}, "error": None}


def view_for(path, *, ledger=NO_LEDGER, owed=NO_OWED, routing=ROUTING, entry_over=None, **over):
    config = cfg(work_projects=[entry(path, **(entry_over or {}))], **over)
    return work.build_view(NOW, cfg=config, ledger_data=ledger, owed=owed, routing_info=routing)


def board(view, project="atlas"):
    return next(p for p in view["projects"] if p["project"] == project)


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from strings(v)


def run(*argv):
    from khipu import cli

    args = cli.build_parser().parse_args(["work", *argv])
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = args.func(args)
    return rc, out.getvalue(), err.getvalue()


# ---- the source states ---------------------------------------------------------------


def test_ok_source_reads_and_cleans_everything(tmp_path):
    got = read(put(tmp_path, summary()))
    assert got["state"] == "ok" and got["problem"] is None
    c = got["contents"]
    assert c["project"] == "atlas" and len(c["jobs"]) == 1 and len(c["prs"]) == 1 and len(c["trains"]) == 1
    assert c["jobs"][0]["state_words"] == "Running" and c["prs"][0]["stage_words"] == "Being fixed"
    assert c["jobs"][0]["account"] == "Claude second"
    assert c["prs"][0]["link"] == "https://github.com/example/atlas/pull/41"  # host and path only


def test_stale_and_gone(tmp_path):
    path = put(tmp_path, summary(written_at=_t(2 * 86400)))
    assert read(path)["state"] == "gone"
    # 0 turns "gone" off: it stays stale.
    assert read(path, work_gone_after=0)["state"] == "stale"
    path = put(tmp_path, summary(written_at=_t(5 * 60)))
    assert read(path)["state"] == "stale"


def test_missing(tmp_path):
    got = read(tmp_path / "runner" / "nope.json")
    assert got["state"] == "missing" and got["contents"] is None
    assert got["problem"] == "No summary file yet at the place you set."


def test_too_big_shows_the_last_good_read(tmp_path):
    path = put(tmp_path, summary())
    assert read(path)["state"] == "ok"
    got = read(path, work_file_max_bytes=100)
    assert got["state"] == "too_big" and "over the" in got["problem"] and got["limit"] == 100
    assert got["contents"]["jobs"][0]["id"] == "job-1"
    assert read(path, work_file_max_bytes=0)["state"] == "ok"


def test_unreadable_shows_the_last_good_read(tmp_path):
    path = put(tmp_path, summary())
    assert read(path)["state"] == "ok"
    put(tmp_path, "{ not json at all")
    got = read(path)
    assert got["state"] == "unreadable" and "Traceback" not in got["problem"] and "Expecting" not in got["problem"]
    assert got["contents"]["jobs"][0]["id"] == "job-1"
    put(tmp_path, json.dumps({"format": "something-else", "version": 1}))
    assert read(path)["state"] == "unreadable"
    # With no earlier good read there is nothing to show.
    other = put(tmp_path, "[1, 2", name="other.json")
    got = work.read_source(entry(other, project="harbor"), cfg(), NOW)
    assert got["state"] == "unreadable" and got["contents"] is None


def test_newer_format_is_not_read(tmp_path):
    got = read(put(tmp_path, summary(version=2)))
    assert got["state"] == "newer_format" and got["contents"] is None and got["version"] == 2
    assert "newer format (2)" in got["problem"]


def test_wrong_project_shows_nothing(tmp_path):
    got = read(put(tmp_path, summary(project={"id": "harbor", "name": "Harbor"})))
    assert got["state"] == "wrong_project" and got["contents"] is None
    assert got["problem"] == "This file says it is for harbor, not atlas."


def test_off_and_none(tmp_path):
    path = put(tmp_path, summary())
    assert work.read_source(entry(path, on=False), cfg(), NOW)["state"] == "off"
    got = work.read_source(None, cfg(), NOW)
    assert got["state"] == "none" and got["problem"] == "No job runner reports for this project"


def test_refused_under_a_credential_folder_and_never_opened(tmp_path, monkeypatch):
    path = tmp_path / "API Keys" / "work-summary.json"
    path.parent.mkdir()
    path.write_text(json.dumps(summary()))
    opened = []
    real = os.open
    monkeypatch.setattr(os, "open", lambda p, *a, **k: (opened.append(os.fspath(p)), real(p, *a, **k))[1])
    got = read(path)
    assert got["state"] == "refused" and got["contents"] is None
    assert str(path) not in opened


def test_the_file_is_opened_read_only(tmp_path, monkeypatch):
    path = put(tmp_path, summary())
    calls = []
    real = os.open

    def spy(p, flags, *a, **k):
        calls.append((os.fspath(p), flags))
        return real(p, flags, *a, **k)

    monkeypatch.setattr(os, "open", spy)
    assert read(path)["state"] == "ok"
    # Opened by its name from its folder's descriptor, never through a link.
    mine = [flags for p, flags in calls if p in (str(path), path.name)]
    assert mine
    for flags in mine:
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        assert (flags & os.O_ACCMODE) == os.O_RDONLY and flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert read(fifo)["state"] == "unreadable"
    assert read(Path("/dev/null"))["state"] == "unreadable"
    private = tmp_path / "API Keys" / "never-opened.json"
    link = tmp_path / "private-link"
    link.symlink_to(private)
    calls.clear()
    assert read(private)["state"] == "refused" and read(link)["state"] == "refused"
    assert not calls
    # Too big is decided from the open file's own size: nothing is read.
    reads = []
    real_read = os.read
    monkeypatch.setattr(os, "read", lambda fd, n: (reads.append(n), real_read(fd, n))[1])
    assert read(path, work_file_max_bytes=1)["state"] == "too_big" and not reads


def test_projects_setting_refuses_a_private_or_relative_path(tmp_path):
    bad = tmp_path / "API Keys" / "s.json"
    with pytest.raises(ValueError, match="private folder"):
        work.set_setting("work_projects", [{"project": "atlas", "file": str(bad)}])
    with pytest.raises(ValueError, match="full path"):
        work.set_setting("work_projects", [{"project": "atlas", "file": "runner/s.json"}])
    with pytest.raises(ValueError):
        work.set_setting("work_projects", [{"project": "atlas", "file": "/x/s.json", "extra": 1}])
    good = tmp_path / "runner" / "s.json"
    work.set_setting("work_projects", [{"project": "atlas", "name": "Atlas", "file": str(good)}])
    stored = work.settings()["work_projects"]
    assert stored == [{"project": "atlas", "name": "Atlas", "file": str(good), "on": True, "show_now": True}]


# ---- items, caps -----------------------------------------------------------------------


def test_one_bad_item_drops_only_itself_and_another_projects_item_is_dropped(tmp_path):
    doc = summary(jobs=[job("job-1"), job("bad id!"), job("job-3", summary=None),
                        job("job-4", project="harbor"), "not an object", job("job-5")],
                  prs=[pr("41"), pr("42", stage_at="yesterday")])
    got = read(put(tmp_path, doc))["contents"]
    assert [j["id"] for j in got["jobs"]] == ["job-1", "job-5"]
    assert [p["id"] for p in got["prs"]] == ["41"]
    assert got["dropped"] == 5 and got["other_project"] == 1
    b = board(view_for(put(tmp_path, doc)))
    assert b["source"]["dropped"] == 5 and b["source"]["other_project"] == 1


def test_an_unknown_state_keeps_the_item(tmp_path):
    got = read(put(tmp_path, summary(jobs=[job(state="daydreaming")])))["contents"]["jobs"][0]
    assert got["state"] == "daydreaming" and got["state_known"] is False
    assert got["state_words"] == "State not known to this Khipu"


def test_a_merged_pr_needs_merged_at(tmp_path):
    got = read(put(tmp_path, summary(prs=[pr("1", stage="merged"), pr("2", stage="merged", merged_at=_t(60))])))
    assert [p["id"] for p in got["contents"]["prs"]] == ["2"]


def test_caps_keep_the_newest_and_say_so(tmp_path):
    jobs = [job(f"job-{i}", updated_at=_t(1000 - i * 100)) for i in range(4)]
    prs = [pr(str(i), stage_at=_t(1000 - i * 100)) for i in range(4)]
    trains = [train(f"t{i}", updated_at=_t(1000 - i * 100),
                    runs=[{"name": f"run {n}", "state": "passed", "at": _t(500 - n * 10)} for n in range(4)])
              for i in range(3)]
    path = put(tmp_path, summary(jobs=jobs, prs=prs, trains=trains))
    c = read(path, work_max_jobs=2, work_max_prs=2, work_max_trains=2, work_max_runs=2)["contents"]
    assert [j["id"] for j in c["jobs"]] == ["job-2", "job-3"]
    assert [p["id"] for p in c["prs"]] == ["2", "3"]
    assert [t["id"] for t in c["trains"]] == ["t1", "t2"]
    # The run cap counts the runs of every train shown: the two newest are each train's run 3.
    assert [[r["name"] for r in t["runs"]] for t in c["trains"]] == [["run 3"], ["run 3"]]
    assert c["capped"]["jobs"] == {"shown": 2, "of": 4} and c["capped"]["prs"] == {"shown": 2, "of": 4}
    assert c["capped"]["trains"] == {"shown": 2, "of": 3} and c["capped"]["runs"] == {"shown": 2, "of": 8}
    assert board(view_for(path, work_max_jobs=2))["source"]["capped"]["jobs"] == {"shown": 2, "of": 4}
    # 0 lifts each cap.
    c = read(path, work_max_jobs=0, work_max_prs=0, work_max_trains=0, work_max_runs=0)["contents"]
    assert (len(c["jobs"]), len(c["prs"]), len(c["trains"]), len(c["trains"][0]["runs"])) == (4, 4, 3, 4)
    assert c["capped"] == {}


def test_file_bytes_and_line_chars_caps_lift_at_zero(tmp_path):
    long = "word " * 60
    path = put(tmp_path, summary(jobs=[job(summary=long)]))
    clipped = read(path, work_line_chars=20)["contents"]["jobs"][0]["summary"]
    assert len(clipped) == 20 and clipped.endswith("…")
    assert len(read(path, work_line_chars=0)["contents"]["jobs"][0]["summary"]) == len(long.strip())
    size = path.stat().st_size
    assert read(path, work_file_max_bytes=size - 1)["state"] == "too_big"
    assert read(path, work_file_max_bytes=size)["state"] == "ok"
    assert read(path, work_file_max_bytes=0)["state"] == "ok"


# ---- text cleaning and the status lines --------------------------------------------------


def test_text_is_cleaned_and_a_key_shaped_field_is_left_out_whole(tmp_path):
    keyed = f"Pushed with {KEY} today"
    doc = summary(jobs=[job("job-1", summary="Editing tmp/x/bar.py now\x00 \x07ok"),
                        job("job-2", summary=keyed)],
                  prs=[pr("41", summary=keyed, title="Fine title")])
    c = read(put(tmp_path, doc))["contents"]
    assert [j["id"] for j in c["jobs"]] == ["job-1"]  # a required text field that trips redaction drops the item
    assert c["jobs"][0]["summary"] == "Editing (a path) now ok"
    assert c["prs"][0]["summary"] is None and c["prs"][0]["title"] == "Fine title"
    assert KEY not in json.dumps(c) and "*" not in json.dumps(c)
    assert work.clean_text(keyed, 200) is None


def test_the_now_list_is_three_short_lines(tmp_path):
    lines = [{"at": _t(100 - i), "text": f"Step {i} " + "x" * 200 if i == 4 else f"Step {i}"} for i in range(5)]
    lines.insert(2, {"at": _t(50), "text": f"leaked {KEY}"})
    c = read(put(tmp_path, summary(jobs=[job(now=lines)])))["contents"]
    got = c["jobs"][0]["now"]
    assert len(got) <= 3 and all(len(x["text"]) <= 90 for x in got)
    assert got[-1]["text"].startswith("Step 4") and got[-1]["text"].endswith("…")
    assert all(KEY not in x["text"] for x in got)


def test_show_now_false_empties_the_now_list(tmp_path):
    path = put(tmp_path, summary())
    assert board(view_for(path))["jobs"][0]["now"]
    assert board(view_for(path, entry_over={"show_now": False}))["jobs"][0]["now"] == []


# ---- the read model --------------------------------------------------------------------------


def test_a_gone_source_leaves_running_and_old_finished_jobs_leave(tmp_path):
    jobs = [job("job-run"), job("job-old", state="done", ended_at=_t(2 * 3600), updated_at=_t(2 * 3600)),
            job("job-new", state="done", ended_at=_t(300), updated_at=_t(300))]
    path = put(tmp_path, summary(jobs=jobs))
    v = view_for(path)
    assert [j["id"] for j in board(v)["jobs"]] == ["job-run", "job-new"]
    assert [j["id"] for j in v["live"]] == ["job-run"] and v["counts"]["running"] == 1
    assert v["live"][0]["account_ref"] == {"id": ACCT_ID, "label": "Claude second", "matched": True}
    # A runner's clock never goes back: forget the newer read before
    # standing in an older file for one that stopped being written.
    shutil.rmtree(work.state_dir() / "last")
    gone = put(tmp_path, summary(jobs=jobs, written_at=_t(2 * 86400)))
    v = view_for(gone)
    assert board(v)["source"]["state"] == "gone" and v["live"] == []


def test_a_job_with_no_matching_account_keeps_its_label(tmp_path):
    path = put(tmp_path, summary(jobs=[job(account="Somebody else", lane="nowhere")]))
    ref = view_for(path)["live"][0]["account_ref"]
    assert ref == {"label": "Somebody else", "matched": False}


def test_quiet_job(tmp_path):
    path = put(tmp_path, summary(jobs=[job(updated_at=_t(20 * 60))]))
    assert board(view_for(path))["jobs"][0]["quiet"] is True


def test_merged_this_week_window(tmp_path):
    prs = [pr("1", stage="merged", merged_at=_t(86400)), pr("2", stage="merged", merged_at=_t(9 * 86400))]
    b = board(view_for(put(tmp_path, summary(prs=prs))))
    assert [p["id"] for p in b["prs"]] == ["1"] and [m["id"] for m in b["merged"]] == ["1"]


def test_a_disagreement_is_said_and_neither_side_changes(tmp_path):
    rows = [row("s-1", "Sign-in slice", state="BUILT-UNMERGED", refs={"pr": "41"})]
    path = put(tmp_path, summary(prs=[pr("41", stage="merged", merged_at=_t(60))]))
    got = board(view_for(path, ledger=ledger_data(rows)))["prs"][0]
    assert got["disagrees"] == "The runner says this merged; the ledger still says built, not merged"
    assert got["ledger"][0]["state"] == "BUILT-UNMERGED"
    agree = board(view_for(path, ledger=ledger_data([row("s-1", "x", state="VERIFIED", refs={"pr": "41"})])))
    assert agree["prs"][0]["disagrees"] is None


def test_ledger_part_of_a_board_without_a_runner():
    rows = [row("g-1", "Ship it", kind="goal", state="IN FLIGHT"),
            row("s-1", "Slice one", parent="g-1", state="VERIFIED", seq=2),
            row("s-2", "Slice two", parent="g-1", state="QUEUED", seq=3)]
    v = work.build_view(NOW, cfg=cfg(), ledger_data=ledger_data(rows), owed=NO_OWED, routing_info=ROUTING)
    b = board(v)
    assert b["source"]["state"] == "none" and b["ledger"]["rows"] == 3
    goal = b["ledger"]["goals"][0]
    assert goal["id"] == "g-1" and goal["children"] == 2 and goal["verified"] == 1
    assert ["DONE", "done"] in b["ledger"]["vocab"]["state"] and "goal" in b["ledger"]["vocab"]["kind"]


def test_the_view_holds_no_path_login_folder_or_account_key(tmp_path):
    path = put(tmp_path, summary(jobs=[job(summary="Building from tmp/runner/atlas/job.py")]))
    rows = [row("n-1", "Choose a name", kind="needs-owner", state="NEEDS OWNER")]
    owed = {"items": [{"id": "c1", "text": "Sign the release notes", "project": "atlas", "kind": "blocker",
                       "owner": "user", "opened_at": _t(3600)}], "error": None}
    v = view_for(path, ledger=ledger_data(rows), owed=owed)
    work.write_view(v)
    text = list(strings(v)) + list(strings(json.loads(work.view_file().read_text())))
    assert text
    for needle in (str(tmp_path), "/Users/", "/home/", ACCT_KEY, "logins", "API Keys"):
        assert not any(needle in s for s in text), needle


def test_valid_until_is_the_next_clock_change(tmp_path):
    v = view_for(put(tmp_path, summary()))
    until = datetime.strptime(v["valid_until"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert NOW < until <= NOW + timedelta(minutes=15)


# ---- Waiting on you -----------------------------------------------------------------------------


def commitment(cid="c1", text="Approve the signing certificate renewal", **over):
    base = {"id": cid, "text": text, "project": "atlas", "kind": "question", "owner": "user",
            "opened_at": _t(7200)}
    base.update(over)
    return base


def waiting(rows, items, routing=ROUTING):
    return work.build_view(NOW, cfg=cfg(), ledger_data=ledger_data(rows), owed={"items": items, "error": None},
                           routing_info=routing)["waiting"]


NEEDS = row("n-1", "Approve the signing certificate renewal", kind="needs-owner", state="NEEDS OWNER", ago=86400)


def test_a_withheld_project_is_marked_so_it_is_never_read_as_none(env):
    # The scan withholds a project it can't keep (here a path); the item
    # still had one, so the app must not name it Khipu's own or "no project".
    got = {w["id"]: w for w in waiting([], [commitment("c1", project="/srv/work/aegis"),
                                            commitment("c2", text="Pick the colour", project=None),
                                            commitment("c3", text="Name the release", project="acme/atlas")])}
    assert got["c1"]["project"] is None and got["c1"].get("masked") is True
    assert got["c2"]["project"] is None and "masked" not in got["c2"]
    assert got["c3"]["project"] == "acme/atlas" and "masked" not in got["c3"]


def test_a_linked_pair_shows_once(env):
    linked = row("n-1", "Something else entirely", kind="needs-owner", state="NEEDS OWNER",
                 refs={"commitment": "c1"})
    got = waiting([linked], [commitment("c1", "Totally different words")])
    assert len(got) == 1 and got[0]["source"] == "ledger" and got[0]["also_owed"] == "c1"
    assert "open_owed" in got[0]["actions"]


def test_an_exact_text_match_links_them(env):
    got = waiting([NEEDS], [commitment("c1", "  approve THE signing   certificate renewal ")])
    assert len(got) == 1 and got[0]["also_owed"] == "c1"


def test_a_near_match_only_suggests_and_not_the_same_removes_it(env):
    near = commitment("c1", "Approve signing certificate renewal")
    got = waiting([NEEDS], [near])
    assert len(got) == 2
    by = {w["source"]: w for w in got}
    assert by["commitment"]["looks_like"] == {"id": "n-1", "title": NEEDS["title"]}
    assert {"same", "not_same"} <= set(by["commitment"]["actions"]) and by["ledger"]["also_owed"] is None
    work.set_link("atlas", "n-1", "c1", False)
    got = waiting([NEEDS], [near])
    assert len(got) == 2 and next(w for w in got if w["source"] == "commitment")["looks_like"] is None
    work.set_link("atlas", "n-1", "c1", True)  # the newest answer wins
    got = waiting([NEEDS], [near])
    assert len(got) == 1 and got[0]["also_owed"] == "c1"


def test_a_hidden_commitment_leaves_until_its_text_changes(env):
    item = commitment("c9", "Pick a colour for the badge")
    assert len(waiting([], [item])) == 1
    work.set_hidden("c9", item)
    assert waiting([], [item]) == []
    assert len(waiting([], [dict(item, text="Pick two colours for the badge")])) == 1
    work.set_hidden("c9", None)
    assert len(waiting([], [item])) == 1


def test_order_blocking_first_then_oldest(env):
    parent = row("g-1", "Release", kind="loose-end", state="IN FLIGHT", seq=1)
    block = row("n-2", "Blocking question", kind="needs-owner", state="NEEDS OWNER", parent="", seq=2, ago=60)
    child = row("n-3", "Child of blocker", kind="slice", state="IN FLIGHT", parent="n-2", seq=3)
    items = [commitment("c1", "Old and plain", opened_at=_t(10 * 86400)),
             commitment("c2", "Newer plain", opened_at=_t(3600)),
             commitment("c3", "A blocker commitment", kind="blocker", opened_at=_t(60))]
    got = waiting([parent, block, child], items)
    assert [w["title"] for w in got] == ["Blocking question", "A blocker commitment", "Old and plain", "Newer plain"]
    assert got[0]["blocking"] and got[1]["blocking"] and not got[2]["blocking"]


def test_routing_items_appear_once_and_never_duplicate(env):
    routing = dict(ROUTING, rules=[{"n": 3, "pending": True, "seeded": False, "confirmed": False, "at": _t(60)},
                                   {"n": 1, "pending": False, "seeded": True, "confirmed": False, "at": _t(900)},
                                   {"n": 2, "pending": False, "seeded": True, "confirmed": False, "at": _t(800)}],
                   holds=[{"id": "h1", "owner": True, "remind": True, "at": _t(95 * 86400),
                           "scope_label": "Muse"}, {"id": "h2", "owner": False, "remind": True, "at": _t(5)}])
    got = waiting([], [], routing)
    titles = [w["title"] for w in got]
    assert "Confirm a standing rule a conversation changed" in titles
    assert "2 seeded rules to confirm" in titles
    assert "Your hold on Muse has been on for 95 days" in titles and len(got) == 3
    assert all(w["source"] == "routing" and w["actions"] == ["open_routing"] for w in got)


def test_a_waiting_list_in_a_file_is_ignored_and_counted(tmp_path, env):
    path = put(tmp_path, summary(waiting_on_owner=[{"text": "Pick a name"}, {"text": "Pick a colour"}]))
    v = view_for(path)
    assert board(v)["source"]["waiting_list_ignored"] == 2 and v["waiting"] == []
    work.set_setting("work_projects", [entry(path)])
    block = work.doctor_block(NOW)
    assert block["sources"][0]["waiting_list_ignored"] == 2
    assert any("waiting list" in w for w in block["warnings"])


# ---- settings, doctor, the CLI ------------------------------------------------------------------


def test_settings_round_trip(env):
    assert work.settings()["work_max_jobs"] == 50 and work.settings()["work_stale_after"] == "auto"
    work.set_setting("work_max_jobs", "0")
    work.set_setting("work_stale_after", "30m")
    assert work.settings()["work_max_jobs"] == 0 and work.settings()["work_stale_after"] == 1800
    status = work.settings_status()
    assert status["work_max_jobs"]["source"] == "file" and status["work_max_prs"]["source"] == "default"
    work.set_setting("work_max_jobs", None)
    assert work.settings()["work_max_jobs"] == 50
    with pytest.raises(ValueError):
        work.set_setting("work_max_jobs", "-3")
    with pytest.raises(KeyError):
        work.set_setting("not_a_setting", "1")


def test_doctor_block_warns_and_lists_sources(tmp_path, env):
    path = put(tmp_path, summary(written_at=_t(5 * 60), jobs=[job(), job("bad id!")]))
    work.set_setting("work_projects", [entry(path)])
    block = work.doctor_block(NOW)
    src = block["sources"][0]
    assert src["state"] == "stale" and src["dropped"] == 1 and src["last_good"]
    assert block["warnings"] and all(str(tmp_path) not in w for w in block["warnings"])
    assert work.doctor_block(NOW)["sources"][0]["project"] == "atlas"


def test_config_and_doctor_wiring(env, monkeypatch):
    from khipu import cli

    args = cli.build_parser().parse_args(["config", "--set", "work_max_jobs", "7"])
    out = io.StringIO()
    with redirect_stdout(out):
        assert args.func(args) == 0
    assert work.settings()["work_max_jobs"] == 7 and json.loads(out.getvalue())["ok"] is True


def test_check_reports_clean_and_problem_files(tmp_path, env):
    rc, out, _ = run("check", str(put(tmp_path, summary())))
    assert rc == 0 and "OK" in out
    bad = put(tmp_path, summary(jobs=[job(), job("bad id!")], waiting_on_owner=[{}]), name="bad.json")
    rc, out, _ = run("check", str(bad))
    assert rc == 1 and "left out" in out and "waiting_on_owner" in out
    rc, out, _ = run("check", str(put(tmp_path, "nope", name="junk.json")))
    assert rc == 1
    rc, out, _ = run("check", str(tmp_path / "runner" / "absent.json"), "--json")
    assert rc == 1 and json.loads(out)["ok"] is False
    keys = tmp_path / "API Keys" / "s.json"
    keys.parent.mkdir()
    keys.write_text(json.dumps(summary()))
    assert run("check", str(keys))[0] == 1


def test_work_json_writes_the_view(tmp_path, env, monkeypatch):
    path = put(tmp_path, summary())
    work.set_setting("work_projects", [entry(path)])
    monkeypatch.setattr(work, "read_ledger", lambda: NO_LEDGER)
    monkeypatch.setattr(work, "read_owed", lambda: NO_OWED)
    monkeypatch.setattr(work, "read_routing", lambda now: ROUTING)
    rc, out, _ = run("--json")
    assert rc == 0
    shown = json.loads(out)
    saved = json.loads((tmp_path / "data" / "state" / "work" / "view.json").read_text())
    assert shown["format"] == saved["format"] == "khipu-work-view" and saved["version"] == 1
    assert saved["projects"][0]["source"]["state"] in ("ok", "stale", "gone")
    rc, text, _ = run()
    assert rc == 0 and "Waiting on you" in text and str(tmp_path) not in text


def test_hide_and_same_commands(env, monkeypatch, khipu_app):
    item = commitment("c5", "Pick a colour")
    monkeypatch.setattr(work, "read_owed", lambda: {"items": [item], "error": None})
    assert run("hide", "c5")[0] == 0 and "c5" in work.read_hidden()
    assert run("hide", "missing")[0] == 1
    assert run("unhide", "c5")[0] == 0 and work.read_hidden() == {}
    assert run("not-same", "atlas#n-1", "c5")[0] == 0 and work.read_links()["not_same"] == [["atlas", "n-1", "c5"]]
    assert run("same", "atlas#n-1", "c5")[0] == 0
    links = work.read_links()
    assert links["same"] == [["atlas", "n-1", "c5"]] and links["not_same"] == []
    assert run("same", "nohash", "c5")[0] == 1


# ---- review regressions (cross-family review of 01813f1) -------------------------------------


def cli_and_saved(monkeypatch, config, ledger=NO_LEDGER, owed=NO_OWED):
    """`khipu work --json` and the saved view, from synthetic inputs only."""
    monkeypatch.setattr(work, "settings", lambda: config)
    monkeypatch.setattr(work, "_now", lambda: NOW)
    monkeypatch.setattr(work, "read_ledger", lambda: ledger)
    monkeypatch.setattr(work, "read_owed", lambda: owed)
    monkeypatch.setattr(work, "read_routing", lambda now: ROUTING)
    rc, out, _ = run("--json")
    assert rc == 0
    return [json.loads(out), json.loads(work.view_file().read_text())]


@pytest.mark.parametrize("field", ["id", "state", "target_pr", "target_ledger", "train_pr", "target_path"])
def test_identifiers_states_and_references_never_carry_a_key_or_path(tmp_path, monkeypatch, field):
    doc = summary()
    secret = "tmp/fictional-login-home/private.txt" if field == "target_path" else KEY
    if field in ("id", "state"):
        doc["jobs"][0][field] = secret
    elif field == "train_pr":
        doc["trains"][0]["prs"] = [secret]
    else:
        doc["jobs"][0]["for"] = {"pr" if field == "target_pr" else "ledger": secret}
    path = put(tmp_path, doc)
    for result in cli_and_saved(monkeypatch, cfg(work_projects=[entry(path)])):
        assert secret not in json.dumps(result)


@pytest.mark.parametrize("source", ["name", "ledger", "commitment", "account_key"])
def test_names_ledger_titles_and_commitments_lose_paths_and_account_keys(tmp_path, monkeypatch, source):
    config = cfg(work_projects=[entry(put(tmp_path, summary()))])
    needle = "/tmp/fictional-login-home/private.txt"
    ledger, owed = NO_LEDGER, NO_OWED
    if source == "account_key":
        config["work_projects"][0]["name"] = needle = ACCT_KEY
    elif source == "name":
        config["work_projects"][0]["name"] = needle + " --password invented-probe-password"
    elif source == "ledger":
        ledger = ledger_data([row("n-1", "Read " + needle, state="NEEDS OWNER")])
    else:
        owed = {"items": [commitment(text="Read " + needle + " --password invented-probe-password")],
                "error": None}
    for result in cli_and_saved(monkeypatch, config, ledger, owed):
        assert needle not in json.dumps(result) and "invented-probe-password" not in json.dumps(result)


def test_the_final_scan_withholds_any_private_string_left(tmp_path):
    # A ledger project key is the owner's free text: a path-shaped one is
    # withheld by the last pass over the finished view, not shown.
    rows = [row("n-1", "Choose a name", state="NEEDS OWNER")]
    data = ledger_data(rows, project="/tmp/fictional-login-home/project")
    v = work.build_view(NOW, cfg=cfg(), ledger_data=data, owed=NO_OWED, routing_info=ROUTING)
    assert "fictional-login-home" not in json.dumps(v)
    assert v["waiting"] and v["waiting"][0]["id"] == "n-1"


@pytest.mark.parametrize("case", ["job_kind", "verdict_outcome", "time_overflow"])
def test_a_list_valued_field_or_an_overflowing_time_drops_only_its_item(tmp_path, case):
    doc = summary()
    if case == "job_kind":
        doc["jobs"] = [job("bad", kind=[]), job("good")]
    elif case == "verdict_outcome":
        doc["prs"] = [pr("1", verdict={"outcome": []}), pr("2")]
    else:
        doc["jobs"] = [job("bad", updated_at="0001-01-01T00:00:00+23:59"), job("good")]
    got = read(put(tmp_path, doc))
    assert got["state"] == "ok"
    c = got["contents"]
    if case == "job_kind":  # a kind that is not text counts as no kind
        assert [(j["id"], j["kind"]) for j in c["jobs"]] == [("bad", None), ("good", "fix")]
    elif case == "verdict_outcome":  # an outcome that is not text counts as no verdict
        assert [(p["id"], p["verdict"]) for p in c["prs"]] == [("1", None), ("2", None)]
    else:  # updated_at is required: only that job is left out
        assert [j["id"] for j in c["jobs"]] == ["good"] and c["dropped"] == 1


def test_an_older_summary_never_replaces_the_last_good_read(tmp_path):
    path = put(tmp_path, summary(jobs=[job("new")]))
    assert read(path)["contents"]["jobs"][0]["id"] == "new"
    put(tmp_path, summary(written_at=_t(3600), jobs=[job("old")]))
    got = read(path)
    assert got["contents"]["jobs"][0]["id"] == "new" and got["state"] == "unreadable"
    assert "older" in got["problem"]


@pytest.mark.parametrize("broken", ["unreadable", "too_big"])
def test_a_failed_source_stops_showing_jobs_live_once_gone(tmp_path, broken):
    path = put(tmp_path, summary())
    assert read(path)["state"] == "ok"
    over = {}
    if broken == "unreadable":
        path.write_text("{")
    else:
        over["work_file_max_bytes"] = 1
    later = NOW + timedelta(days=2)
    v = work.build_view(later, cfg=cfg(work_projects=[entry(path)], **over), ledger_data=NO_LEDGER,
                        owed=NO_OWED, routing_info=ROUTING)
    assert v["live"] == [] and v["counts"]["running"] == 0
    # Within work_gone_after the last good read still shows live.
    v = work.build_view(NOW, cfg=cfg(work_projects=[entry(path)], **over), ledger_data=NO_LEDGER,
                        owed=NO_OWED, routing_info=ROUTING)
    assert [j["id"] for j in v["live"]] == ["job-1"]


@pytest.mark.parametrize("which", ["stale", "finished", "merged"])
def test_zero_lifts_the_stale_finished_and_merged_limits(tmp_path, which):
    if which == "stale":
        path = put(tmp_path, summary(written_at=_t(3600)))
        assert read(path, work_stale_after=0)["state"] == "ok"
    elif which == "finished":
        path = put(tmp_path, summary(jobs=[job(state="done", ended_at=_t(3600))]))
        assert board(view_for(path, work_finished_for=0))["jobs"]
    else:
        path = put(tmp_path, summary(prs=[pr(stage="merged", merged_at=_t(9 * 86400))]))
        assert board(view_for(path, work_merged_for=0))["merged"]


def test_a_shipped_or_dropped_train_ages_out_with_merged_pull_requests(tmp_path):
    old = [train("t1", state="shipped", updated_at=_t(9 * 86400)),
           train("t2", state="dropped", updated_at=_t(9 * 86400)), train("t3", state="shipped", updated_at=_t(60))]
    path = put(tmp_path, summary(trains=old))
    assert [t["id"] for t in board(view_for(path))["trains"]] == ["t3"]
    assert len(board(view_for(path, work_merged_for=0))["trains"]) == 3


def test_a_linked_blocker_commitment_keeps_its_blocking_priority(env):
    rows = [row("n-1", "Approve licence", state="NEEDS OWNER")]
    got = waiting(rows, [commitment("c1", "Approve licence", kind="blocker")])
    assert len(got) == 1 and got[0]["blocking"] and got[0]["also_owed"] == "c1"


def test_a_summary_inside_a_configured_login_folder_is_refused(tmp_path, monkeypatch):
    from khipu import config

    login = tmp_path / "fictional-login-home"
    login.mkdir()
    path = login / "work-summary.json"
    path.write_text(json.dumps(summary()))
    monkeypatch.setattr(config, "load_config", lambda: {"usage_accounts": [
        {"label": "Synthetic", "harness": "claude", "home": str(login)}]})
    opened = []
    real = os.open
    monkeypatch.setattr(os, "open", lambda p, *a, **k: (opened.append(os.fspath(p)), real(p, *a, **k))[1])
    assert read(path)["state"] == "refused"
    assert path.name not in opened and str(path) not in opened


def test_a_symlink_is_never_followed_even_when_swapped_after_the_checks(tmp_path, monkeypatch):
    public = put(tmp_path, summary())
    target = tmp_path / "auth.json"  # synthetic, holds no credential
    target.write_text(json.dumps(summary(jobs=[job("credential-target")])))
    link = tmp_path / "summary-link.json"
    link.symlink_to(public)
    real = os.open

    def swap(p, flags, *a, **k):
        if os.fspath(p) in (str(link), link.name):
            link.unlink()
            link.symlink_to(target)
        return real(p, flags, *a, **k)

    monkeypatch.setattr(os, "open", swap)
    got = read(link)
    assert got["state"] == "refused" and got["contents"] is None


# ---- review regressions, round 2 (cross-family review of 0e8d5b3) ----------------------------


def test_a_failed_privacy_table_withholds_every_name_and_says_so(tmp_path, monkeypatch):
    from khipu import agent_view, config

    login_name = "fictional-login-home"
    monkeypatch.setattr(config, "load_config", lambda: {"usage_accounts": [
        {"harness": "claude", "label": "Synthetic", "home": str(tmp_path / login_name)}]})
    path = put(tmp_path, summary(jobs=[job(login_name, account=login_name, summary=login_name)]))

    def broken(*args):
        raise OSError("synthetic privacy-table read failure")

    monkeypatch.setattr(agent_view, "_table", broken)
    for document in cli_and_saved(monkeypatch, cfg(work_projects=[entry(path)])):
        assert login_name not in json.dumps(document)
        assert document["problems"]["privacy"] and document["format"] == "khipu-work-view"
        assert document["counts"]["running"] == 1  # the shape and counts stay


def test_an_explicit_commitment_link_beats_a_text_match(env):
    rows = [row("text-match", "Approve licence", state="NEEDS OWNER"),
            row("explicit", "Approve the release", state="NEEDS OWNER", refs={"commitment": "c1"})]
    by_id = {w["id"]: w for w in waiting(rows, [commitment("c1", "Approve licence")])}
    assert by_id["explicit"]["also_owed"] == "c1" and by_id["text-match"]["also_owed"] is None


def test_not_the_same_holds_even_when_the_words_later_match(env):
    rows = [row("n1", "Approve the licence", state="NEEDS OWNER")]
    assert len(waiting(rows, [commitment("c1", "Approve licence")])) == 2
    work.set_link("atlas", "n1", "c1", False)
    assert len(waiting(rows, [commitment("c1", "Approve the licence")])) == 2


def test_the_run_cap_is_per_project(tmp_path):
    runs = [{"name": f"Run {i}", "state": "passed", "at": _t(30 + i)} for i in range(2)]
    path = put(tmp_path, summary(trains=[train("t1", runs=runs), train("t2", runs=runs)]))
    b = board(view_for(path, work_max_runs=2))
    assert sum(len(t["runs"]) for t in b["trains"]) == 2
    assert b["source"]["capped"]["runs"] == {"shown": 2, "of": 4}


def test_a_note_only_history_entry_does_not_reset_when_a_row_entered_needs_you():
    rows = [row("n1", "Approve licence", state="NEEDS OWNER")]
    history = [{"row_id": "n1", "from_state": "IN FLIGHT", "to_state": "NEEDS OWNER", "at": _t(86400)},
               {"row_id": "n1", "from_state": "NEEDS OWNER", "to_state": "NEEDS OWNER", "at": _t(30)}]
    assert work._entered(None, "atlas", rows, history)["n1"] == work._parse_time(_t(86400))


def test_another_file_with_the_same_time_and_size_is_read_again(tmp_path):
    first = put(tmp_path, summary(jobs=[job("job-one")]), name="first.json")
    second = put(tmp_path, summary(jobs=[job("job-two")]), name="second.json")
    os.utime(second, ns=(first.stat().st_atime_ns, first.stat().st_mtime_ns))
    assert first.stat().st_size == second.stat().st_size
    assert read(first)["contents"]["jobs"][0]["id"] == "job-one"
    assert read(second)["contents"]["jobs"][0]["id"] == "job-two"


def test_valid_until_counts_the_ledger_changed_today_clock():
    rows = [row("n1", "Check the release", ago=86399)]

    def at(now):
        return work.build_view(now, cfg=cfg(), ledger_data=ledger_data(rows), owed=NO_OWED, routing_info=ROUTING)

    view = at(NOW)
    assert view["projects"][0]["ledger"]["changed_today"]
    assert not at(NOW + timedelta(seconds=2))["projects"][0]["ledger"]["changed_today"]
    assert view["valid_until"] == _t(-1)


def test_a_link_in_a_parent_folder_is_refused(tmp_path):
    target = put(tmp_path, summary())
    alias = tmp_path / "alias"
    alias.symlink_to(target.parent, target_is_directory=True)
    got = read(alias / target.name)
    assert got["state"] == "refused" and got["contents"] is None


# ---- review regressions, round 3 (cross-family review of 2a69343) ----------------------------


@pytest.mark.parametrize("variable", ["CODEX_HOME", "GROK_HOME"])
def test_a_login_folder_set_through_the_environment_is_refused_and_scrubbed(tmp_path, monkeypatch, variable):
    from khipu import agent_view

    login = tmp_path / "fictional-login-739"
    login.mkdir()
    inside = login / "work-summary.json"
    inside.write_text(json.dumps(summary()))
    monkeypatch.setenv(variable, str(login))
    agent_view._TABLE.update(sig=None, at=0.0, table=None)
    opened = []
    real = safe_read.open_file
    monkeypatch.setattr(safe_read, "open_file", lambda p, *a: (opened.append(p), real(p, *a))[1])
    assert read(inside)["state"] == "refused" and opened == []
    # A summary elsewhere that names the folder: the name never leaves.
    path = put(tmp_path, summary(jobs=[job(summary="Checking fictional-login-739")]))
    monkeypatch.setattr(safe_read, "open_file", real)
    for result in cli_and_saved(monkeypatch, cfg(work_projects=[entry(path)])):
        assert "fictional-login-739" not in json.dumps(result)


@pytest.mark.parametrize("broken", ["unreadable", "too_big"])
@pytest.mark.parametrize("cap", ["count", "text"])
def test_a_last_good_read_obeys_caps_lowered_since(tmp_path, monkeypatch, broken, cap):
    path = put(tmp_path, summary(jobs=[job(f"job-{i}") for i in range(3)]))
    assert read(path, work_max_jobs=0, work_line_chars=0)["state"] == "ok"
    config = cfg(work_projects=[entry(path)])
    config["work_max_jobs" if cap == "count" else "work_line_chars"] = 1 if cap == "count" else 8
    if broken == "unreadable":
        path.write_text("{")
    else:
        config["work_file_max_bytes"] = 1
    for result in cli_and_saved(monkeypatch, config):
        board_ = result["projects"][0]
        assert board_["source"]["state"] == broken
        if cap == "count":
            assert len(board_["jobs"]) == 1 and board_["source"]["capped"]["jobs"] == {"shown": 1, "of": 3}
        else:
            assert max(len(j["summary"]) for j in board_["jobs"]) <= 8


def test_the_routing_record_deadline_is_in_valid_until(monkeypatch):
    import contextlib

    from khipu import routing

    route_cfg = routing.settings({"routing_owner_hold_reminder": 3600})
    deadline = NOW + timedelta(seconds=60)
    hold = {"type": "hold", "id": "hold-1", "_seq": 1, "scope": {"kind": "all"}, "reason": "keep",
            "text": "Synthetic hold", "until": None, "owner": True, "written_by": routing.OWNER, "via": "app",
            "written_at": _t(3540)}
    monkeypatch.setattr(routing, "read_view", lambda now: routing.fold([hold], now=now, cfg=route_cfg, known=[]))
    monkeypatch.setattr(routing, "settings", lambda: route_cfg)
    monkeypatch.setattr(routing, "plan_facts", lambda view, now: {})
    monkeypatch.setattr(routing, "lock_budget", lambda *a: contextlib.nullcontext())

    def at(now):
        return work.build_view(now, cfg=cfg(), ledger_data=NO_LEDGER, owed=NO_OWED,
                               routing_info=work.read_routing(now))

    before = at(NOW)
    assert before["counts"]["waiting"] == 0 and at(deadline)["counts"]["waiting"] == 1
    assert before["valid_until"] == work._z(deadline)


def test_a_refresh_at_the_exact_deadline_sees_the_change(tmp_path):
    path = put(tmp_path, summary())

    def at(now):
        return work.build_view(now, cfg=cfg(work_projects=[entry(path)]), ledger_data=NO_LEDGER,
                               owed=NO_OWED, routing_info=ROUTING)

    first = at(NOW)
    deadline = work._parse_time(first["valid_until"])
    assert first["projects"][0]["source"]["state"] == "ok"
    exact = at(deadline)
    # The state at the deadline is the new one, or the next deadline is
    # no later than the first instant the new state shows.
    if exact["projects"][0]["source"]["state"] == "ok":
        following = work._parse_time(exact["valid_until"])
        assert following is not None and following <= deadline + timedelta(seconds=1)
    assert at(deadline + timedelta(seconds=1))["projects"][0]["source"]["state"] == "stale"


# ---- review regressions, round 4 (cross-family review of 6287846) ----------------------------


def test_a_relative_path_with_one_separator_never_leaves(tmp_path, monkeypatch):
    path = put(tmp_path, summary(jobs=[job(**{"for": {"ledger": "source/private.txt"}})]))
    for result in cli_and_saved(monkeypatch, cfg(work_projects=[entry(path)])):
        assert "source/private.txt" not in json.dumps(result)


@pytest.mark.parametrize("words", ["Fix CI/CD", "Pick one and/or both", "Finish 3/4 of it", "Due 10/16",
                                   "Open read/write access"])
def test_ordinary_words_with_a_slash_stay_in_a_title(words):
    data = ledger_data([row("n-1", words, state="NEEDS OWNER")])
    v = work.build_view(NOW, cfg=cfg(), ledger_data=data, owed=NO_OWED, routing_info=ROUTING)
    assert [w["title"] for w in v["waiting"]] == [words]


def test_a_path_in_a_title_becomes_a_path_and_the_other_words_stay():
    title = "Fix CI/CD after reading source/private.txt and /tmp/fictional-login-home/notes"
    data = ledger_data([row("n-1", title, state="NEEDS OWNER")])
    v = work.build_view(NOW, cfg=cfg(), ledger_data=data, owed=NO_OWED, routing_info=ROUTING)
    assert [w["title"] for w in v["waiting"]] == ["Fix CI/CD after reading (a path) and (a path)"]
    assert "private.txt" not in json.dumps(v) and "fictional-login-home" not in json.dumps(v)


def test_a_reference_and_a_remembered_same_keep_both_commitments(env):
    linked = row("n-1", "Approve the licence", state="NEEDS OWNER", refs={"commitment": "c1"})
    work.set_link("atlas", "n-1", "c2", True)
    got = waiting([linked], [commitment("c1", "Approve the licence"), commitment("c2", "Approve the release")])
    shown = [w["id"] for w in got if w["source"] == "commitment"]
    shown += [w["also_owed"] for w in got if w["source"] == "ledger" and w["also_owed"]]
    assert sorted(shown) == ["c1", "c2"]


def test_a_fractional_deadline_is_rounded_up_past_now():
    rows = [row("n1", "Check the release", updated_at=NOW - timedelta(seconds=86399.5))]
    view = work.build_view(NOW, cfg=cfg(), ledger_data=ledger_data(rows), owed=NO_OWED, routing_info=ROUTING)
    assert view["projects"][0]["ledger"]["changed_today"]
    assert work._parse_time(view["valid_until"]) > NOW


def test_every_open_user_commitment_is_read(monkeypatch):
    from khipu import commitments, hub_snapshot

    cols = ["id", "text", "project", "owner", "kind", "opened_episode", "opened_at", "due_after", "status",
            "closed_episode", "closed_at", "close_reason"]
    rows = [(i, "Approve the licence", "atlas", "user", "blocker", None, NOW - timedelta(days=i), None, "open",
             None, None, None) for i in range(1, 1202)]
    assert len(cols) == len(rows[0])

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a):
            pass

        def fetchall(self):
            return rows

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(hub_snapshot, "try_hub_connect", lambda **kw: Connection())
    monkeypatch.setattr(commitments, "_not_forgotten_clause", lambda cur: "TRUE")
    monkeypatch.setattr(commitments, "_seen_columns_ready", lambda cur: False)
    monkeypatch.setattr(commitments, "_future_trigger_ready", lambda cur: False)
    got = work.read_owed()
    assert got["error"] is None and len(got["items"]) == len(rows)


def test_the_apps_same_and_unhide_argv_parse(khipu_app):
    """The desktop's fixed argv (src-tauri lib.rs, work_command_tests)."""
    from khipu import cli

    for argv in (["work", "same", "--", "owner/atlas#n-7", "42"], ["work", "unhide", "--", "42"]):
        args = cli.build_parser().parse_args(argv)
        with redirect_stdout(io.StringIO()) as out:
            assert args.func(args) == 0, out.getvalue()
    assert work.read_links()["same"] == [["owner/atlas", "n-7", "42"]]
    args = cli.build_parser().parse_args(["work", "not-same", "--", "owner/atlas#n-7", "42"])
    with redirect_stdout(io.StringIO()):
        assert args.func(args) == 0
    assert work.read_links() == {"same": [], "not_same": [["owner/atlas", "n-7", "42"]]}


def test_a_parent_folder_replaced_after_the_open_is_refused(tmp_path, monkeypatch):
    # The folder is swapped right after its descriptor is taken: reading the
    # old file through the old descriptor would return bytes no longer at
    # the named path.
    parent = tmp_path / "public"
    parent.mkdir()
    (parent / "tracker.json").write_text("old-file")
    real_open = os.open
    swapped = []

    def race(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if path == "public" and not swapped:
            swapped.append(True)
            parent.rename(tmp_path / "moved")
            parent.mkdir()
            (parent / "tracker.json").write_text("named-file")
        return fd

    monkeypatch.setattr(os, "open", race)
    try:
        data, _st = safe_read.read_file(parent / "tracker.json")
    except safe_read.Refused:
        return
    assert data == b"named-file", data


# ---- review regressions, round 4 ------------------------------------------------------------

_DATA_VOLUME = "/System/Volumes/Data"


def _login_on_the_data_volume(tmp_path, monkeypatch):
    """A fake login folder (``CODEX_HOME``) under tmp_path with a summary in
    it, when tmp_path can also be reached through macOS's Data volume."""
    from khipu import agent_view

    real = os.path.realpath(tmp_path)
    if not os.path.isdir(_DATA_VOLUME + real):
        pytest.skip("tmp_path is not reachable through the Data volume")
    login = Path(real) / "fictional-login-531"
    login.mkdir()
    inside = login / "work-summary.json"
    inside.write_text(json.dumps(summary()))
    monkeypatch.setenv("CODEX_HOME", str(login))
    agent_view._TABLE.update(sig=None, at=0.0, table=None)
    return inside


def test_a_login_folder_reached_through_another_name_for_the_disk_is_refused(tmp_path, monkeypatch):
    # /System/Volumes/Data/<path> is the very folder at <path>: a name check
    # alone misses it, the folder's identity does not.
    inside = _login_on_the_data_volume(tmp_path, monkeypatch)
    alias = _DATA_VOLUME + str(inside)
    assert os.path.samefile(alias, inside)
    with pytest.raises(safe_read.Refused):
        safe_read.read_file(inside)
    with pytest.raises(safe_read.Refused):
        safe_read.read_file(alias)
    assert read(Path(alias))["state"] == "refused"


def test_safe_read_refuses_a_named_path_whose_dotdot_crosses_a_link(tmp_path):
    # Collapsing "link/.." lexically would open tmp_path/summary.json, a
    # different file from the one named (outside/summary.json).
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    (outside / "summary.json").write_bytes(b"named")
    (tmp_path / "summary.json").write_bytes(b"other")
    (tmp_path / "link").symlink_to(outside / "nested", target_is_directory=True)
    for named in (tmp_path / "link" / ".." / "summary.json", f"{tmp_path}/./summary.json"):
        with pytest.raises(safe_read.Refused):
            safe_read.read_file(named)


def test_a_source_set_to_another_file_never_shows_the_previous_files_jobs(tmp_path):
    first = put(tmp_path, summary(), "first.json")
    assert work.read_source(entry(first), cfg(), NOW)["contents"]["jobs"]
    replacement = put(tmp_path, "{", "replacement.json")
    got = work.read_source(entry(replacement), cfg(), NOW)
    assert got["state"] == "unreadable"
    assert not got["contents"]


def test_a_snooze_ending_is_a_deadline_of_the_view(monkeypatch):
    from khipu import commitments, hub_snapshot

    deadline = NOW + timedelta(seconds=60)
    clock = [NOW]
    parked = {"id": 42, "text": "Approve the release", "project": "atlas", "owner": "user",
              "kind": "blocker", "opened_at": NOW - timedelta(days=2), "due_after": deadline}

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Conn:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(hub_snapshot, "try_hub_connect", lambda **kw: Conn())
    # One read of every open commitment; the snooze is decided against the
    # view's own time.
    monkeypatch.setattr(commitments, "list_owed", lambda cur, **kw: [dict(parked)])

    def view():
        return work.build_view(clock[0], cfg=cfg(), ledger_data=NO_LEDGER, owed=work.read_owed(clock[0]),
                               routing_info=ROUTING)

    before = view()
    clock[0] = deadline
    after = view()
    assert before["counts"]["waiting"] == 0 and after["counts"]["waiting"] == 1
    assert before["valid_until"] == work._z(deadline)
