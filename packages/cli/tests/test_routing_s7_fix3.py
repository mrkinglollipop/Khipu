# ruff: noqa: F811
"""Round-three regression probes for routing slice 7, through the real khipu_route handler."""
import json

import pytest

from khipu import agent_view, recall_prompt, routing, routing_delivery
from tests.test_routing_s7_fix1 import home, _call, _route, _configure  # noqa: F401

SECRET = "sk-proj-" + "Z" * 40
ALIAS = "SecretAliasZZZ"


@pytest.fixture(autouse=True)
def isolated(home, monkeypatch):
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(home["tmp"] / "capture"))
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    monkeypatch.delenv("ROUTING_NOW_OFF", raising=False)
    monkeypatch.setattr(agent_view, "_TABLE", {"sig": None, "at": 0.0, "table": None})


def _ledgers(home):
    return sorted(p.name for p in home["tmp"].rglob("*.routing.json"))


# A: a pull-only child cannot acknowledge its environment-identified parent
def test_A_child_under_env_parent_acks_parent(home, monkeypatch):
    monkeypatch.setenv("KHIPU_HARNESS", "aegis")
    monkeypatch.setenv("KHIPU_SESSION_ID", "t3:parent")
    routing.add_rule("owner debt", via="app")
    parent_batch = _route()["routing_changes"]["batch"]
    # The child inherited the parent's environment and passes its own id, as the instructions say.
    error, body = _call("khipu_route", {"session_id": "t3:parent/child", "ack": parent_batch})
    assert error and body["code"] == "ack_not_yours"
    snap = routing_delivery.snapshot("t3:parent", "mcp")
    assert parent_batch not in snap.get("batches", {}), "child's ack moved the parent's ledger"
    assert not snap.get("acked_through")
    assert routing_delivery.snapshot("t3:parent/child", "mcp")["flight"]


# B: a child id under a native/T3 root with no env is refused outright (untracked)
@pytest.mark.parametrize("sid", ["t3:parent/child", "codex:parent/child", "parent/child"])
def test_B_child_ids_without_env(home, sid):
    routing.add_rule("owner debt", via="app")
    body = _route(session_id=sid)
    if sid.startswith(("t3:", "codex:")):
        assert body["delivery"] == "untracked: pass session_id"
    else:
        assert body["routing_changes"]["batch"]


# C: a complete default-only inventory covers innocuous lane notices
# D: complete inventory distinguishes a harmless lane from a private login
# E: only the secret token is removed from current owner words
# F: owner words with a login path survive whole in result and in routing_changes, and ack fully
# G: a pending conversation rule with a login path and with a secret: absent; never acked
@pytest.mark.parametrize("payload", ["path", "secret", "key", "alias"])
@pytest.mark.parametrize("error", [False, True])
def test_G_pending_rule_payloads(home, monkeypatch, payload, error):
    monkeypatch.setenv("CODEX_THREAD_ID", "g")
    login = home["tmp"] / ".codex-privateQQ"
    login.mkdir()
    word = {"path": str(login), "secret": SECRET, "key": "codex|" + str(login), "alias": "codex-privateQQ"}[payload]
    routing.request_rule("Use " + word + " now", author={"kind": "conversation", "harness": "codex", "session": "g"})
    args = {"since": "bad"} if error else {}
    _, body = _call("khipu_route", args)
    for _ in range(6):
        ch = body.get("routing_changes")
        if not ch or not ch.get("batch"):
            break
        _, body = _call("khipu_route", dict(args, ack=ch["batch"]))
    s = json.dumps(body)
    leaked = [x for x in (word, "privateQQ", str(home["tmp"]), SECRET) if x in s]
    entry = next((e for k, e in routing_delivery.snapshot("codex:g", "mcp").get("entries", {}).items()
                  if k.startswith("rule:")), None)
    assert not leaked


# H: sentinels in arguments that may be echoed by errors
@pytest.mark.parametrize("arg", ["since", "since_epoch", "account", "session_id", "ack"])
@pytest.mark.parametrize("payload", ["secret", "path", "key"])
def test_H_argument_echo(home, monkeypatch, arg, payload):
    monkeypatch.setenv("CODEX_THREAD_ID", "h")
    login = home["tmp"] / ".codex-privateHH"
    login.mkdir()
    word = {"secret": SECRET, "path": str(login), "key": "codex|" + str(login)}[payload]
    routing.add_rule("owner debt", via="app")
    args = {"since_epoch": {"since": word + ":1"}}.get(arg, {arg: word})
    if arg == "session_id":
        monkeypatch.delenv("CODEX_THREAD_ID")
    err, body = _call("khipu_route", args)
    s = json.dumps(body)
    leaked = [x for x in (word, "privateHH", SECRET) if x in s]
    assert not leaked


# I: a short owner phrase cannot mask part of account-key syntax
@pytest.mark.parametrize("error", [False, True])
def test_I_owner_mask_hides_account_key(home, monkeypatch, error):
    monkeypatch.setenv("CODEX_THREAD_ID", "i")
    routing.add_rule("Prefer codex", via="app")
    routing.request_rule("Prefer codex|" + ALIAS, author={"kind": "conversation", "harness": "codex", "session": "i"})
    _, body = _call("khipu_route", {"since": "bad"} if error else {})
    s = json.dumps(body)
    assert "codex|" + ALIAS not in s


# J: unidentified caller: no ledger file at all, even with ack and an error
@pytest.mark.parametrize("args", [{}, {"ack": "whatever"}, {"since": "bad"}, {"since": "0"}])
def test_J_unidentified_consumes_nothing(home, args):
    routing.add_rule("owner debt", via="app")
    before = _ledgers(home)
    err, body = _call("khipu_route", args)
    after = _ledgers(home)
    assert before == after


# K: hook (prompt helper path), and MCP land in one ledger for Claude Code env
def test_K_claude_hook_and_mcp_share_ledger(home, monkeypatch):
    _configure(home, routing_prompt_budget_ms=1000, routing_reset_check=False)
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc")
    monkeypatch.setenv("CLAUDECODE", "1")
    routing.add_rule("one ledger", via="app")
    import os
    at = recall_prompt._RoutingAtPrompt().begin("abc", dict(os.environ), None)
    out = at.collect()
    if out and out.get("routing_changes"):
        _, info = at.add_to("", out)
        recall_prompt.finish_delivery({"routing": info}, delivered=True)
    body = _route()
    files = _ledgers(home)
    data = json.loads(routing_delivery._path("claude:abc").read_text())
    assert len(files) == 1 and {"main", "mcp"} <= set(data)


# L: ack of another identity's batch is refused and does not move it (explicit vs explicit)
def test_L_explicit_identities_isolated(home):
    routing.add_rule("owner debt", via="app")
    a = _route(session_id="client:a")["routing_changes"]["batch"]
    err, body = _call("khipu_route", {"session_id": "client:b", "ack": a})
    assert err and body.get("code") == "ack_not_yours"
    assert _route(session_id="client:a")["routing_changes"]["batch"] == a


# I2: owner-mask bypass with a real login folder name in key form
@pytest.mark.parametrize("form", ["bare", "dot", "tilde", "abs"])
def test_I2_owner_mask_real_login(home, monkeypatch, form):
    monkeypatch.setenv("CODEX_THREAD_ID", "i2")
    login = home["tmp"] / ".codex-privateRR"
    login.mkdir()
    tail = {"bare": "codex-privateRR", "dot": ".codex-privateRR", "tilde": "~/.codex-privateRR", "abs": str(login)}[form]
    routing.add_rule("Prefer codex", via="app")
    routing.request_rule("Prefer codex|" + tail, author={"kind": "conversation", "harness": "codex", "session": "i2"})
    body = _route()
    s = json.dumps(body)
    pend = [r.get("text") for r in body.get("rules", []) if r.get("pending")]
    assert "privateRR" not in s


# F2: final dispatch uses the folded view when the saved view lags
@pytest.mark.parametrize("env,root", [
    ({"KHIPU_SESSION_ID": "t3:parent", "KHIPU_HARNESS": "aegis"}, "t3:parent"),
    ({"CODEX_THREAD_ID": "parent"}, "codex:parent"),
    ({"CLAUDE_CODE_SESSION_ID": "parent", "CLAUDECODE": "1"}, "claude:parent"),
])
def test_child_ack_is_separate_from_environment_parent(home, monkeypatch, env, root):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    routing.add_rule("separate child debt", via="app")
    parent_batch = _route()["routing_changes"]["batch"]
    child = root + "/child/grandchild"
    batch = _route(session_id=child)["routing_changes"]["batch"]
    assert batch != parent_batch
    _route(session_id=child, ack=batch)
    assert routing_delivery.snapshot(child, "mcp")["acked_through"] is not None
    parent = routing_delivery.snapshot(root, "mcp")
    assert not parent.get("acked_through") and parent["flight"]["out"]["routing_changes"]["batch"] == parent_batch
    error, body = _call("khipu_route", {"session_id": child, "ack": parent_batch})
    assert error and body["code"] == "ack_not_yours"
    assert _route()["routing_changes"]["batch"] == parent_batch


@pytest.mark.parametrize("suffix", ["/", "/../sibling", "/./child", "//child", "ish/child", "/child/", "/child\0bad"])
def test_non_descendants_are_refused_without_ledger_writes(home, monkeypatch, suffix):
    monkeypatch.setenv("KHIPU_SESSION_ID", "t3:parent")
    before = _ledgers(home)
    error, body = _call("khipu_route", {"session_id": "t3:parent" + suffix})
    assert error and body["code"] == "session_not_yours"
    assert _ledgers(home) == before


def test_unknown_ack_has_bounded_locked_ledger_reads(home, monkeypatch):
    from pathlib import Path
    import fcntl

    root = routing_delivery._path("owner").parent
    root.mkdir(parents=True, exist_ok=True)
    for n in range(100):
        (root / f"other-{n}.routing.json").write_text("{}")
    reads, locks = [], []
    original_read, original_lock = Path.read_text, fcntl.flock
    def read(path, *args, **kwargs):
        if path.name.endswith(".routing.json"):
            reads.append(path)
            assert len(locks) >= len(reads)
        return original_read(path, *args, **kwargs)
    def lock(fd, flags):
        if flags == fcntl.LOCK_SH | fcntl.LOCK_NB:
            locks.append(fd)
        return original_lock(fd, flags)
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(fcntl, "flock", lock)
    assert not routing_delivery._batch_owned_elsewhere("owner", "unknown")
    assert 0 < len(reads) <= 8


def test_unknown_ack_skips_busy_ledger(home):
    import fcntl

    path = routing_delivery._path("other")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcp": {"batches": {"foreign": True}}}))
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not routing_delivery._batch_owned_elsewhere("owner", "foreign")
    assert routing_delivery._batch_owned_elsewhere("owner", "foreign")

