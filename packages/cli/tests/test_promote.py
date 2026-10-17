"""Promotion gate, phase 1 (docs/plans/2026-10-14-promotion-gate.md).

Every test runs under its own temporary HOME; the gate's subprocesses are
mocked (no hub, no network, no keychain). The one real subprocess is git, on a
throwaway repository created here.
"""
from __future__ import annotations

import builtins
import io
import json
import os
import plistlib
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from khipu import integrations, launchd_gen
from khipu import promote as pm

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "gate_transcript.jsonl"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(integrations, "HOME", h)
    # paths.DEFAULT_DIR was fixed at import; point the data dir (query_log.jsonl) here too.
    monkeypatch.setenv("KHIPU_DATA_DIR", str(h / ".config" / "khipu"))
    return h


def _git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=gate", "-c", "user.email=gate@example.invalid",
                    "-c", "commit.gpgsign=false", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def _commit(repo: Path, version: str) -> str:
    pkg = repo / "packages" / "cli" / "khipu"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(f'__version__ = "{version}"\n')
    bin_dir = repo / "packages" / "cli" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "khipu-stop-hook").write_text("#!/bin/sh\nexit 0\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", f"v{version}", cwd=repo)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True,
                          text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git("init", "-q", cwd=r)
    first = _commit(r, "0.0.1")
    second = _commit(r, "0.0.2")
    (r / ".python_libs").mkdir()
    return r, first, second


def _fake_checkout(path: Path) -> Path:
    (path / "packages" / "cli" / "khipu").mkdir(parents=True, exist_ok=True)
    (path / "packages" / "cli" / "bin").mkdir(parents=True, exist_ok=True)
    return path


def _gate_ready(path: Path) -> Path:
    """A fake candidate carrying the marks of a checkout whose capture is
    isolated (KHIPU_ISOLATED_DIR, the stop hook's gate mode)."""
    _fake_checkout(path)
    for rel, mark in pm._ISOLATION_MARKS:
        (path / rel).write_text(mark + "\n")
    (path / "packages" / "cli" / "bin" / "khipu-stop-hook").write_text("#!/bin/sh\n")
    return path


# ---- flip ------------------------------------------------------------------


def test_flip_is_atomic_and_reversible(home, repo, monkeypatch):
    r, first, second = repo
    a, b = pm.slots()
    assert pm._prepare_slot(a, first, r)["ok"]
    assert pm._prepare_slot(b, second, r)["ok"]
    # The untracked vendored libs are linked into each slot for the launchers.
    assert (a / ".python_libs").is_symlink() and (a / ".python_libs").resolve() == (r / ".python_libs").resolve()

    assert pm.live_root() is None
    assert pm.flip(a)["ok"]
    assert pm.active_slot() == a and pm.inactive_slot() == b
    assert pm._head(pm.live_link()) == first

    # The new link is made under a temporary name and renamed over the old
    # one: at the moment of the rename the old link is still in place.
    seen = []
    real_replace = os.replace

    def watching_replace(src, dst):
        seen.append((Path(src).name, Path(dst), os.readlink(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(pm.os, "replace", watching_replace)
    assert pm.flip(b)["ok"]
    (tmp_name, dst, old_target), = seen
    assert tmp_name.startswith(".live.tmp-") and dst == pm.live_link() and old_target == str(a)
    assert pm._head(pm.live_link()) == second
    assert not [p for p in pm.config_dir().iterdir() if p.name.startswith(".live.tmp-")]

    # A rename that fails leaves the link where it was and no temp link behind.
    def failing_replace(src, dst):
        raise OSError("disk said no")

    monkeypatch.setattr(pm.os, "replace", failing_replace)
    out = pm.flip(a)
    assert not out["ok"] and os.readlink(pm.live_link()) == str(b)
    assert not [p for p in pm.config_dir().iterdir() if p.name.startswith(".live.tmp-")]
    monkeypatch.setattr(pm.os, "replace", real_replace)

    # Rollback flips back with no gate.
    monkeypatch.setattr(pm, "run_gate", lambda *_a, **_k: pytest.fail("rollback must not gate"))
    monkeypatch.setattr(pm, "_after_flip", lambda: {"kicked": []})
    back = pm.rollback()
    assert back["ok"], back
    assert os.readlink(pm.live_link()) == str(a) and pm._head(pm.live_link()) == first
    assert pm.records()[-1]["action"] == "rollback"


def test_flip_refuses_a_real_directory_and_a_non_slot(home, tmp_path):
    a, _b = pm.slots()
    _fake_checkout(a)
    assert not pm.flip(_fake_checkout(tmp_path / "elsewhere"))["ok"]
    pm.live_link().mkdir(parents=True)
    out = pm.flip(a)
    assert not out["ok"] and "not a link" in out["error"]


def test_rollback_restores_the_slot_a_failed_run_moved(home, repo, monkeypatch):
    r, first, second = repo
    a, b = pm.slots()
    pm._prepare_slot(a, first, r)
    pm._prepare_slot(b, second, r)
    assert pm.flip(a)["ok"] and pm.flip(b)["ok"]  # a (first) was live, then b (second)
    assert pm.last_live() == {"live-a": first, "live-b": second}
    # A later failed promotion moved a to second.
    pm._prepare_slot(a, second, r)
    monkeypatch.setattr(pm, "_after_flip", lambda: {"kicked": []})
    out = pm.rollback()
    assert out["ok"] and out["restored"] == first
    assert pm._head(pm.live_link()) == first


def test_rollback_after_rollback_never_activates_an_untested_commit(home, repo, monkeypatch):
    r, first, second = repo
    third = _commit(r, "0.0.3")
    a, b = pm.slots()
    monkeypatch.setattr(pm, "_after_flip", lambda: {"kicked": []})
    pm._prepare_slot(a, first, r)
    assert pm.flip(a)["ok"]
    # Promote a -> b (second), then roll back b -> a.
    pm._prepare_slot(b, second, r)
    assert pm.flip(b)["ok"]
    back = pm.rollback()
    assert back["ok"] and pm._head(pm.live_link()) == first
    # A failed (or gate-only) run now moves b to a commit that was never live.
    pm._prepare_slot(b, third, r)
    pm.record({"action": "promote", "ok": False, "slot": str(b), "commit": third})
    again = pm.rollback()
    assert again["ok"], again
    assert again["restored"] == second and pm._head(pm.live_link()) == second
    assert pm._head(pm.live_link()) != third


def test_rollback_refuses_a_slot_that_was_never_live(home, repo, monkeypatch):
    r, first, second = repo
    a, b = pm.slots()
    pm._prepare_slot(a, first, r)
    pm._prepare_slot(b, second, r)  # checked out by a gate-only run, never live
    assert pm.flip(a)["ok"]
    monkeypatch.setattr(pm, "_after_flip", lambda: pytest.fail("no flip, so nothing to restart"))
    out = pm.rollback()
    assert not out["ok"] and "nothing known-good" in out["error"]
    assert os.readlink(pm.live_link()) == str(a)


# ---- promote ---------------------------------------------------------------


def _ok(detail="ok"):
    return lambda candidate: {"ok": True, "ms": 1, "detail": detail}


def test_a_failing_gate_step_leaves_the_link_and_records_the_failure(home, monkeypatch, tmp_path):
    a, b = pm.slots()
    _fake_checkout(a)
    assert pm.flip(a)["ok"]
    monkeypatch.setattr(pm, "_repo", lambda: tmp_path)
    monkeypatch.setattr(pm, "_fetch_and_resolve", lambda repo, ref: {"ok": True, "commit": "c0ffee"})
    monkeypatch.setattr(pm, "_prepare_slot",
                        lambda slot, commit, repo: (_fake_checkout(slot), {"ok": True})[1])
    monkeypatch.setattr(pm, "_head", lambda root: "headsha" if root else None)
    monkeypatch.setattr(pm, "_after_flip", lambda: pytest.fail("no flip, so nothing to restart"))
    later = []
    monkeypatch.setattr(pm, "GATE_STEPS", {
        "doctor": _ok(),
        "recall": lambda c: {"ok": False, "ms": 7, "detail": "prompt 1: empty answer where live had 3 hit(s)"},
        "capture": lambda c: later.append("capture"),
        "version": lambda c: later.append("version"),
    })
    out = pm.promote("origin/main")
    assert not out["ok"] and out["failed_step"] == "recall"
    assert os.readlink(pm.live_link()) == str(a)
    assert later == []
    last = pm.records()[-1]
    assert last["action"] == "promote" and last["ok"] is False and last["slot"] == str(b)
    assert [s["step"] for s in last["gate"]] == ["doctor", "recall"]
    assert "empty answer" in last["error"]
    assert pm.status()["last_gate"]["ok"] is False


def test_a_passing_gate_flips_and_restarts(home, monkeypatch, tmp_path):
    a, b = pm.slots()
    _fake_checkout(a)
    assert pm.flip(a)["ok"]
    monkeypatch.setattr(pm, "_repo", lambda: tmp_path)
    monkeypatch.setattr(pm, "_fetch_and_resolve", lambda repo, ref: {"ok": True, "commit": "c0ffee"})
    monkeypatch.setattr(pm, "_prepare_slot",
                        lambda slot, commit, repo: (_fake_checkout(slot), {"ok": True})[1])
    monkeypatch.setattr(pm, "_head", lambda root: "headsha" if root else None)
    monkeypatch.setattr(pm, "GATE_STEPS", {name: _ok() for name in pm.GATE_ORDER})
    calls, envs, live_at = [], [], []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        envs.append(kwargs.get("env") or {})
        if argv[1:3] == ["-c", pm._PRECOMPILE] and argv[-1] == str(pm.live_link() / "packages" / "cli" / "khipu"):
            live_at.append(os.readlink(pm.live_link()))  # the slot live while it compiled
        return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "refreshed": []}), "")

    monkeypatch.setattr(pm, "_run", fake_run)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")  # as every harness that starts a hook has it
    from khipu.jobs import PLIST_QUEUE_DRAIN, _plist_path

    _plist_path(PLIST_QUEUE_DRAIN).parent.mkdir(parents=True, exist_ok=True)
    _plist_path(PLIST_QUEUE_DRAIN).write_text("<plist/>")
    out = pm.promote()
    assert out["ok"], out
    assert os.readlink(pm.live_link()) == str(b)
    # Live hooks never write bytecode: the slot is compiled by its own path
    # before its gate, and the live link's path just before the flip (the old
    # code, checked-hash) and after it (the new code), before the restarts.
    compiled = [argv[-1] for argv in calls if argv[1:3] == ["-c", pm._PRECOMPILE]]
    live_pkg = str(pm.live_link() / "packages" / "cli" / "khipu")
    assert compiled == [str(b / "packages" / "cli" / "khipu"), live_pkg, live_pkg]
    assert live_at == [str(a), str(b)]
    assert out["precompile"]["ok"] and out["precompile_live"]["ok"] and out["after"]["precompile"]["ok"]
    restarts = [i for i, argv in enumerate(calls) if argv[:1] == ["launchctl"] or argv[-2:] == ["jobs", "refresh"]]
    assert max(i for i, argv in enumerate(calls) if argv[-1] == live_pkg) < min(restarts)
    for argv, env in zip(calls, envs):
        if argv[1:3] == ["-c", pm._PRECOMPILE]:
            assert "PYTHONDONTWRITEBYTECODE" not in env
            assert env["PYTHONPYCACHEPREFIX"] == str(Path.home() / "Library" / "Caches" / "Khipu" / "pycache")
    assert any(argv[-2:] == ["jobs", "refresh"] for argv in calls)
    assert any(argv[:3] == ["launchctl", "kickstart", "-k"] and argv[3].endswith(PLIST_QUEUE_DRAIN)
               for argv in calls)
    assert pm.MCP_NOTE in pm.format_result(out)


def test_precompile_never_leaves_the_previous_slots_bytecode_in_use(home, tmp_path, monkeypatch):
    """Codex review of the cold-start fix: two slots' versions of a module
    with the same size and mtime second matched the timestamp pyc compiled
    under ``live`` for the old slot, so compileall skipped it and a fresh hook
    imported the old code. Bytecode is checked-hash and forced, and the live
    path is compiled again just before the flip (a cache from before this
    change held timestamp pycs): a hook imports the live source right after
    the flip, before the precompile that follows it, and after it. Real compiles, real imports, as a live hook (no bytecode
    writes)."""
    import sys

    files = []
    for name, value in (("slot-a", "old"), ("slot-b", "new")):
        pkg = tmp_path / name / "packages" / "cli" / "khipu"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "probe.py").write_text(f"VALUE = '{value}'\n")
        files.append(pkg / "probe.py")
    stamp = files[0].stat().st_mtime_ns
    for f in files:
        os.utime(f, ns=(stamp, stamp))
    live = tmp_path / "live"
    live.symlink_to(tmp_path / "slot-a")

    def imported() -> str:
        env = pm._code_env(live)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        r = pm._run([sys.executable, "-c", "from khipu.probe import VALUE; print(VALUE)"], env=env)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    # A cache from before checked-hash bytecode: timestamp pycs under ``live``.
    env = pm._code_env(live)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    legacy = "import compileall, sys\nsys.exit(0 if compileall.compile_dir(sys.argv[1], quiet=1) else 1)\n"
    assert pm._run([sys.executable, "-c", legacy, str(live / "packages" / "cli" / "khipu")], env=env).returncode == 0
    assert imported() == "old"
    monkeypatch.setattr(pm, "live_root", lambda: live)
    monkeypatch.setattr(pm, "_hook_python", lambda: sys.executable)
    assert pm._before_flip()["ok"]  # promote's and rollback's step just before the flip
    swap = tmp_path / "swap"
    swap.symlink_to(tmp_path / "slot-b")
    os.replace(swap, live)
    assert imported() == "new"  # the old slot's bytecode is never served, even before a precompile
    assert pm.precompile(tmp_path / "slot-b", python=sys.executable)["ok"]
    assert pm.precompile(live, python=sys.executable)["ok"]
    assert imported() == "new"


# ---- the replay ------------------------------------------------------------


class _SpyEnviron(dict):
    """os.environ stand-in that records every variable name read."""

    def __init__(self, data, reads):
        super().__init__(data)
        self.reads = reads

    def __getitem__(self, key):
        self.reads.append(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.reads.append(key)
        return super().get(key, default)

    def __contains__(self, key):
        self.reads.append(key)
        return super().__contains__(key)

    def __iter__(self):
        self.reads.append("*every variable*")
        return super().__iter__()

    def keys(self):
        self.reads.append("*every variable*")
        return super().keys()

    def items(self):
        self.reads.append("*every variable*")
        return super().items()

    def values(self):
        self.reads.append("*every variable*")
        return super().values()

    def copy(self):
        self.reads.append("*every variable*")
        return dict(super().items())


def _summary(when: datetime, reason: str, hits: list[str]) -> str:
    """A prompt-recall summary line, as the hook writes into its own log."""
    stamp = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    return (f"{stamp} [khipu-prompt-recall] session=s1 reason={reason} ms=300.0 hits={hits} "
            f"legs=['lexical'] degraded=None")


def _query(when: datetime, query, harness: str = "claude_code") -> str:
    return json.dumps({"ts": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "harness": harness, "query": query,
                       "mode": "hybrid", "filters": {}, "result_count": 3, "top": []})


def _query_log(home: Path) -> Path:
    from khipu.query_log import log_path

    path = log_path()
    assert str(path).startswith(str(home))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _hooks(home: Path) -> Path:
    live_hook = home / ".config" / "khipu" / "bin" / "khipu-prompt-recall"
    live_hook.parent.mkdir(parents=True, exist_ok=True)
    live_hook.write_text("#!/bin/sh\n")
    candidate = _fake_checkout(home / "candidate")
    (candidate / "packages" / "cli" / "bin" / "khipu-prompt-recall").write_text("#!/bin/sh\n")
    return candidate


def _fake_hook(hits_for, envs=None, payloads=None):
    def fake_run(argv, *, env=None, input=None, cwd=None, timeout=120):
        if argv[1:2] == ["-c"]:
            return subprocess.CompletedProcess(argv, 0, "", "")  # the candidate's routing probe
        if envs is not None:
            envs.append(dict(env))
        if payloads is not None:
            payloads.append(json.loads(input))
        capture_home = Path(env["KHIPU_CAPTURE_HOME"])
        iso = Path(env["KHIPU_ISOLATED_DIR"])
        seeded = json.loads((iso / "config.json").read_text())
        assert "memory_root" not in seeded and "gateway_token" not in seeded
        if "features" in seeded:
            assert seeded["gemini_key_file"] == "/k/file"
            assert (iso / "hub_snapshot.sqlite").is_symlink()
        (capture_home / "logs").mkdir(parents=True, exist_ok=True)
        with (capture_home / "logs" / "prompt-recall.log").open("a") as fh:
            fh.write(_summary(datetime.now(timezone.utc), "ok", hits_for(argv[0])) + "\n")
        return subprocess.CompletedProcess(argv, 0, '{"hookSpecificOutput": {}}\n', "")
    return fake_run


def test_replay_reads_only_the_query_log_and_never_a_credential(home, monkeypatch):
    import khipu.config  # noqa: F401 — imported before the spy is installed
    import khipu.hub_snapshot  # noqa: F401
    import khipu.query_log  # noqa: F401
    import khipu.redact  # noqa: F401
    from khipu.config import config_file

    now = datetime.now(timezone.utc)
    log = _query_log(home)
    log.write_text("\n".join([
        _query(now - timedelta(days=3), "too old to replay"),
        _query(now - timedelta(hours=5), None),  # a redacted gateway line
        _query(now - timedelta(hours=4), "   "),
        _query(now - timedelta(hours=3), "where did we leave the gate"),
        _query(now - timedelta(hours=2), "where did we leave the gate", harness="codex"),
        _query(now - timedelta(hours=1), "deploy note api_key=sk-live-abcdefghijklmnopqrstuvwxyz0123456789",
               harness="mcp"),
        "not json",
    ]) + "\n")
    # Things the replay must never touch.
    secret_file = home / ".config" / "khipu" / "dsn"
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    secret_file.write_text("postgresql://user:hunter2@db.example.invalid/khipu")
    cfg = config_file()
    cfg.write_text(json.dumps({"features": {"x": True}, "memory_root": str(home / "wiki"),
                                         "gemini_key_file": "/k/file", "gateway_token": "not-real"}))
    replica = home / ".config" / "khipu" / "hub_snapshot.sqlite"
    replica.write_bytes(b"replica bytes")
    monkeypatch.setenv("FAKE_PROVIDER_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("GATEWAY_TOKEN", "not-real-either")
    candidate = _hooks(home)
    envs, payloads = [], []
    monkeypatch.setattr(pm, "_run", _fake_hook(
        lambda hook: ["episode:3"] if "candidate" in hook else ["episode:3", "topic:x"], envs, payloads))
    opened = []
    real_open = builtins.open

    def recording_open(file, *args, **kwargs):
        opened.append(Path(os.fspath(file)) if not isinstance(file, int) else file)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", recording_open)
    monkeypatch.setattr(io, "open", recording_open)
    reads: list[str] = []
    monkeypatch.setattr(os, "environ", _SpyEnviron(dict(os.environ), reads))
    out = pm.gate_recall_replay(candidate)
    monkeypatch.undo()

    assert out["ok"], out
    assert set(out) == {"ok", "ms", "detail"}
    assert "2 prompt(s) replayed" in out["detail"]
    # Live, and the candidate with routing off and on, for each of the two distinct queries
    # (the candidate's routing probe is not a hook run).
    assert len(envs) == 6
    assert sorted(env["KHIPU_FEATURE_ROUTING_PUSH"] for env in envs) == ["0"] * 4 + ["1"] * 2
    for env in envs:
        assert not [k for k in env if "KEY" in k or "TOKEN" in k], env
        assert env["KHIPU_RECALL_DAEMON"] == "0"
        # Every write (log, dedup state, embedding cache, budget) lands in an
        # isolated dir; old live code is kept there by KHIPU_DATA_DIR too.
        iso = Path(env["KHIPU_ISOLATED_DIR"])
        assert "khipu-gate-recall-" in str(iso) and env["KHIPU_CAPTURE_HOME"] == str(iso / "capture")
        # Neither hook here knows KHIPU_ISOLATED_DIR, so its data dir is pointed in too.
        assert env["KHIPU_DATA_DIR"] == str(iso)
    assert replica.read_bytes() == b"replica bytes"
    prompts = [p["prompt"] for p in payloads]
    assert "where did we leave the gate" in prompts
    assert not [p for p in prompts if "sk-live-abcdefghij" in p], prompts  # masked before it is sent
    for p in payloads:
        assert p["hook_event_name"] == "UserPromptSubmit" and p["session_id"].startswith("khipu-gate-")
    assert "*every variable*" not in reads
    assert not [k for k in reads if "KEY" in k or "TOKEN" in k], reads
    files = [p for p in opened if isinstance(p, Path)]
    assert log in files
    assert secret_file not in files
    for p in files:
        # The candidate's own source is read to see whether it knows KHIPU_ISOLATED_DIR.
        assert p in (log, cfg) or "khipu-gate-recall-" in str(p) or candidate in p.parents, p


def test_replay_refuses_an_empty_answer_where_live_had_one(home, monkeypatch):
    _query_log(home).write_text(_query(datetime.now(timezone.utc) - timedelta(hours=1), "the queue drain path") + "\n")
    candidate = _hooks(home)
    monkeypatch.setattr(pm, "_run", _fake_hook(lambda hook: [] if "candidate" in hook else ["episode:9"]))
    out = pm.gate_recall_replay(candidate)
    assert not out["ok"] and "empty answer where live had 1 hit" in out["detail"]


def _cosine_only_hook(runs: list, cand_late: list[bool], live_late: tuple[bool, ...] = (False,),
                      note: str = "embedding late"):
    """A fake hook for a query whose words match nothing: its hits come from
    the cosine leg alone, so a run whose embedding was late answers nothing
    (``legs=['lexical'] degraded=embedding late``). The candidate's runs with
    routing off are late as ``cand_late`` says in order (then on time), and
    live's as ``live_late``."""
    def fake_run(argv, *, env=None, input=None, cwd=None, timeout=120):
        if argv[1:2] == ["-c"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"push": True}) + "\n", "")
        on = env.get("KHIPU_FEATURE_ROUTING_PUSH") == "1"
        who = "cand" if "/candidate/packages/" in argv[0] else "live"
        runs.append((who, "on" if on else "off"))
        nth = sum(1 for r in runs if r == (who, "off")) - 1
        plan = cand_late if who == "cand" else live_late
        late = (not on) and nth < len(plan) and plan[nth]
        hits, legs, degraded = ([], ["lexical"], note) if late else (
            ["episode:3"], ["lexical", "cosine"], None)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = (f"{stamp} [khipu-prompt-recall] session=s1 reason=ok ms=300.0 hits={hits} legs={legs} "
                f"degraded={degraded}" + (" routing=full" if on else ""))
        log = Path(env["KHIPU_CAPTURE_HOME"]) / "logs" / "prompt-recall.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(line + "\n")
        return subprocess.CompletedProcess(argv, 0, '{"hookSpecificOutput": {}}\n', "")
    return fake_run


def test_replay_passes_an_empty_answer_whose_embedding_was_late_once(home, monkeypatch):
    # 2026-10-16: a promotion refused with "empty answer where live had 3
    # hit(s)" for a query whose hits are cosine-only. Replayed by hand, live
    # and the candidate alike answered nothing whenever their embedding was
    # late; the gate failed the candidate on one late embedding.
    candidate = _one_query(home)
    runs: list = []
    monkeypatch.setattr(pm, "_run", _cosine_only_hook(runs, cand_late=[True]))
    out = pm.gate_recall_replay(candidate)
    assert out["ok"], out
    assert "prompt 1: 1 hit(s), 1 shared with live" in out["detail"]
    # Live and the candidate were both replayed once before the step went on.
    assert runs[:4] == [("live", "off"), ("cand", "off"), ("live", "off"), ("cand", "off")]


def test_replay_notes_an_empty_answer_whose_legs_differ_from_live_twice(home, monkeypatch):
    candidate = _one_query(home)
    monkeypatch.setattr(pm, "_run", _cosine_only_hook([], cand_late=[True, True, True]))
    out = pm.gate_recall_replay(candidate)
    assert out["ok"], out
    assert "empty where live had 1 hit(s) not counted" in out["detail"]
    assert "degraded=embedding late" in out["detail"]


def test_replay_refuses_an_embedding_that_failed_on_every_prompt_while_live_s_ran(home, monkeypatch):
    # A failed embedding on one cold run is the provider; on every replayed
    # prompt while live's ran, it is the candidate's code.
    candidate = _one_query(home)
    monkeypatch.setattr(pm, "_run", _cosine_only_hook([], cand_late=[True] * 4, note="embedding error"))
    out = pm.gate_recall_replay(candidate)
    assert not out["ok"], out
    assert "the candidate's embedding failed on all 1 prompt(s) while live's ran" in out["detail"]


def test_replay_passes_an_embedding_that_failed_once_then_ran(home, monkeypatch):
    candidate = _one_query(home)
    monkeypatch.setattr(pm, "_run", _cosine_only_hook([], cand_late=[True], note="embedding error"))
    out = pm.gate_recall_replay(candidate)
    assert out["ok"], out


def test_replay_still_refuses_an_empty_answer_when_the_replay_ran_live_s_legs(home, monkeypatch):
    # Late once, then empty with the very legs live ran: that is the code.
    candidate = _one_query(home)

    def fake_run(argv, *, env=None, input=None, cwd=None, timeout=120):
        if argv[1:2] == ["-c"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        cand = "/candidate/packages/" in argv[0]
        seen.append(cand)
        late = cand and seen.count(True) == 1
        hits = [] if cand else ["episode:3"]
        legs, degraded = (["lexical"], "embedding late") if late else (["lexical", "cosine"], None)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log = Path(env["KHIPU_CAPTURE_HOME"]) / "logs" / "prompt-recall.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"{stamp} [khipu-prompt-recall] session=s1 reason=ok ms=300.0 hits={hits} "
                       f"legs={legs} degraded={degraded}\n")
        return subprocess.CompletedProcess(argv, 0, '{"hookSpecificOutput": {}}\n', "")

    seen: list = []
    monkeypatch.setattr(pm, "_run", fake_run)
    out = pm.gate_recall_replay(candidate)
    assert not out["ok"] and "prompt 1: empty answer where live had 1 hit(s)" in out["detail"]
    assert seen == [False, True, False, True]


def _routing_hook(runs: list, *, on_status="full", on_hits=("episode:3",), off_hits=("episode:3",),
                  first_on_hits=None, first_on_status=None, push=None, on_reason="ok", on_ms=300.0,
                  again_off=None):
    """A fake hook that answers with routing on (the run's environment says
    so) as ``on_status``, ``on_hits``, ``on_reason`` and ``on_ms``;
    ``first_on_hits`` and ``first_on_status`` only the first time (None: no
    routing field). ``push`` is what the candidate's probe says (None: it
    says nothing). ``again_off`` is ``(reason, ms, hits)`` for the
    candidate's second run with routing off (the replayed one), which then
    ran no legs."""
    def fake_run(argv, *, env=None, input=None, cwd=None, timeout=120):
        if argv[1:2] == ["-c"]:
            runs.append(("prefold", env["PYTHONPATH"], env["KHIPU_ISOLATED_DIR"]))
            out = "" if push is None else json.dumps({"push": push}) + "\n"
            return subprocess.CompletedProcess(argv, 0, out, "")
        on = env.get("KHIPU_FEATURE_ROUTING_PUSH") == "1"
        iso = Path(env["KHIPU_ISOLATED_DIR"])
        record = (iso / "state" / "routing" / "events.jsonl").is_file()
        runs.append(("on" if on else "off", "/candidate/packages/" in argv[0], record))
        hits = list(off_hits)
        status = on_status
        reason, ms = "ok", 300.0
        if on:
            first = sum(1 for r in runs if r[0] == "on") == 1
            hits = list(first_on_hits if first_on_hits is not None and first else on_hits)
            status = first_on_status if first_on_status is not None and first else on_status
            reason, ms = on_reason, on_ms
        again = not on and again_off is not None and sum(1 for r in runs if r[:2] == ("off", True)) == 2
        if again:
            reason, ms, hits = again_off[0], again_off[1], list(again_off[2])
        line = _summary(datetime.now(timezone.utc), reason, hits).replace("ms=300.0", f"ms={ms}")
        if again:
            line = line.replace("legs=['lexical']", "legs=[]")
        line += f" routing={status}" if on and status else ""
        log = Path(env["KHIPU_CAPTURE_HOME"]) / "logs" / "prompt-recall.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(line + "\n")
        return subprocess.CompletedProcess(argv, 0, '{"hookSpecificOutput": {}}\n', "")
    return fake_run


def _one_query(home) -> Path:
    _query_log(home).write_text(_query(datetime.now(timezone.utc) - timedelta(hours=1), "the routing gate") + "\n")
    return _hooks(home)


def test_replay_fails_a_candidate_whose_routing_read_is_over_budget(home, monkeypatch):
    candidate = _one_query(home)
    runs: list = []
    monkeypatch.setattr(pm, "_run", _routing_hook(runs, on_status="skipped:budget"))
    out = pm.gate_recall_replay(candidate)
    assert not out["ok"]
    assert "with routing on, its routing read ran out of its budget (routing=skipped:budget)" in out["detail"]
    # Run out twice: the failing pair was replayed once more before the step failed.
    assert [r[:2] for r in runs if r[0] != "prefold"] == [("off", False), ("off", True), ("on", True),
                                                          ("off", True), ("on", True)]


def test_replay_passes_a_candidate_whose_routing_read_ran_out_once_only(home, monkeypatch):
    # A runner's heartbeat rewritten between the prefold and the hook's read
    # makes the hook refold inline once; that is timing, not the candidate.
    candidate = _one_query(home)
    runs: list = []
    monkeypatch.setattr(pm, "_run", _routing_hook(runs, first_on_status="skipped:budget"))
    out = pm.gate_recall_replay(candidate)
    assert out["ok"], out
    assert "(routing=full)" in out["detail"]


@pytest.mark.parametrize("status", ["skipped:no-answer", "skipped:no-fork", "skipped:starting",
                                    "skipped:busy", "skipped:no-session", "skipped:somethingnew"])
def test_replay_fails_every_routing_skip_but_the_benign_ones(home, monkeypatch, status):
    # skipped:no-answer: the routing child died before it answered (an import
    # the candidate cannot load, a BaseException); every prompt would skip routing.
    candidate = _one_query(home)
    monkeypatch.setattr(pm, "_run", _routing_hook([], on_status=status))
    out = pm.gate_recall_replay(candidate)
    assert not out["ok"], out
    assert f"(routing={status})" in out["detail"]


@pytest.mark.parametrize("status", ["skipped:fold-busy", "skipped:unsaved"])
def test_replay_passes_a_prompt_that_waited_for_another_fold_or_could_not_save(home, monkeypatch, status):
    candidate = _one_query(home)
    monkeypatch.setattr(pm, "_run", _routing_hook([], on_status=status))
    out = pm.gate_recall_replay(candidate)
    assert out["ok"], out


def test_replay_fails_when_the_candidate_says_its_push_is_on_and_its_hook_logs_no_routing(home, monkeypatch):
    candidate = _one_query(home)
    monkeypatch.setattr(pm, "_run", _routing_hook([], on_status=None, push=True))
    out = pm.gate_recall_replay(candidate)
    assert not out["ok"], out
    assert "logged no routing status" in out["detail"]


def test_replay_passes_without_a_routing_field_when_the_candidate_says_its_push_is_off(home, monkeypatch):
    candidate = _one_query(home)
    monkeypatch.setattr(pm, "_run", _routing_hook([], on_status=None, push=False))
    out = pm.gate_recall_replay(candidate)
    assert out["ok"] and "routing did not run" in out["detail"]


def test_replay_fails_a_candidate_whose_routing_read_fails(home, monkeypatch):
    candidate = _one_query(home)
    monkeypatch.setattr(pm, "_run", _routing_hook([], on_status="skipped:error-TypeError"))
    out = pm.gate_recall_replay(candidate)
    assert not out["ok"] and "its routing read failed (routing=skipped:error-TypeError)" in out["detail"]


def test_replay_fails_a_candidate_whose_search_answers_differ_with_routing_on(home, monkeypatch):
    candidate = _one_query(home)
    runs: list = []
    monkeypatch.setattr(pm, "_run", _routing_hook(runs, on_hits=("episode:4",)))
    out = pm.gate_recall_replay(candidate)
    assert not out["ok"]
    assert "the search answer changed with routing on" in out["detail"]
    # The differing pair was replayed once more before the step failed.
    assert [r[:2] for r in runs if r[0] != "prefold"] == [("off", False), ("off", True), ("on", True),
                                                          ("off", True), ("on", True)]


@pytest.mark.parametrize(("reason", "ms", "hits", "expect"), [
    ("timeout>1.2s", 1200.0, (), "with routing on, timeout>1.2s"),
    ("error:RuntimeError", 40.0, (), "with routing on, error:RuntimeError"),
    ("ok", 1900.0, ("episode:3",), "with routing on, 1900 ms > 1200 ms"),
])
def test_replay_fails_a_candidate_whose_search_with_routing_on_times_out_fails_or_runs_long(
        home, monkeypatch, reason, ms, hits, expect):
    # The run with routing on is held to the same search test as the run
    # with it off: an empty answer both ways, or the same hits found late,
    # once let a routing that broke the search pass.
    candidate = _one_query(home)
    runs: list = []
    off_hits = hits or ()
    monkeypatch.setattr(pm, "_run", _routing_hook(runs, off_hits=off_hits, on_hits=hits, on_reason=reason, on_ms=ms))
    out = pm.gate_recall_replay(candidate)
    assert not out["ok"], out
    assert expect in out["detail"]
    # The failing pair was replayed once more before the step failed.
    assert [r[0] for r in runs if r[0] != "prefold"] == ["off", "off", "on", "off", "on"]


@pytest.mark.parametrize(("again_off", "expect"), [
    (("timeout>1.2s", 1200.0, ()), "prompt 1: timeout>1.2s"),
    (("ok", 300.0, ()), "prompt 1: empty answer where live had 1 hit(s)"),
])
def test_replay_holds_the_replayed_run_with_routing_off_to_the_same_tests(home, monkeypatch, again_off, expect):
    # Fix round 5, finding 2: the replayed pair's run with routing off
    # replaced the first one but was never tested, so a candidate whose
    # replayed search timed out (or came back empty where live had hits)
    # passed whenever its replayed run with routing on was fine and ran
    # other legs.
    candidate = _one_query(home)
    runs: list = []
    monkeypatch.setattr(pm, "_run", _routing_hook(runs, first_on_status="skipped:budget", again_off=again_off))
    out = pm.gate_recall_replay(candidate)
    assert not out["ok"], out
    assert expect in out["detail"]
    assert [r[0] for r in runs if r[0] != "prefold"] == ["off", "off", "on", "off", "on"]


def test_the_search_test_is_the_same_for_both_runs():
    off = {"exit": 0, "valid_json": True, "reason": "ok", "ms": 300.0, "hits": [], "routing": None}
    on = {"exit": 0, "valid_json": True, "reason": "timeout>1.2s", "ms": 1200.0, "hits": [],
          "routing": "none", "push": True}
    assert pm._routing_problem("prompt 1", off, on) == "prompt 1: with routing on, timeout>1.2s"
    off["hits"] = ["episode:1"]
    on.update(reason="ok", ms=1900.0, hits=["episode:1"], routing="full")
    assert pm._routing_problem("prompt 1", off, on) == "prompt 1: with routing on, 1900 ms > 1200 ms"
    on.update(reason=None)
    assert pm._routing_problem("prompt 1", off, on) == "prompt 1: with routing on, the hook wrote no summary line"
    on.update(reason="ok", ms=900.0)
    assert pm._routing_problem("prompt 1", off, on) is None


def _late_embedding_hook(rng, late: float):
    """A fake candidate whose routing is fine and whose embedding is late at
    random, with routing on or off alike: late, the search answers from the
    lexical leg alone (``degraded=embedding late``, fewer hits)."""
    def fake_run(argv, *, env=None, input=None, cwd=None, timeout=120):
        if argv[1:2] == ["-c"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"push": True}) + "\n", "")
        on = env.get("KHIPU_FEATURE_ROUTING_PUSH") == "1"
        if rng.random() < late:
            hits, legs, degraded = ["episode:3"], ["lexical"], "embedding late"
        else:
            hits, legs, degraded = ["episode:3", "episode:5"], ["lexical", "cosine"], None
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = (f"{stamp} [khipu-prompt-recall] session=s1 reason=ok ms=300.0 hits={hits} legs={legs} "
                f"degraded={degraded}" + (" routing=full" if on else ""))
        log = Path(env["KHIPU_CAPTURE_HOME"]) / "logs" / "prompt-recall.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(line + "\n")
        return subprocess.CompletedProcess(argv, 0, '{"hookSpecificOutput": {}}\n', "")
    return fake_run


def test_replay_passes_a_good_candidate_whose_embedding_is_late_at_random(home, monkeypatch):
    # Each run is a cold one-shot hook: a late embedding gives a lexical-only
    # answer whatever routing does. A pair whose degraded note or legs differ
    # is timing, not routing, and never fails the step.
    import random

    candidate = _one_query(home)
    monkeypatch.setattr(pm, "_run", _late_embedding_hook(random.Random(7), late=0.3))
    failed = [out["detail"] for out in (pm.gate_recall_replay(candidate) for _ in range(100)) if not out["ok"]]
    assert failed == []


def test_replay_passes_a_search_that_differed_once_and_agrees_again(home, monkeypatch):
    candidate = _one_query(home)
    monkeypatch.setattr(pm, "_run", _routing_hook([], first_on_hits=()))
    out = pm.gate_recall_replay(candidate)
    assert out["ok"], out


def test_replay_reads_a_copy_of_the_live_routing_record_folded_by_the_candidate(home, monkeypatch):
    from khipu import routing

    candidate = _one_query(home)
    live = routing.routing_dir()
    live.mkdir(parents=True)
    (live / "events.jsonl").write_text('{"type": "report"}\n')
    before = (live / "events.jsonl").read_bytes()
    runs: list = []
    monkeypatch.setattr(pm, "_run", _routing_hook(runs))
    out = pm.gate_recall_replay(candidate)
    assert out["ok"], out
    assert "with routing on (routing=full)" in out["detail"]
    prefold = [r for r in runs if r[0] == "prefold"]
    assert len(prefold) == 1 and prefold[0][1].startswith(str(candidate / "packages" / "cli"))
    assert "khipu-gate-recall-" in prefold[0][2]
    assert ("on", True, True) in runs and ("off", True, False) in runs  # copied only for the routing run
    assert (live / "events.jsonl").read_bytes() == before and sorted(p.name for p in live.iterdir()) == ["events.jsonl"]


def test_replay_says_so_when_routing_did_not_run(home, monkeypatch):
    candidate = _one_query(home)
    monkeypatch.setattr(pm, "_run", _fake_hook(lambda hook: ["episode:3"]))
    out = pm.gate_recall_replay(candidate)
    assert out["ok"] and "routing did not run" in out["detail"]


def test_the_gate_never_runs_python_in_the_callers_folder(tmp_path, monkeypatch):
    # ``python -c`` and ``-m`` put the working folder first on sys.path: run
    # from a checkout's packages/cli, the prefold and the replayed hook
    # imported that checkout's khipu instead of the candidate's.
    planted = tmp_path / "caller"
    (planted / "khipu").mkdir(parents=True)
    (planted / "khipu" / "__init__.py").write_text("MARK = 'the caller'\n")
    candidate = tmp_path / "candidate" / "packages" / "cli"
    (candidate / "khipu").mkdir(parents=True)
    (candidate / "khipu" / "__init__.py").write_text("MARK = 'the candidate'\n")
    monkeypatch.chdir(planted)
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(candidate)}
    script = "import khipu; print(khipu.MARK)"
    for argv in ([pm._hook_python(), "-c", script], [pm._hook_python(), "-m", "khipu"]):
        if argv[1] == "-m":
            (candidate / "khipu" / "__main__.py").write_text("import khipu; print(khipu.MARK)\n")
            (planted / "khipu" / "__main__.py").write_text("import khipu; print(khipu.MARK)\n")
        r = pm._run(argv, env=env, timeout=60)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "the candidate", argv
    # A caller that names a folder still gets it.
    r = pm._run([pm._hook_python(), "-c", "import os; print(os.getcwd())"], env=env, cwd=str(tmp_path), timeout=60)
    assert Path(r.stdout.strip()).resolve() == tmp_path.resolve()


def test_replay_with_nothing_to_replay_is_a_failure_not_a_pass(home):
    candidate = _hooks(home)
    out = pm.gate_recall_replay(candidate)
    assert not out["ok"] and out["detail"] == "no queries in the last day; use Khipu once and re-run"
    _query_log(home).write_text(_query(datetime.now(timezone.utc) - timedelta(days=2), "stale") + "\n")
    assert pm.gate_recall_replay(candidate)["detail"] == "no queries in the last day; use Khipu once and re-run"


# ---- capture and version ---------------------------------------------------


def test_the_gate_fixture_is_queued_as_a_gate_job(home, monkeypatch, tmp_path):
    from khipu import session_capture

    assert len(FIXTURE.read_text()) < 6000
    capture_home = tmp_path / "capture"
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(capture_home))
    monkeypatch.setenv("KHIPU_GATE", "1")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(FIXTURE.read_text())
    payload = {"session_id": "khipu-gate-test", "transcript_path": str(transcript),
               "hook_event_name": "SessionEnd", "cwd": str(tmp_path)}
    out = session_capture.hook_main(json.dumps(payload), "claude_code")
    assert out["due"], out
    jobs = [json.loads(p.read_text()) for p in (capture_home / "queue").glob("*.json")]
    assert len(jobs) == 1 and jobs[0]["gate"] is True
    assert "percentage column" in jobs[0]["transcript"]


class _FakeHub:
    """Stands in for the gate's subprocesses: the stop hook, the episode
    lookup and ``khipu episode forget``, with a hub that holds ``live`` ids."""

    def __init__(self, home: Path, live=(41,), fail_lookups=0, fail_forget=False):
        self.home, self.live, self.fail_lookups, self.fail_forget = home, list(live), fail_lookups, fail_forget
        self.calls: list[tuple[list[str], dict]] = []
        self.hook_env: dict = {}

    def __call__(self, argv, *, env=None, input=None, cwd=None, timeout=120):
        self.calls.append((argv, dict(env or {})))
        if argv[0].endswith("khipu-stop-hook"):
            self.hook_env = dict(env)
            assert json.loads(input)["hook_event_name"] == "SessionEnd"
            # Durable before the hook runs: the run's marker and the jsonl line.
            iso = Path(env["KHIPU_ISOLATED_DIR"])
            assert json.loads((iso / "gate.json").read_text())["cleanup"] == "cleanup_pending"
            assert [r for r in pm.pending_cleanups() if r["dir"] == str(iso)]
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[1] == "-c":
            if self.fail_lookups:
                self.fail_lookups -= 1
                return subprocess.CompletedProcess(argv, 1, "", "OperationalError: hub unreachable")
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.live) + "\n", "")
        if argv[-3:-1] == ["episode", "forget"]:
            if self.fail_forget:
                return subprocess.CompletedProcess(argv, 1, "", "boom")
            self.live.remove(int(argv[-1]))
            return subprocess.CompletedProcess(argv, 0, '{"ok": true}', "")
        return subprocess.CompletedProcess(argv, 0, '{"ok": true}', "")


def test_capture_step_finds_and_forgets_the_episode_inside_its_isolated_dir(home, monkeypatch):
    candidate = _gate_ready(home / "candidate")
    hub = _FakeHub(home)
    monkeypatch.setattr(pm, "_run", hub)
    out = pm.gate_capture(candidate)
    assert out["ok"], out
    env = hub.hook_env
    assert env["KHIPU_HOOK_INLINE"] == "1" and env["KHIPU_GATE"] == "1"
    iso = Path(env["KHIPU_ISOLATED_DIR"])
    assert iso.parent == pm.gate_runs_dir()
    assert env["KHIPU_CAPTURE_HOME"] == str(iso / "capture")
    # Credentials are read from the caller's data dir, as a live hook reads them.
    assert env["KHIPU_DATA_DIR"] == os.environ["KHIPU_DATA_DIR"]
    assert not [k for k in env if "KEY" in k or "TOKEN" in k], env
    forget = [(argv, e) for argv, e in hub.calls if argv[-3:] == ["episode", "forget", "41"]]
    assert len(forget) == 1
    assert forget[0][1]["PYTHONPATH"].startswith(str(candidate / "packages" / "cli"))
    # The forget's replica erasure and outbox check run in the same isolated dir.
    assert forget[0][1]["KHIPU_ISOLATED_DIR"] == str(iso)
    assert hub.live == [] and not iso.exists() and pm.pending_cleanups() == []
    states = [r["cleanup"] for r in pm.records() if r.get("action") == "gate-cleanup"]
    assert states == ["cleanup_pending", "cleanup_done"]


def test_capture_step_fails_when_no_episode_lands(home, monkeypatch):
    candidate = _gate_ready(home / "candidate")
    monkeypatch.setattr(pm, "_run", _FakeHub(home, live=()))
    out = pm.gate_capture(candidate)
    assert not out["ok"] and "no episode" in out["detail"]
    assert pm.pending_cleanups() == []  # the hub answered: nothing to forget


def test_capture_step_refuses_a_candidate_without_isolation(home, monkeypatch):
    candidate = _fake_checkout(home / "candidate")
    (candidate / "packages" / "cli" / "bin" / "khipu-stop-hook").write_text("#!/bin/sh\n")
    monkeypatch.setattr(pm, "_run", lambda *a, **k: pytest.fail("nothing may run"))
    out = pm.gate_capture(candidate)
    assert not out["ok"] and "KHIPU_ISOLATED_DIR" in out["detail"]


def test_a_failed_lookup_leaves_the_cleanup_pending_and_status_finishes_it(home, monkeypatch):
    candidate = _gate_ready(home / "candidate")
    hub = _FakeHub(home, fail_lookups=1)
    monkeypatch.setattr(pm, "_run", hub)
    out = pm.gate_capture(candidate)
    assert not out["ok"] and "cleanup pending" in out["detail"]
    pending = pm.pending_cleanups()
    assert len(pending) == 1 and pending[0]["session_id"].startswith("claude_code:khipu-gate-")
    run_dir = Path(pending[0]["dir"])
    assert (run_dir / "gate.json").is_file()
    assert hub.live == [41]
    st = pm.status()
    assert st["cleanups"][0]["ok"] and st["cleanups"][0]["forgotten"] == [41]
    assert hub.live == [] and st["cleanup_pending"] == [] and not run_dir.exists()


def test_a_failed_forget_is_retried_by_the_next_promote(home, monkeypatch, tmp_path):
    candidate = _gate_ready(home / "candidate")
    hub = _FakeHub(home, fail_forget=True)
    monkeypatch.setattr(pm, "_run", hub)
    out = pm.gate_capture(candidate)
    assert not out["ok"] and "forget not confirmed" in out["detail"] and "still live" in out["detail"]
    assert len(pm.pending_cleanups()) == 1
    hub.fail_forget = False
    monkeypatch.setattr(pm, "_repo", lambda: tmp_path)
    monkeypatch.setattr(pm, "_fetch_and_resolve", lambda repo, ref: {"ok": False, "error": "offline"})
    pm.promote()
    assert hub.live == [] and pm.pending_cleanups() == []
    retried = [r for r in pm.records() if r.get("action") == "promote"][-1]
    assert retried["cleanups"][0]["ok"]


def test_a_cleanup_known_only_from_its_marker_is_retried(home, monkeypatch):
    run_dir = pm.gate_runs_dir() / "khipu-gate-orphan"
    run_dir.mkdir(parents=True)
    (run_dir / "gate.json").write_text(json.dumps({"session_id": "claude_code:khipu-gate-orphan",
                                                   "cleanup": "cleanup_pending"}))
    hub = _FakeHub(home, live=(7,))
    monkeypatch.setattr(pm, "_run", hub)
    assert [r["session_id"] for r in pm.pending_cleanups()] == ["claude_code:khipu-gate-orphan"]
    assert pm.retry_cleanups()[0]["forgotten"] == [7]
    assert pm.pending_cleanups() == [] and not run_dir.exists()


def test_version_step(home, monkeypatch):
    candidate = _fake_checkout(home / "candidate")
    live = _fake_checkout(home / "live-code")
    monkeypatch.setattr(pm, "_live_code_root", lambda: live)
    facts = {}
    monkeypatch.setattr(pm, "_code_facts", lambda root: facts[Path(root)])
    facts.update({candidate: {"version": "0.4.14", "schema": 4, "disk_schema": 4},
                  live: {"version": "0.4.13", "schema": 4, "disk_schema": 4}})
    assert pm.gate_version(candidate)["ok"]
    facts[candidate] = {"version": "0.4.12", "schema": 4, "disk_schema": 4}
    assert "older than live" in pm.gate_version(candidate)["detail"]
    facts[candidate] = {"version": "0.4.13", "schema": 3, "disk_schema": 4}
    assert not pm.gate_version(candidate)["ok"]
    facts[candidate] = {"version": "0.4.13", "schema": 5, "disk_schema": 4}
    assert pm.gate_version(candidate)["ok"]


def test_doctor_step_runs_quick_doctor_under_the_launchd_environment(home, monkeypatch):
    candidate = _fake_checkout(home / "candidate")
    seen = {}

    def fake_run(argv, *, env=None, input=None, cwd=None, timeout=120):
        seen.update(argv=argv, env=env)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True}), "")

    monkeypatch.setattr(pm, "_run", fake_run)
    out = pm.gate_doctor(candidate)
    assert out["ok"], out
    assert seen["argv"][-3:] == ["khipu", "doctor", "--quick"]
    assert seen["env"]["PATH"] == pm.LAUNCHD_PATH
    assert seen["env"]["KHIPU_ROOT"] == str(candidate)
    assert seen["env"]["PYTHONPATH"].split(":")[0] == str(candidate / "packages" / "cli")


def test_doctor_quick_never_touches_the_hub(home, monkeypatch, capsys):
    from khipu import cli, drift

    monkeypatch.setattr(drift, "status_payload", lambda *_a: pytest.fail("quick doctor must not reach the hub"))
    rc = cli.main(["doctor", "--quick"])
    out = json.loads(capsys.readouterr().out)
    assert out["quick"] is True and rc in (0, 2)
    assert out["imports"]["ok"], out["imports"]
    assert {"config", "secrets", "jobs", "synth_provider"} <= set(out)


# ---- --gate-only -----------------------------------------------------------


def test_gate_only_runs_one_step(home, monkeypatch, tmp_path, capsys):
    from khipu import cli

    monkeypatch.setattr(pm, "_repo", lambda: _fake_checkout(tmp_path / "checkout"))
    ran = []
    monkeypatch.setattr(pm, "GATE_STEPS", {
        name: (lambda c, name=name: (ran.append((name, Path(c))), {"ok": True, "ms": 2, "detail": name})[1])
        for name in pm.GATE_ORDER
    })
    rc = cli.main(["promote", "--gate-only", "version", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"]
    assert ran == [("version", tmp_path / "checkout")]
    assert pm.live_root() is None  # never flips
    assert pm.records()[-1]["action"] == "gate-only"
    assert not pm.gate_only("nope")["ok"]


# ---- installs with no live link are unchanged ------------------------------


def test_jobs_install_without_a_live_link_renders_todays_paths_byte_for_byte(home, monkeypatch, tmp_path):
    checkout = _fake_checkout(tmp_path / "checkout")
    monkeypatch.setattr(launchd_gen, "_repo_root", lambda: checkout)
    monkeypatch.setattr(launchd_gen, "_bundled_python", lambda *a: Path("/example/python3.11"))
    monkeypatch.setattr(launchd_gen, "_launchctl_load", lambda label, path: {"ok": True})
    from khipu.components_matrix import application_support_dir
    from khipu.jobs import _JOB_SPECS, _log_paths
    from khipu.paths import pycache_dir

    assert pm.live_root() is None
    out = launchd_gen.install_job("recall_daemon")
    assert out["ok"], out
    text = (launchd_gen.TEMPLATE_DIR / "com.khipu.recall-daemon.plist").read_text(encoding="utf-8")
    for key, value in {
        "KHIPU_ROOT": str(checkout),
        "KHIPU_PYTHON": "/example/python3.11",
        "PYTHONPATH": str(checkout / "packages" / "cli"),
        "WORKING_DIRECTORY": str(application_support_dir()),
        "PYTHONPYCACHEPREFIX": str(pycache_dir()),
    }.items():
        text = text.replace("{{" + key + "}}", escape(value))
    out_log, err_log = _log_paths(_JOB_SPECS["recall_daemon"]["log_stem"])
    text = (text.replace("{{EXTRA_ENV}}", launchd_gen.render_extra_env())
            .replace("{{STDOUT_LOG}}", escape(str(out_log)))
            .replace("{{STDERR_LOG}}", escape(str(err_log))))
    assert Path(out["path"]).read_bytes() == text.encode("utf-8")

    # With a live link the same install names the link, never a slot.
    slot = _fake_checkout(pm.slots()[0])
    assert pm.flip(slot)["ok"]
    data = plistlib.loads(launchd_gen.render_plist("recall_daemon"))
    assert data["EnvironmentVariables"]["KHIPU_ROOT"] == str(pm.live_link())
    assert data["EnvironmentVariables"]["PYTHONPATH"] == str(pm.live_link() / "packages" / "cli")


def test_integrations_install_without_a_live_link_points_where_it_did(home, monkeypatch, tmp_path):
    checkout = _fake_checkout(tmp_path / "checkout")
    (checkout / "packages" / "cli" / "bin" / "khipu-stop-hook").write_text("#!/bin/sh\n")
    monkeypatch.setattr(integrations, "_root", lambda: checkout)
    with integrations._installing(dry=False):
        link = Path(integrations._shim("khipu-stop-hook"))
    assert link == home / ".config" / "khipu" / "bin" / "khipu-stop-hook"
    assert os.readlink(link) == str(checkout / "packages" / "cli" / "bin" / "khipu-stop-hook")

    slot = _fake_checkout(pm.slots()[1])
    (slot / "packages" / "cli" / "bin" / "khipu-stop-hook").write_text("#!/bin/sh\n")
    assert pm.flip(slot)["ok"]
    with integrations._installing(dry=False):
        integrations._shim("khipu-stop-hook")
    assert os.readlink(link) == str(pm.live_link() / "packages" / "cli" / "bin" / "khipu-stop-hook")


# ---- fix round: isolation, masking, credentials, parity --------------------


def test_replay_masks_even_when_redaction_is_switched_off(home, monkeypatch):
    monkeypatch.setenv("KHIPU_REDACT_SECRETS", "0")
    now = datetime.now(timezone.utc)
    _query_log(home).write_text(
        _query(now - timedelta(minutes=5), "token api_key=sk-live-abcdefghijklmnopqrstuvwxyz0123456789") + "\n")
    samples = pm.sample_queries(_query_log(home))
    assert len(samples) == 1 and "sk-live-abcdefghij" not in samples[0]["prompt"]


def test_the_gate_never_reads_a_key_or_token_variable(home, monkeypatch, tmp_path):
    """Every step and the restart after a flip, with a trap environment."""
    import khipu.config  # noqa: F401 — imported before the trap is installed
    import khipu.hub_snapshot  # noqa: F401
    import khipu.jobs  # noqa: F401
    import khipu.launchd_gen  # noqa: F401
    import khipu.query_log  # noqa: F401
    import khipu.redact  # noqa: F401

    now = datetime.now(timezone.utc)
    _query_log(home).write_text(_query(now - timedelta(hours=1), "where is the gate") + "\n")
    candidate = _gate_ready(home / "candidate")
    (candidate / "packages" / "cli" / "bin" / "khipu-prompt-recall").write_text("#!/bin/sh\n")
    _hooks(home)
    envs: list[dict] = []
    hub = _FakeHub(home)

    def fake_run(argv, *, env=None, input=None, cwd=None, timeout=120):
        envs.append(dict(env or {}))
        if argv[0].endswith("khipu-prompt-recall"):
            return _fake_hook(lambda hook: ["episode:1"])(argv, env=env, input=input)
        if argv[1:3] == ["-m", "khipu"] and argv[3:5] == ["doctor", "--quick"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True}), "")
        if argv[1:2] == ["-c"] and "__version__" in argv[2]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"version": "9.9.9", "schema": 1,
                                                                     "disk_schema": 1}), "")
        return hub(argv, env=env, input=input, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(pm, "_run", fake_run)
    for name in ("GEMINI_API_KEY", "VOYAGE_API_KEY", "KHIPU_GATEWAY_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.setenv(name, "not-real")
    slot = _fake_checkout(pm.slots()[0])
    assert pm.flip(slot)["ok"]
    reads: list[str] = []
    monkeypatch.setattr(os, "environ", _SpyEnviron(dict(os.environ), reads))
    results = [pm.gate_doctor(candidate), pm.gate_recall_replay(candidate), pm.gate_capture(candidate),
               pm.gate_version(candidate)]
    pm._after_flip()
    monkeypatch.undo()
    assert all(r["ok"] for r in results), results
    assert "*every variable*" not in reads
    assert not [k for k in reads if "KEY" in k or "TOKEN" in k], sorted(set(reads))
    for env in envs:
        assert not [k for k in env if "KEY" in k or "TOKEN" in k], env


def test_gate_mode_stop_hook_background_drains_and_does_nothing_else(home, monkeypatch, tmp_path):
    from khipu import hook_background, mirror, notes, outbox
    from khipu import embed as embed_mod

    monkeypatch.setenv("KHIPU_ISOLATED_DIR", str(tmp_path / "iso"))
    monkeypatch.setenv("KHIPU_GATE", "1")
    drained = []
    monkeypatch.setattr(hook_background, "_drain", lambda sid, harness: drained.append(sid))
    monkeypatch.setattr(mirror, "sync_recent_episodes", lambda *a, **k: pytest.fail("mirror ran"))
    monkeypatch.setattr(outbox, "jobs", lambda *a, **k: pytest.fail("outbox replay ran"))
    monkeypatch.setattr(outbox, "drain", lambda *a, **k: pytest.fail("outbox replay ran"))
    monkeypatch.setattr(notes, "reconcile", lambda *a, **k: pytest.fail("notes reconcile ran"))
    monkeypatch.setattr(embed_mod, "embed_recent_missing", lambda *a, **k: pytest.fail("embed catch-up ran"))
    hook_background.run(str(home / "wiki"), "claude_code", "khipu-gate-x")
    assert drained == ["khipu-gate-x"]


_WRITE_EVENTS = {"os.mkdir", "os.rename", "os.replace", "os.remove", "os.rmdir", "os.symlink",
                 "os.link", "os.truncate", "shutil.rmtree", "shutil.copyfile", "shutil.move"}
_SPY: dict = {"on": False, "writes": [], "at": {}}
# Where each os.* call made relative to a directory descriptor (dir_fd) points:
# the descriptor's own path, read with F_GETPATH at the time of the call.
_DIR_FD_ARG = {"os.mkdir": 2, "os.rmdir": 1, "os.remove": 1, "os.rename": (2, 3), "os.replace": (2, 3),
               "os.utime": 3, "os.chmod": 2}


def _fd_path(fd) -> str | None:
    import fcntl

    if not isinstance(fd, int) or fd < 0 or not hasattr(fcntl, "F_GETPATH"):
        return None
    try:
        return os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024).rstrip(b"\0"))
    except OSError:
        return None


def _at(event, args, i, path):
    """``path`` made absolute through the call's dir_fd, when it has one;
    a relative name is remembered so a plain ``open`` of it resolves too."""
    slot = _DIR_FD_ARG.get(event)
    slot = slot[i] if isinstance(slot, tuple) else slot
    base = _fd_path(args[slot]) if slot is not None and len(args) > slot else None
    if base and not os.path.isabs(path):
        _SPY["at"][path] = base
        return os.path.join(base, path)
    return path


def _audit(event, args, _spy=_SPY):
    # An audit hook can't be removed and runs until the interpreter exits,
    # after module globals are set to None: it holds its own reference.
    if not _spy["on"]:
        return
    if event == "open":
        path, mode, flags = (list(args) + [None, None, None])[:3]
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            mode is None and isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT))
        if writing and isinstance(path, (str, bytes, os.PathLike)):
            _SPY["writes"].append((event, os.fsdecode(path)))
    elif event == "sqlite3.connect":
        target = str(args[0]) if args else ""
        if "mode=ro" not in target and target != ":memory:":
            _SPY["writes"].append((event, target.removeprefix("file:").split("?")[0]))
    elif event in _WRITE_EVENTS and args:
        for i, path in enumerate(args[:2] if event in ("os.rename", "os.replace", "os.symlink", "os.link",
                                                       "shutil.copyfile", "shutil.move") else args[:1]):
            if isinstance(path, (str, bytes, os.PathLike)):
                _SPY["writes"].append((event, _at(event, args, i, os.fsdecode(path))))


sys_audit_installed = []


@pytest.fixture
def write_spy():
    import sys

    if not sys_audit_installed:
        sys.addaudithook(_audit)
        sys_audit_installed.append(True)
    _SPY["writes"] = []
    _SPY["at"] = {}
    _SPY["on"] = True
    try:
        yield _SPY["writes"]
    finally:
        _SPY["on"] = False


def _outside(writes, *allowed: Path) -> list:
    roots = [os.path.realpath(str(a)) for a in allowed]
    bad = []
    for event, path in writes:
        # ``open`` reports no dir_fd: a relative name opened through one is
        # placed where the os.* call on the same name (its rename) placed it.
        if not os.path.isabs(path) and path in _SPY["at"]:
            path = os.path.join(_SPY["at"][path], path)
        real = os.path.realpath(path)
        if not any(real == r or real.startswith(r + os.sep) for r in roots):
            bad.append((event, path))
    return bad


def test_an_isolated_drain_writes_nothing_outside_its_dir(home, monkeypatch, tmp_path, write_spy):
    """The capture half of the gate in process, unmocked but for the
    summariser: hook_main queues the fixture, the drain captures it (the hub
    is unreachable here, so the capture goes to the outbox), the decision sync
    and the file-wiki leg run. Every write must land in the isolated dir."""
    from khipu import session_capture

    iso = tmp_path / "iso"
    pm._isolate(iso)
    monkeypatch.setenv("KHIPU_ISOLATED_DIR", str(iso))
    monkeypatch.setenv("KHIPU_GATE", "1")
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_MEMORY_ROOT", str(home / "wiki"))  # read as unset when isolated
    for name in ("KHIPU_DATABASE_URL", "ALZY_DATABASE_URL", "KHIPU_CAPTURE_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("khipu.extract.extract_memory",
                        lambda *a, **k: {"summary": "gate capture", "decisions": []})
    transcript = iso / "transcript.jsonl"
    transcript.write_text(FIXTURE.read_text())
    write_spy.clear()
    queued = session_capture.hook_main(json.dumps({
        "session_id": "khipu-gate-spy", "transcript_path": str(transcript),
        "hook_event_name": "SessionEnd", "cwd": str(iso)}), "claude_code")
    assert queued["due"], queued
    out = session_capture.drain()
    assert out["captured"] == 1, out
    assert list((iso / "outbox").glob("*.json")), "the unreachable hub's capture waits in the isolated outbox"
    assert not (home / "wiki").exists()
    assert write_spy, "the spy saw no writes at all"
    assert _outside(write_spy, iso) == []


def test_the_real_stop_hook_in_gate_mode_writes_only_its_isolated_dir(home, monkeypatch):
    """gate_capture with the real stop hook of this checkout (a shell script
    and a Python process). The hub is not reachable from a test, so the
    lookup answers "nothing landed". Outside the run dir only the gate's own
    record and the bytecode cache may change."""
    real_run = pm._run

    def run(argv, *, env=None, input=None, cwd=None, timeout=120):
        if argv[0].endswith("khipu-stop-hook"):
            env = {**env, "KHIPU_KEYCHAIN": "0"}  # this test's own guard: never the real Keychain
            return real_run(argv, env=env, input=input, cwd=cwd, timeout=timeout)
        if argv[1] == "-c":
            return subprocess.CompletedProcess(argv, 0, "[]\n", "")
        return pytest.fail(f"unexpected subprocess {argv}")

    def tree(root: Path) -> dict:
        out = {}
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames + filenames:
                p = Path(dirpath) / name
                try:
                    st = p.lstat()
                except OSError:
                    continue
                out[str(p)] = (st.st_mtime_ns, st.st_size)
        return out

    (home / "Library" / "Logs" / "khipu").mkdir(parents=True)
    (home / ".grok" / "khipu").mkdir(parents=True)
    before = tree(home)
    monkeypatch.setattr(pm, "_run", run)
    seen = {}
    real_isolate = pm._isolate

    def isolate(directory, **kw):
        seen["dir"] = Path(directory)
        return real_isolate(directory, **kw)

    monkeypatch.setattr(pm, "_isolate", isolate)
    out = pm.gate_capture(pm._self_root())
    assert not out["ok"] and "no episode" in out["detail"], out
    run_dir = seen["dir"]
    assert "capture" in out["detail"]
    after = tree(home)
    allowed = (str(pm.state_file()), str(pm.state_file().parent), str(run_dir),
               str(pm.gate_runs_dir()), str(home / "Library" / "Caches" / "Khipu"))
    changed = sorted(p for p in set(before) | set(after)
                     if before.get(p) != after.get(p)
                     and not any(p == a or p.startswith(a + os.sep) for a in allowed))
    # Parents of the allowed paths are created on the way.
    changed = [p for p in changed if not any(a.startswith(p + os.sep) for a in allowed)]
    assert changed == [], changed
    assert not list((home / "Library" / "Logs" / "khipu").iterdir())
    assert not list((home / ".grok" / "khipu").iterdir())


def test_a_slot_imports_what_only_its_vendored_libs_hold(home, repo):
    import sys

    r, first, _second = repo
    (r / ".python_libs" / "khipu_gate_vendored_probe.py").write_text("VALUE = 7\n")
    a, _b = pm.slots()
    assert pm._prepare_slot(a, first, r)["ok"]
    assert pm.flip(a)["ok"]
    for ctx in (launchd_gen.render_context(), launchd_gen.render_context(root=a)):
        assert any(part.endswith(".python_libs") for part in ctx["PYTHONPATH"].split(":")), ctx
        env = {**pm._launchd_env(ctx), "PYTHONPATH": ctx["PYTHONPATH"]}
        done = subprocess.run([sys.executable, "-c", "import khipu_gate_vendored_probe as m; print(m.VALUE)"],
                              env=env, cwd=str(home), capture_output=True, text=True)
        assert done.returncode == 0 and done.stdout.strip() == "7", done.stderr


def _main_module(rel: str, name: str):
    """The given module as it is on the main commit this branch is based on."""
    import importlib.util

    root = pm._self_root()
    base = None
    for ref in ("origin/main", "main"):
        found = subprocess.run(["git", "merge-base", "HEAD", ref], cwd=str(root), capture_output=True, text=True)
        if found.returncode == 0 and found.stdout.strip():
            base = found.stdout.strip()
            break
    if base is None:
        pytest.skip("no main ref to compare against")
    src = subprocess.run(["git", "show", f"{base}:{rel}"], cwd=str(root), capture_output=True, text=True)
    if src.returncode != 0:
        pytest.skip(f"{rel} not on {base}")
    spec = importlib.util.spec_from_loader(name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = str(root / rel)
    exec(compile(src.stdout, f"{base[:12]}:{rel}", "exec"), mod.__dict__)  # noqa: S102 — our own repo
    return mod


def test_both_installers_match_main_byte_for_byte_without_a_live_link(home, monkeypatch, tmp_path):
    checkout = _fake_checkout(tmp_path / "checkout")
    (checkout / ".python_libs").mkdir()  # a dev checkout's vendored libs stay off the plist, as on main
    main_ld = _main_module("packages/cli/khipu/launchd_gen.py", "_main_launchd_gen")
    main_in = _main_module("packages/cli/khipu/integrations.py", "_main_integrations")
    py = Path("/example/python3.11")
    for mod in (launchd_gen, main_ld):
        monkeypatch.setattr(mod, "_repo_root", lambda: checkout)
        monkeypatch.setattr(mod, "_bundled_python", lambda *a: py)
    assert pm.live_root() is None
    for job in launchd_gen._JOB_TEMPLATE:
        if job not in main_ld._JOB_TEMPLATE:
            continue  # a job this branch adds has nothing on main to match yet
        assert launchd_gen.render_plist(job) == main_ld.render_plist(job), job
    monkeypatch.setattr(main_in, "HOME", home)
    for mod in (integrations, main_in):
        monkeypatch.setattr(mod, "_root", lambda: checkout)
    for name in ("khipu-mcp", "khipu-stop-hook", "khipu-recall-hook", "khipu-prompt-recall", "khipu-aegis-capture"):
        assert integrations._bin_script(name) == main_in._bin_script(name), name


def _parity_patches():
    from unittest import mock

    from tests.test_cli_recall_quality import _green_doctor_patches

    return [*_green_doctor_patches(),
            mock.patch("khipu.keychain.secrets_status",
                       return_value={"dsn_file": {"ok": True}, "gemini_in_keychain": True, "dsn": True}),
            mock.patch("khipu.probe.status", return_value={"ok": True, "reason": None}),
            mock.patch("khipu.drift.recall_quality", return_value={}),
            mock.patch("khipu.recall_daemon.daemon_health", return_value={"ok": True}),
            mock.patch("khipu.recall_prompt.prompt_recall_outcomes", return_value={"ok": True}),
            mock.patch("khipu.integrations.launcher_health", return_value={"ok": True}),
            mock.patch("khipu.bundle_seal.check", return_value={"ok": True, "na": True})]


_LOCAL_FAILURES = {
    "outbox_ok": ("khipu.outbox.status", {"pending": 2}),
    "capture_liveness_ok": ("khipu.session_capture.liveness_all", {"ok": False, "red": ["codex"], "harnesses": {}}),
    "git_sync_ok": ("khipu.git_sync_health.status", {"ok": False}),
    "dsn_file_ok": ("khipu.keychain.secrets_status",
                    {"dsn_file": {"ok": False}, "gemini_in_keychain": True, "dsn": True}),
    "synth_provider_ok": ("khipu.keychain.secrets_status", {"dsn_file": {"ok": True}, "dsn": True}),
    "jobs_ok": ("khipu.jobs.job_status", {"queue_drain": {"last_exit": 1, "expected": True}}),
    "index_freshness_ok": ("khipu.jobs.index_freshness", {"ok": False}),
    "synth_budget_ok": ("khipu.embed.budget_status", {"exhausted": True}),
    "graph_backup_ok": ("khipu.graph_backup.local_health", {"ok": False}),
    "bundle_seal_ok": ("khipu.bundle_seal.check", {"ok": False}),
    "prompt_recall_snapshot_ok": ("khipu.hub_snapshot.prompt_recall_snapshot_status", {"ok": False}),
    "prompt_recall_outcomes_ok": ("khipu.recall_prompt.prompt_recall_outcomes", {"ok": False}),
    "recall_daemon_ok": ("khipu.recall_daemon.daemon_health", {"ok": False}),
    "nightly_steps_ok": ("khipu.jobs.nightly_step_health", {"notes_reconcile_ok": {"ok": False}}),
    "degraded_rate_ok": ("khipu.query_log.degraded_rate", {"ok": False}),
    "launchers_ok": ("khipu.integrations.launcher_health", {"ok": False}),
    "ledger_outbox_ok": ("khipu.ledger.outbox_status", {"pending": 1, "refused": 0}),
    "disk_space_ok": ("khipu.storage.disk_space", {"ok": False, "level": "red", "volumes": []}),
}


def _doctor(quick: bool, extra=()) -> dict:
    from contextlib import ExitStack, redirect_stdout

    from khipu import cli

    with ExitStack() as stack:
        for patch in [*_parity_patches(), *extra]:
            stack.enter_context(patch)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_doctor(cli.build_parser().parse_args(["doctor", "--quick"] if quick else ["doctor"]))
    return json.loads(buf.getvalue())


def test_quick_doctor_gates_on_every_local_check_full_doctor_gates_on(home):
    from unittest import mock

    from khipu import cli

    assert set(_LOCAL_FAILURES) == set(cli.LOCAL_GATES)
    quick, full = _doctor(True), _doctor(False)
    assert quick["ok"] is True and full["ok"] is True, (quick, full)
    assert {g: quick[g] for g in cli.LOCAL_GATES} == {g: full[g] for g in cli.LOCAL_GATES}
    for gate, (target, value) in _LOCAL_FAILURES.items():
        extra = [mock.patch(target, return_value=value)]
        quick, full = _doctor(True, extra), _doctor(False, extra)
        assert quick[gate] is False and full[gate] is False, gate
        assert quick["ok"] is False and full["ok"] is False, gate


@pytest.mark.parametrize("free_gb, ok", [(2, False), (8, True)])
def test_a_red_disk_fails_both_doctors_and_amber_is_only_a_warning(home, monkeypatch, free_gb, ok):
    import shutil as _sh
    from unittest import mock

    from khipu import storage

    assert "disk_space" in pm.gate_ignorable()
    monkeypatch.setattr(storage.shutil, "disk_usage",
                        lambda _p: _sh._ntuple_diskusage(100 * 10**9, (100 - free_gb) * 10**9, free_gb * 10**9))
    real = [mock.patch("khipu.storage.disk_space", storage.disk_space)]
    quick, full = _doctor(True, real), _doctor(False, real)
    assert quick["disk_space_ok"] is ok and full["disk_space_ok"] is ok
    assert quick["ok"] is ok and full["ok"] is ok, (quick, full)
    assert full["disk_space"]["level"] == ("amber" if ok else "red")
    # What Khipu holds rides along in both, never as a gate.
    assert "line" in quick["storage"] and "line" in full["storage"] and "storage_ok" not in full
    warned = [w for w in full["warnings"] + quick["warnings"] if "is free on the disk" in w]
    assert len(warned) == (2 if ok else 0)

def test_an_isolated_run_keeps_state_inside_and_reads_credentials_from_the_caller(home, monkeypatch, tmp_path):
    from khipu import config, paths, session_capture

    caller = Path(os.environ["KHIPU_DATA_DIR"])
    iso = tmp_path / "iso"
    monkeypatch.setenv("KHIPU_ISOLATED_DIR", str(iso))
    monkeypatch.setenv("KHIPU_MEMORY_ROOT", str(home / "wiki"))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(home / "elsewhere"))
    assert paths.data_dir() == iso and session_capture.khipu_home() == iso / "capture"
    assert paths.credential_dir() == caller
    assert paths.dsn_file() == caller / "dsn" and paths.root_cert_file() == caller / "root.crt"
    assert config.path_setting("memory_root") is None
    monkeypatch.delenv("KHIPU_ISOLATED_DIR")
    assert config.path_setting("memory_root") == home / "wiki"


# ---- fix round 2: the replica, live-only writers, overrides ----------------


def _temp_replica(path: Path) -> None:
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.executescript(
        "CREATE TABLE episodes (id INTEGER PRIMARY KEY, summary TEXT, deleted_at TEXT);"
        "CREATE TABLE memory_embeddings (kind TEXT, ref TEXT, vector BLOB);"
        "CREATE TABLE edges (src TEXT, dst TEXT, type TEXT);"
        "INSERT INTO episodes VALUES (1, 'the promotion gate', NULL);"
        "INSERT INTO edges VALUES ('episode:1', 'topic:gate', 'mentions');")
    con.commit()
    con.close()


def test_an_isolated_replay_never_changes_the_live_replica(home, monkeypatch, tmp_path):
    import sqlite3

    from khipu import hub_snapshot

    live = hub_snapshot.snapshot_path()
    _temp_replica(live)  # no edge indexes: what a replica dumped before them looks like
    before = (live.read_bytes(), live.stat().st_mtime_ns)
    # The real candidate hook, replayed the way the gate replays it.
    real_run = pm._run
    monkeypatch.setattr(pm, "_run", lambda argv, *, env=None, **kw: real_run(
        argv, env={**env, "KHIPU_KEYCHAIN": "0"}, **kw))
    res = pm._recall_once(pm._launcher(pm._self_root(), "khipu-prompt-recall"),
                          {"prompt": "the promotion gate"}, "khipu-gate-replica")
    assert res["exit"] == 0, res
    # The graph leg's maintenance and the reader, in process, through the link.
    iso = tmp_path / "iso"
    pm._isolate(iso, replica=True)
    assert (iso / live.name).is_symlink()
    monkeypatch.setenv("KHIPU_ISOLATED_DIR", str(iso))
    hub_snapshot._EDGE_INDEXES_TRIED.clear()
    hub_snapshot.ensure_edge_indexes()
    con = hub_snapshot.open_snapshot()
    try:
        assert con.execute("SELECT summary FROM episodes").fetchall() == [("the promotion gate",)]
        with pytest.raises(sqlite3.OperationalError):
            con.execute("CREATE INDEX idx_x ON edges (src)")
    finally:
        con.close()
    assert (live.read_bytes(), live.stat().st_mtime_ns) == before
    names = {r[0] for r in sqlite3.connect(f"file:{live}?mode=ro", uri=True).execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert not {"idx_edges_src_type", "idx_edges_dst_type"} & names
    assert not [p for p in live.parent.iterdir() if p.name.startswith(live.name + "-")]


def test_live_only_writers_refuse_inside_an_isolated_run(home, monkeypatch, tmp_path):
    from khipu import join, keychain, paths

    caller = Path(os.environ["KHIPU_DATA_DIR"])
    caller.mkdir(parents=True, exist_ok=True)
    (caller / "dsn").write_text("postgresql://live\n")
    monkeypatch.setenv("KHIPU_ISOLATED_DIR", str(tmp_path / "iso"))
    with pytest.raises(paths.IsolatedWriteRefused):
        paths.set_data_dir(tmp_path / "moved")
    with pytest.raises(paths.IsolatedWriteRefused):
        paths.import_local(source=tmp_path / "kit.zip")
    for target in (paths.dsn_file(), paths.root_cert_file()):
        with pytest.raises(paths.IsolatedWriteRefused):
            join.write_private_file(target, b"overwritten\n")
    with pytest.raises(paths.IsolatedWriteRefused):
        keychain.set_password(keychain.DSN_ACCOUNT, "overwritten")
    assert (caller / "dsn").read_text() == "postgresql://live\n"
    assert not paths.pointer_file().exists() or "moved" not in paths.pointer_file().read_text()
    assert not (tmp_path / "moved").exists()
    # Readers still read the caller's files.
    assert paths.dsn_file() == caller / "dsn"


def test_credential_reads_never_migrate_inside_an_isolated_run(home, monkeypatch, tmp_path):
    from khipu import keychain

    monkeypatch.setenv("KHIPU_ISOLATED_DIR", str(tmp_path / "iso"))
    monkeypatch.setattr(keychain, "keychain_available", lambda: True)
    monkeypatch.setattr(keychain, "get_password",
                        lambda account, service=keychain.SERVICE: "legacy" if service != keychain.SERVICE else None)
    monkeypatch.setattr(keychain, "_security", lambda *a, **k: pytest.fail("no Keychain write"))
    assert keychain.get_dsn() == "legacy" and keychain.get_gemini_key() == "legacy"
    assert keychain.migrate_legacy_secrets() == {"dsn_migrated": False, "gemini_migrated": False,
                                                 "config_dir_migrated": False}


def test_isolation_outranks_live_overrides(home, monkeypatch, tmp_path):
    from khipu import graph_backup, outbox, recall_eval

    iso = tmp_path / "iso"
    monkeypatch.setenv("KHIPU_OUTBOX", str(home / "live-outbox"))
    monkeypatch.setenv("KHIPU_GRAPH_SNAPSHOT_DIR", str(home / "live-graph"))
    monkeypatch.setenv("KHIPU_RECALL_GOLDEN", str(home / "golden.jsonl"))
    assert outbox.outbox_dir() == home / "live-outbox"
    monkeypatch.setenv("KHIPU_ISOLATED_DIR", str(iso))
    assert outbox.outbox_dir() == iso / "outbox" and outbox.dead_dir() == iso / "outbox" / "dead"
    assert graph_backup._default_snapshot_dir() == iso / "backups" / "graph"
    assert recall_eval.default_golden_path() == iso / "recall-golden.jsonl"
    outbox.enqueue({"ts": "2026-10-14T00:00:00Z", "summary": "gate", "session_id": "claude_code:khipu-gate-o"},
                   reason="test")
    assert list((iso / "outbox").glob("*.json")) and not (home / "live-outbox").exists()


def test_the_recall_client_logs_inside_the_isolated_dir(home, monkeypatch, tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "khipu_recall_client_t", pm._launcher(pm._self_root(), "khipu-recall-client.py"))
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    iso = tmp_path / "iso"
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(home / "live-capture"))
    monkeypatch.setenv("KHIPU_ISOLATED_DIR", str(iso))
    assert client.socket_path() == str(iso / "capture" / "recall.sock")
    client.log_failure('{"session_id": "khipu-gate-c"}', "timeout", 0.0)
    assert "session=khipu-gate-c" in (iso / "capture" / "logs" / "prompt-recall.log").read_text()
    assert not (home / "live-capture").exists()


def test_after_a_flip_the_replica_job_is_woken_to_heal_at_once(home, monkeypatch):
    """A promote that changes the replica schema must not leave the old copy
    in place until the job's next interval: the watched wake file is touched."""
    from khipu import replica_heal
    from khipu.jobs import PLIST_REPLICA_REFRESH, _plist_path

    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    monkeypatch.setattr(pm, "_run", lambda argv, **_kw: subprocess.CompletedProcess(argv, 0, "{}", ""))
    out = pm._after_flip()
    assert "replica_woken" not in out and not replica_heal.wake_path().exists()

    plist = _plist_path(PLIST_REPLICA_REFRESH)
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("<plist/>")
    wake = replica_heal.ensure_wake_file()
    os.utime(wake, (1, 1))
    out = pm._after_flip()
    assert out["replica_woken"] is True
    assert wake.stat().st_mtime > 1
