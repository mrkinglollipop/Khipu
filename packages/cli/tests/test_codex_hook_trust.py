"""The full doctor asks Codex whether it trusts Khipu's hooks (read-only), with
a stand-in process for Codex's app-server; the real Codex is never run."""
from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

from khipu import codex_hook_trust as trust, integrations as integ
from tests.test_hooks_stay_current import _wire_everything, home  # noqa: F401  (fixture)


STAND_IN = r'''#!{python}
import json, sys, time
mode = {mode!r}
status = {status!r}
if mode == "hang":
    time.sleep(60)
for line in sys.stdin:
    m = json.loads(line)
    if m.get("method") == "initialize":
        print(json.dumps({{"id": m["id"], "result": {{"userAgent": "stand-in"}}}}), flush=True)
    elif m.get("method") == "hooks/list":
        if mode == "error":
            print(json.dumps({{"id": m["id"], "error": {{"code": -1, "message": "no"}}}}), flush=True)
        elif mode == "garbage":
            print(json.dumps({{"id": m["id"], "result": {{"nope": 1}}}}), flush=True)
        else:
            hooks = [{{"key": "a", "currentHash": "h", "trustStatus": s, "command": {cmd!r}, "handlerType": "command"}}
                     for s in status]
            hooks.append({{"key": "z", "currentHash": "h", "trustStatus": "untrusted", "command": "/other/tool"}})
            print(json.dumps({{"id": m["id"], "result": {{"data": [{{"cwd": m["params"]["cwds"][0], "hooks": hooks,
                                                                     "errors": [], "warnings": []}}]}}}}), flush=True)
        break
'''


def _stand_in(tmp_path: Path, mode="ok", status=("trusted",)) -> str:
    path = tmp_path / "codex"
    path.write_text(STAND_IN.format(python=sys.executable, mode=mode, status=list(status), cmd=integ.stop_hook()))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    # `codex app-server`: the stand-in ignores its argument.
    return str(path)


def test_trusted_hooks_are_installed_and_trusted(tmp_path):
    assert trust.ask(binary=_stand_in(tmp_path, status=("trusted", "managed")))["words"] == "installed and trusted"


def test_untrusted_or_modified_hooks_wait_for_review(tmp_path):
    for status in (("untrusted",), ("trusted", "modified")):
        out = trust.ask(binary=_stand_in(tmp_path, status=status))
        assert out["state"] == "waiting" and out["words"] == "installed, waiting for your review in Codex"


def test_other_tools_hooks_do_not_count(tmp_path):
    # The stand-in always lists an untrusted hook that is not Khipu's.
    assert trust.ask(binary=_stand_in(tmp_path, status=("trusted",)))["state"] == "trusted"


def test_a_missing_binary_an_error_and_an_unreadable_answer_are_could_not_ask(tmp_path):
    for binary in (str(tmp_path / "nothing-here"), _stand_in(tmp_path, mode="error"), _stand_in(tmp_path, mode="garbage"),
                   _stand_in(tmp_path, status=())):
        out = trust.ask(binary=binary)
        assert out["state"] == "unknown" and out["words"].startswith("installed; could not ask Codex")


def test_a_hung_codex_is_killed_at_the_limit(tmp_path):
    start = time.monotonic()
    out = trust.ask(binary=_stand_in(tmp_path, mode="hang"), timeout=0.7)
    assert time.monotonic() - start < 5
    assert out["state"] == "unknown" and "did not answer in time" in out["words"]


def test_the_row_carries_the_answer_only_when_asked(home, tmp_path, monkeypatch):  # noqa: F811
    _wire_everything(home)
    monkeypatch.setattr(trust, "codex_binary", lambda: _stand_in(tmp_path, status=("untrusted",)))
    quiet = integ.hook_wiring_report()
    assert all("trust" not in r for r in quiet["harnesses"]) and quiet["ok"]
    asked = integ.hook_wiring_report(ask_codex=True)
    codex = next(r for r in asked["harnesses"] if r["harness"] == "codex")
    assert codex["state"] == "waiting for review" and codex["words"] == "installed, waiting for your review in Codex"
    assert any(w.startswith("Codex:") and "waiting for your review in Codex" in w and "khipu " not in w
               for w in asked["warnings"])
    monkeypatch.setattr(trust, "codex_binary", lambda: _stand_in(tmp_path, status=("trusted",)))
    ok = integ.hook_wiring_report(ask_codex=True)
    codex = next(r for r in ok["harnesses"] if r["harness"] == "codex")
    assert codex["state"] == "current" and codex["words"] == "installed and trusted" and ok["ok"]


def test_quick_doctor_and_the_hook_paths_never_ask(monkeypatch):
    from khipu import cli

    def boom(*a, **k):
        raise AssertionError("asked Codex")

    monkeypatch.setattr(trust, "ask", boom)
    seen = []
    monkeypatch.setattr(integ, "hook_wiring_report", lambda **kw: seen.append(kw) or {"warnings": []})
    cli._hook_wiring_block()
    assert seen == [{}]
    cli._hook_wiring_block(ask_codex=True)
    assert seen[-1] == {"ask_codex": True}
