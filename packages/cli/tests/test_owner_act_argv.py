"""owner_act builds argv without ``--`` so it parses the same on Python 3.11 and later."""
import os
import subprocess

import pytest

from khipu import owner_act

PY311 = "/opt/homebrew/bin/python3.11"


def test_built_argv_never_contains_double_dash(monkeypatch):
    from khipu import cli

    seen = []

    class Stop(Exception):
        pass

    class P:
        def parse_args(self, full):
            seen.append(full)
            raise Stop

    monkeypatch.setattr(cli, "build_parser", lambda: P())
    for action, args in (("route.pause", {"account": "codex", "until": "5pm"}),
                         ("route.rule.confirm", {"rule": 3}),
                         ("route.hold.end", {"hold": "h1"}),
                         ("work.same", {"row": "a", "commitment": "b"})):
        with pytest.raises(Stop):
            owner_act.act(action, args)
    assert len(seen) == 4
    assert all("--" not in full for full in seen)
    assert seen[0][:3] == ["route", "pause", "codex"]
    assert all(full[-1] == "--owner-direction" for full in seen)


def test_a_positional_starting_with_a_dash_is_refused():
    ok, out = owner_act.act("route.note.set", {"text": "--from-app"})
    assert not ok and out["code"] == "bad_args"


@pytest.mark.skipif(not os.path.exists(PY311), reason="python3.11 not installed")
def test_real_parser_reads_the_argv_under_python311():
    code = (
        "from khipu import cli, owner_act\n"
        "cases = [('route.rule.confirm', {'rule': 2}), ('route.pause', {'account': 'codex', 'until': '5pm'}),\n"
        "         ('route.hold.end', {'hold': 'h1'}), ('route.note.set', {'text': 'hello there'}),\n"
        "         ('work.same', {'row': 'a', 'commitment': 'b'}), ('direction.undo', {'id': 'x'})]\n"
        "for action, args in cases:\n"
        "    h, p = owner_act.argv(action, owner_act._values(action, args))\n"
        "    f = owner_act.full_argv(h, p, ['--owner-direction'])\n"
        "    ns = cli.build_parser().parse_args(f)\n"
        "    assert ns.by_direction is True, action\n"
        "print('ok')\n"
    )
    pkg = os.path.dirname(os.path.dirname(owner_act.__file__))
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([pkg, os.environ.get("PYTHONPATH", "")]),
               KHIPU_KEYCHAIN="0", KHIPU_TEST="1")
    r = subprocess.run([PY311, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok"
