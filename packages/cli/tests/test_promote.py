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
    assert pm.flip(b)["ok"]
    # The last successful promotion went a (first) -> b (second) ...
    pm.record({"action": "promote", "ok": True, "from_slot": str(a), "from_commit": first,
               "slot": str(b), "commit": second})
    # ... and a later failed promotion moved a to second.
    pm._prepare_slot(a, second, r)
    monkeypatch.setattr(pm, "_after_flip", lambda: {"kicked": []})
    out = pm.rollback()
    assert out["ok"] and out["restored"] == first
    assert pm._head(pm.live_link()) == first


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
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "refreshed": []}), "")

    monkeypatch.setattr(pm, "_run", fake_run)
    from khipu.jobs import PLIST_QUEUE_DRAIN, _plist_path

    _plist_path(PLIST_QUEUE_DRAIN).parent.mkdir(parents=True, exist_ok=True)
    _plist_path(PLIST_QUEUE_DRAIN).write_text("<plist/>")
    out = pm.promote()
    assert out["ok"], out
    assert os.readlink(pm.live_link()) == str(b)
    assert any(argv[-2:] == ["jobs", "refresh"] for argv in calls)
    assert any(argv[:3] == ["launchctl", "kickstart", "-k"] and argv[3].endswith(PLIST_QUEUE_DRAIN)
               for argv in calls)
    assert pm.MCP_NOTE in pm.format_result(out)


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


def _summary(when: datetime, reason: str, hits: list[str], prompt: str | None) -> str:
    stamp = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    line = (f"{stamp} [khipu-prompt-recall] session=s1 reason={reason} ms=300.0 hits={hits} "
            f"legs=['lexical'] degraded=None")
    if prompt is not None:
        line += " replay=" + json.dumps({"prompt": prompt, "cwd": "/work/project"})
    return line


def test_replay_reads_only_the_hook_log_and_never_a_credential(home, monkeypatch):
    import khipu.recall_prompt  # noqa: F401 — imported before the spy is installed

    now = datetime.now(timezone.utc)
    log = home / ".grok" / "khipu" / "logs" / "prompt-recall.log"
    log.parent.mkdir(parents=True)
    log.write_text("\n".join([
        _summary(now - timedelta(days=3), "ok", ["episode:1"], "too old to replay"),
        _summary(now - timedelta(hours=5), "trivial acknowledgment", [], "ok thanks"),
        _summary(now - timedelta(hours=4), "ok", ["episode:2"], None),
        _summary(now - timedelta(hours=3), "ok", ["episode:3", "topic:x"], "where did we leave the gate"),
        _summary(now - timedelta(hours=2), "ok", ["episode:4"], "what about the release card"),
        "2026-10-14T00:00:00Z [khipu-prompt-recall] snapshot unusable (x) — falling back to hub",
    ]) + "\n")
    # Things the replay must never touch.
    secret_file = home / ".config" / "khipu" / "dsn"
    secret_file.parent.mkdir(parents=True)
    secret_file.write_text("postgresql://user:hunter2@db.example.invalid/khipu")
    monkeypatch.setenv("FAKE_PROVIDER_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("GATEWAY_TOKEN", "not-real-either")
    live_hook = home / ".config" / "khipu" / "bin" / "khipu-prompt-recall"
    live_hook.parent.mkdir(parents=True)
    live_hook.write_text("#!/bin/sh\n")
    candidate = _fake_checkout(home / "candidate")
    (candidate / "packages" / "cli" / "bin" / "khipu-prompt-recall").write_text("#!/bin/sh\n")

    envs = []

    def fake_run(argv, *, env=None, input=None, cwd=None, timeout=120):
        envs.append(dict(env))
        payload = json.loads(input)
        capture_home = Path(env["KHIPU_CAPTURE_HOME"])
        (capture_home / "logs").mkdir(parents=True, exist_ok=True)
        hits = ["episode:3"] if "candidate" in argv[0] else ["episode:3", "topic:x"]
        with (capture_home / "logs" / "prompt-recall.log").open("a") as fh:
            fh.write(_summary(datetime.now(timezone.utc), "ok", hits, payload["prompt"]) + "\n")
        return subprocess.CompletedProcess(argv, 0, '{"hookSpecificOutput": {}}\n', "")

    monkeypatch.setattr(pm, "_run", fake_run)
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
    assert len(envs) == 4  # live and candidate, for each of the two replayable prompts
    for env in envs:
        assert not [k for k in env if "KEY" in k or "TOKEN" in k], env
        assert env["KHIPU_RECALL_DAEMON"] == "0"
    assert "*every variable*" not in reads
    assert not [k for k in reads if "KEY" in k or "TOKEN" in k], reads
    files = [p for p in opened if isinstance(p, Path)]
    assert log in files
    assert secret_file not in files
    for p in files:
        assert p == log or "khipu-gate-recall-" in str(p), p


def test_replay_refuses_an_empty_answer_where_live_had_one(home, monkeypatch):
    now = datetime.now(timezone.utc)
    log = home / ".grok" / "khipu" / "logs" / "prompt-recall.log"
    log.parent.mkdir(parents=True)
    log.write_text(_summary(now - timedelta(hours=1), "ok", ["episode:9"], "the queue drain path") + "\n")
    live_hook = home / ".config" / "khipu" / "bin" / "khipu-prompt-recall"
    live_hook.parent.mkdir(parents=True)
    live_hook.write_text("#!/bin/sh\n")
    candidate = _fake_checkout(home / "candidate")
    (candidate / "packages" / "cli" / "bin" / "khipu-prompt-recall").write_text("#!/bin/sh\n")

    def fake_run(argv, *, env=None, input=None, cwd=None, timeout=120):
        capture_home = Path(env["KHIPU_CAPTURE_HOME"])
        (capture_home / "logs").mkdir(parents=True, exist_ok=True)
        hits = [] if "candidate" in argv[0] else ["episode:9"]
        (capture_home / "logs" / "prompt-recall.log").write_text(
            _summary(datetime.now(timezone.utc), "ok", hits, "the queue drain path") + "\n")
        return subprocess.CompletedProcess(argv, 0, "{}\n", "")

    monkeypatch.setattr(pm, "_run", fake_run)
    out = pm.gate_recall_replay(candidate)
    assert not out["ok"] and "empty answer where live had 1 hit" in out["detail"]


def test_replay_with_nothing_to_replay_is_a_failure_not_a_pass(home):
    out = pm.gate_recall_replay(_fake_checkout(home / "candidate"))
    assert not out["ok"] and "no replayable prompt" in out["detail"]


def test_the_hook_log_line_carries_its_prompt_for_replay():
    from khipu.recall_prompt import replay_field

    field = replay_field({"prompt": "where did we leave the gate", "cwd": "/work"})
    line = _summary(datetime.now(timezone.utc), "ok", ["episode:1"], None) + field
    row = pm._parse_summary(line)
    assert row["prompt"] == "where did we leave the gate" and row["cwd"] == "/work"
    assert row["hits"] == ["episode:1"]
    assert replay_field({"prompt": ""}) == "" and replay_field({}) == ""


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


def test_capture_step_finds_and_forgets_the_episode(home, monkeypatch):
    candidate = _fake_checkout(home / "candidate")
    (candidate / "packages" / "cli" / "bin" / "khipu-stop-hook").write_text("#!/bin/sh\n")
    calls = []

    def fake_run(argv, *, env=None, input=None, cwd=None, timeout=120):
        calls.append((argv, env))
        if argv[0].endswith("khipu-stop-hook"):
            assert env["KHIPU_HOOK_INLINE"] == "1" and env["KHIPU_GATE"] == "1"
            assert env["KHIPU_CAPTURE_HOME"] != str(home / ".grok" / "khipu")
            assert json.loads(input)["hook_event_name"] == "SessionEnd"
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[1] == "-c":
            return subprocess.CompletedProcess(argv, 0, "[41]\n", "")
        return subprocess.CompletedProcess(argv, 0, '{"ok": true}', "")

    monkeypatch.setattr(pm, "_run", fake_run)
    out = pm.gate_capture(candidate)
    assert out["ok"], out
    assert calls[-1][0][-3:] == ["episode", "forget", "41"]
    assert calls[-1][1]["PYTHONPATH"].startswith(str(candidate / "packages" / "cli"))


def test_capture_step_fails_when_no_episode_lands(home, monkeypatch):
    candidate = _fake_checkout(home / "candidate")
    (candidate / "packages" / "cli" / "bin" / "khipu-stop-hook").write_text("#!/bin/sh\n")
    monkeypatch.setattr(pm, "_run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "[]", ""))
    out = pm.gate_capture(candidate)
    assert not out["ok"] and "no episode" in out["detail"]


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
