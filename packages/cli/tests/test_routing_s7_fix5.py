"""Routing slice 7 fix round 5: the strict-scrub pre-screen must not skip
secrets spelled with Unicode look-alikes that re.I folds onto ASCII."""
from __future__ import annotations

import html
import json
import random
import urllib.parse

import pytest

from khipu import agent_view, routing
from khipu.redact import redact_secrets
from tests.test_routing_s7_fix1 import _call, _configure, home  # noqa: F401

VAL = "Hunter2passXYZ"
AUTH = {"kind": "conversation", "harness": "codex", "session": "s"}
Z = "Z" * 40

# Every redact_secrets family, with a value long enough to match.
FAMILIES = {
    "private-key": "-----BEGIN RSA PRIVATE KEY-----\nMIIabc123\n-----END RSA PRIVATE KEY-----",
    "anthropic": "sk-ant-" + Z,
    "openai": "sk-proj-" + Z,
    "openai-plain": "sk-" + Z,
    "xai": "xai-" + Z,
    "stripe-live": "sk_live_" + "A" * 20,
    "stripe-test": "rk_test_" + "A" * 20,
    "aws-akid": "AKIA" + "A" * 16,
    "aws-session": "ASIA" + "B" * 16,
    "github-pat-old": "ghp_" + "a" * 36,
    "github-oauth": "gho_" + "a" * 36,
    "github-user": "ghu_" + "a" * 36,
    "github-server": "ghs_" + "a" * 36,
    "github-refresh": "ghr_" + "a" * 36,
    "github-pat": "github_pat_" + "a" * 40,
    "slack": "xoxb-" + "1" * 12,
    "google": "AIza" + "a" * 35,
    "gemini-like": "ya29." + "a" * 35,
    "jwt": "eyJ" + "a" * 8 + "." + "b" * 8 + "." + "c" * 8,
    "auth-header-bearer": "Bearer " + "a" * 20,
    "auth-header-basic": "Basic " + "a" * 20,
    "auth-header-x-api-key": "x-api-key: " + "a" * 20,
    "auth-header-api-key": "api-key: " + "a" * 20,
    "auth-header-x-goog": "x-goog-api-key: " + "a" * 20,
    "url-credential": "https://user:" + VAL + "@host/x",
    "quoted-assignment": '"api_key": "' + VAL + '"',
    "quoted-assignment-secret": "'client_secret' = '" + VAL + "'",
    "assignment-api-key": "api_key=" + VAL,
    "assignment-secret": "secret=" + VAL,
    "assignment-password": "password=" + VAL,
    "assignment-connection": "connection_string=" + VAL,
    "assignment-private-key": "private_key=" + VAL,
    "assignment-token": "access_token=" + VAL,
}

# Unicode letters re.I folds onto ASCII ones. Each maps both cases.
LOOKALIKES = {
    "long-s": str.maketrans({"s": "ſ", "S": "ſ"}),
    "dotless-i": str.maketrans({"i": "ı", "I": "ı"}),
    "dotted-I": str.maketrans({"i": "İ", "I": "İ"}),
    "kelvin": str.maketrans({"k": "K", "K": "K"}),
}


def _redact_catches(text: str) -> bool:
    return any(redact_secrets(v, force=True)[1] for v in agent_view._decoded_variants(text))


@pytest.mark.parametrize("lookalike", list(LOOKALIKES))
@pytest.mark.parametrize("family", list(FAMILIES))
def test_scrub_strict_withholds_lookalike_secrets(family, lookalike):
    mutated = FAMILIES[family].translate(LOOKALIKES[lookalike])
    if not _redact_catches(mutated):
        pytest.skip("redact_secrets itself does not match this spelling")
    out = agent_view.scrub_strict({"t": mutated})
    assert out == {"t": agent_view.WITHHELD}, (family, lookalike, mutated, out)
    assert VAL not in json.dumps(out, ensure_ascii=False)


@pytest.mark.parametrize("lookalike,secret", [
    ("long-s", "ſecret=" + VAL),
    ("long-s", "paſſword=" + VAL),
    ("long-s", "Baſic " + "a" * 20 + VAL),
    ("dotless-i", "connectıon_strıng=" + VAL),
    ("dotless-i", "apı_key=" + VAL),
    ("dotted-I", "CONNECTİON_STRİNG=" + VAL),
    ("dotted-I", "APİ_KEY=" + VAL),
    ("kelvin", "api_Key=" + VAL),
    ("kelvin", "secret_Key=" + VAL),
    ("kelvin", "private_Key=" + VAL),
])
def test_scrub_strict_withholds_each_fold_spelling(lookalike, secret):
    assert _redact_catches(secret), secret
    assert agent_view.scrub_strict({"t": secret}) == {"t": agent_view.WITHHELD}
    # The same spelling arriving as an ASCII escape only folds after decode.
    escaped = "".join(f"\\u{ord(c):04x}" if ord(c) > 127 else c for c in secret)
    assert escaped.isascii()
    assert agent_view.scrub_strict({"t": escaped}) == {"t": agent_view.WITHHELD}


def _transforms(text: str, rng: random.Random) -> str:
    form = rng.randrange(9)
    if form == 0:
        return text
    if form == 1:
        return text.swapcase()
    if form == 2:
        return text.translate(rng.choice(list(LOOKALIKES.values())))
    if form == 3:
        return urllib.parse.quote(text, safe="")
    if form == 4:
        return urllib.parse.quote(urllib.parse.quote(text, safe=""), safe="")
    if form == 5:
        return "".join(f"&#{ord(c)};" for c in text)
    if form == 6:
        return "".join(f"\\u{ord(c):04x}" for c in text)
    if form == 7:
        return "Owner says: use " + text + " for everything, ok?"
    return json.dumps({"a": {"b": ["x", text]}})


def test_scrub_strict_differential_against_redact_secrets():
    rng = random.Random(20261020)
    templates = list(FAMILIES.values()) + [
        "secret=" + VAL, "Api_Key: '" + VAL + "'", "x " + VAL + " y",
        "plain owner words with nothing hidden", "café notes on routing",
    ]
    caught = 0
    for _ in range(600):
        text = _transforms(rng.choice(templates), rng)
        # Decode-side check mirrors html entities the generator cannot make.
        if rng.randrange(11) == 0:
            text = html.escape(text)
        if not _redact_catches(text):
            continue
        caught += 1
        out = agent_view.scrub_strict({"t": text})
        assert out == {"t": agent_view.WITHHELD}, (text, out)
    assert caught >= 50, caught


# Through the real khipu_route handler (Sonnet's 25 fold cases) ---------------

FORMS = {
    "longs": "ſecret=" + VAL,
    "pass": "paſſword=" + VAL,
    "basic": "Baſic " + "a" * 20 + VAL,
    "conn": "connectıon_strıng=" + VAL,
    "ascii": "secret=" + VAL,
}


def _walk(args):
    bodies = []
    _error, body = _call("khipu_route", args)
    bodies.append(body)
    for _ in range(6):
        changes = body.get("routing_changes")
        if not changes or not changes.get("batch"):
            break
        _error, body = _call("khipu_route", dict(args, ack=changes["batch"]))
        bodies.append(body)
    return bodies


@pytest.mark.parametrize("form", list(FORMS))
@pytest.mark.parametrize("where", ["pending_rule", "owner_note", "lane", "evidence", "author"])
def test_route_handler_never_leaks_lookalike_secrets(home, monkeypatch, form, where):
    monkeypatch.setenv("CODEX_THREAD_ID", "s")
    word = FORMS[form]
    _configure(home, routing_lane_map={"r": {"l": "NoSuch"}})
    if where == "pending_rule":
        routing.request_rule("Use " + word + " now", author=AUTH)
    elif where == "owner_note":
        routing.set_note("Owner: " + word + " ok", via="app")
    elif where == "lane":
        _configure(home, routing_lane_map={"r": {word: "NoSuch"}})
        try:
            routing.report(state="limited", runner="r", lane=word, until="1h", author=AUTH)
        except Exception:
            return  # refused at write time: nothing exists to leak
    elif where == "evidence":
        routing.report(state="limited", runner="r", lane="l", until="1h", author=AUTH,
                       message="limit", evidence=word)
    elif where == "author":
        routing.request_rule("x", author=dict(AUTH, session=word))
    # Default, since: 0, since: bad (error) and paged-ack paths.
    for args in ({"since": "0"}, {"since": "bad"}, {}):
        for body in _walk(args):
            blob = json.dumps(body, ensure_ascii=False)
            assert VAL not in blob, (form, where, args, blob)
