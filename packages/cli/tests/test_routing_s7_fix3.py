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
@pytest.mark.parametrize("inventory", ["none", "default-only", "one-private"])
def test_C_innocuous_lane_visibility(home, monkeypatch, inventory):
    monkeypatch.setenv("CODEX_THREAD_ID", "lane")
    if inventory in ("default-only", "one-private"):
        (home["tmp"] / ".codex").mkdir()
    if inventory == "one-private":
        (home["tmp"] / ".claude-work").mkdir()
    _configure(home, routing_lane_map={"church-harness": {"glm": "NoSuchAccount"}})
    routing.report(state="limited", runner="church-harness", lane="glm", until="1h",
                   author={"kind": "conversation", "harness": "codex", "session": "lane"})
    body = _route()
    lanes = [i for i in body.get("routing_changes", {}).get("items", []) if i["entity"].startswith("lane:")]
    assert body["unmatched"] and lanes


# D: complete inventory distinguishes a harmless lane from a private login
@pytest.mark.parametrize("error", [False, True])
def test_D_partial_inventory_unknown_alias(home, monkeypatch, error):
    monkeypatch.setenv("CODEX_THREAD_ID", "alias")
    (home["tmp"] / ".claude-work").mkdir()
    _configure(home, routing_lane_map={"runnerx": {ALIAS: "NoSuchAccount"}})
    routing.report(state="limited", runner="runnerx", lane=ALIAS, until="1h",
                   author={"kind": "conversation", "harness": "codex", "session": "alias"})
    _, body = _call("khipu_route", {"since": "bad"} if error else {})
    assert ALIAS in json.dumps(body)


# E: only the secret token is removed from current owner words
def test_E_owner_note_with_secret(home, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "e")
    routing.set_note("Use key " + SECRET + " for nothing", via="app")
    body = _route()
    s = json.dumps(body)
    assert SECRET not in s
    assert body["note"]["text"] == "Use key [REDACTED] for nothing"
    items = [i for i in body["routing_changes"]["items"] if i["entity"] == "note"]
    assert items and "Use key [REDACTED] for nothing" in items[0]["text"]
    _route(ack=body["routing_changes"]["batch"])
    entry = routing_delivery.snapshot("codex:e", "mcp")["entries"]["note"]
    assert entry["acked"] and entry["offset"] == len(entry["text"])



# F: owner words with a login path survive whole in result and in routing_changes, and ack fully
def test_F_owner_note_with_path_survives(home, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "f")
    login = home["tmp"] / ".codex-private2"
    login.mkdir()
    words = f"Owner says use {login} for routing work"
    routing.set_note(words, via="app")
    body = _route()
    texts = [i.get("text") for i in body.get("routing_changes", {}).get("items", []) if i["entity"] == "note"]
    assert texts and words in texts[0]
    _route(ack=body["routing_changes"]["batch"])
    entry = routing_delivery.snapshot("codex:f", "mcp")["entries"]["note"]
    assert entry["acked"]


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
@pytest.mark.parametrize("words_kind", ["path", "keysyntax"])
def test_F2_owner_words_with_stale_saved_view(home, monkeypatch, words_kind):
    monkeypatch.setenv("CODEX_THREAD_ID", "f2")
    login = home["tmp"] / ".codex-private3"
    login.mkdir()
    words = {"path": f"Owner says use {login} for routing work",
             "keysyntax": "Owner says codex|work is the second Codex"}[words_kind]
    routing.set_note(words, via="app")
    monkeypatch.setattr(agent_view, "_saved_view", lambda: {})
    body = _route()
    texts = [i.get("text") for i in body.get("routing_changes", {}).get("items", []) if i["entity"] == "note"]
    assert body["note"]["text"] == words
    assert texts and words in texts[0]


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


def test_response_and_delivery_share_one_folded_view(home, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "one-view")
    words = "Owner says codex|work is the second Codex"
    routing.set_note(words, via="app")
    original = routing.read_view
    reads = []
    def read(*args, **kwargs):
        view = original(*args, **kwargs)
        reads.append(view)
        return view
    monkeypatch.setattr(routing, "read_view", read)
    monkeypatch.setattr(agent_view, "_saved_view", lambda: {})
    body = _route()
    assert len(reads) == 1
    assert body["note"]["text"] == words
    assert any(i["entity"] == "note" and words in i["text"] for i in body["routing_changes"]["items"])


@pytest.mark.parametrize("encoding", ["plain", "percent", "html"])
def test_owner_secret_redaction_preserves_paged_words_and_clears_debt(home, monkeypatch, encoding):
    monkeypatch.setenv("CODEX_THREAD_ID", "owner-secret-pages")
    _configure(home, routing_prompt_chars=400)
    token = {"plain": SECRET, "percent": "%73" + SECRET[1:], "html": "&#115;" + SECRET[1:]}[encoding]
    words = "Owner says use " + token + " for nothing. " + "Keep these owner words. " * 40
    routing.set_note(words, via="app")
    body = _route()
    expected = words.replace(token, "[REDACTED]")
    assert body["note"]["text"] == expected
    chunks = []
    for _ in range(20):
        changes = body.get("routing_changes")
        if not changes:
            break
        assert SECRET not in json.dumps(body) and token not in json.dumps(body)
        chunks.extend(i["text"] for i in changes["items"] if i["entity"] == "note")
        body = _route(ack=changes["batch"])
    assert expected in "".join(chunks)
    state = routing_delivery.snapshot("codex:owner-secret-pages", "mcp")
    assert state["entries"]["note"]["acked"]
    assert state["acked_through"] is not None and body["delivery"]["owed"] == 0


def test_owner_credential_cannot_hide_prefix_of_pending_credential(home, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "credential-prefix")
    routing.set_note(SECRET, via="app")
    secret = SECRET + "MoreSecretCharacters"
    routing.request_rule("Use " + secret, author={"kind": "conversation", "harness": "codex"})
    body = _route()
    assert "MoreSecretCharacters" not in json.dumps(body)
    entry = next(e for k, e in routing_delivery.snapshot("codex:credential-prefix", "mcp")["entries"].items()
                 if k.startswith("rule:"))
    assert entry["offset"] == 0 and not entry["acked"]


@pytest.mark.parametrize("paged", [False, True])
def test_legacy_owner_secret_reservation_is_replaced_before_retransmission(home, monkeypatch, paged):
    monkeypatch.setenv("CODEX_THREAD_ID", "legacy-note")
    if paged:
        _configure(home, routing_prompt_chars=400)
    words = "Use key " + SECRET + " for nothing" + (". Keep these words" * 80 if paged else "")
    routing.set_note(words, via="app")
    view = routing.read_view()
    with monkeypatch.context() as old:
        old.setattr(routing, "_for_agents", lambda value, view=None: value)
        planned = routing_delivery.plan({"kind": "full", "text": "", "version": view["version"],
                                         "epoch": view["epoch"]}, view, home["clock"].now, routing.settings())
        changes, _ = routing_delivery.pull_offer(planned, session="codex:legacy-note")
    assert any(SECRET in i["text"] for i in changes["items"] if i["entity"] == "note")
    if paged:
        assert routing_delivery.pull_acknowledge("codex:legacy-note", changes["batch"]) == "acked"
        changes, _ = routing_delivery.pull_offer(planned, session="codex:legacy-note")
    body = _route(ack=changes["batch"])
    assert SECRET not in json.dumps(body)
    assert body["routing_changes"]["batch"] != changes["batch"]
    entry = routing_delivery.snapshot("codex:legacy-note", "mcp")["entries"]["note"]
    assert not entry["acked"] and entry["offset"] == 0
    chunks = []
    for _ in range(20):
        current = body.get("routing_changes")
        if not current:
            break
        assert SECRET not in json.dumps(body)
        chunks.extend(i["text"] for i in current["items"] if i["entity"] == "note")
        body = _route(ack=current["batch"])
    assert words.replace(SECRET, "[REDACTED]") in "".join(chunks)
    assert routing_delivery.snapshot("codex:legacy-note", "mcp")["entries"]["note"]["acked"]
