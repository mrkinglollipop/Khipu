"""Routing producer regressions and the investigation's 115 fake privacy controls."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import pytest

from khipu import agent_view, redact, routing, routing_delivery, routing_providers


NOW = datetime(2026, 10, 22, 16, tzinfo=timezone.utc)
STAMP = routing._iso(NOW)
ENCODINGS = ("literal", "percent", "double-percent", "unicode", "entity")


def _encode(text, encoding):
    if encoding == "percent":
        return quote(text, safe="")
    if encoding == "double-percent":
        return quote(quote(text, safe=""), safe="")
    if encoding == "unicode":
        return "".join(r"\u%04x" % ord(c) for c in text)
    if encoding == "entity":
        return "".join(f"&#{ord(c)};" for c in text)
    return text


@pytest.fixture
def routing_world(tmp_path, monkeypatch):
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    data.mkdir()
    accounts = []
    for i, harness in enumerate(("claude", "codex", "grok")):
        folder = home / f".fake-login-{i}"
        folder.mkdir()
        accounts.append(dict(harness=harness, label=f"{harness.title()} main", home=str(folder),
                             label_source="owner"))
    (data / "config.json").write_text(json.dumps(dict(
        usage_accounts=accounts, memory_root=str(tmp_path / "memory"),
        routing_prompt_chars=8000, routing_summary_chars=8000, routing_line_chars=0,
        routing_resend_after=0, routing_resend_after_versions=0)))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_TEST", "1")
    monkeypatch.setenv("USER", "fixtureowner")
    monkeypatch.setenv("LOGNAME", "fixtureowner")
    for account in accounts:
        account.update(key=account["harness"] + "|" + account["home"], state=routing.OPEN,
                       until=None, basis="checked", scope="account", reason=None, models=[],
                       configured=True, witness=dict(source="check", at=STAMP,
                                                     written_by={"kind": "owner"}))
    base = dict(accounts=accounts, holds=[], rules=[], note=None, priority=None, unmatched=[],
                sources={}, definitions={"providers": {}}, forwards={}, proposals={}, causes={},
                priority_ended=[])
    base["effective"] = {"account:" + a["key"]: routing._effective_account(a) for a in accounts}
    monkeypatch.setattr(agent_view, "_saved_view", lambda: base)
    return base


def _push_change(world, monkeypatch, entity, *, label=None):
    body = copy.deepcopy(world)
    previous = copy.deepcopy(world["effective"])
    if label is not None:
        previous[entity] = {"label": label}
        body["accounts"] = [a for a in body["accounts"] if "account:" + a["key"] != entity]
        body["effective"].pop(entity, None)
    else:
        previous.pop(entity, None)
        body["effective"].setdefault(entity, {"fixture": True})
    view = routing._assemble(dict(version=10, epoch="fake-epoch", effective=previous,
                                  folded_at=routing._iso(NOW - timedelta(seconds=30)), changes=[]),
                             body, NOW, 0, {}, True)
    monkeypatch.setattr(agent_view, "_saved_view", lambda: view)
    monkeypatch.setattr(routing, "_prompt_view", lambda *a, **kw: view)
    agent_view.prepare_routing_table(refresh=True)
    output = routing.prompt_push(routing_delivery.Marker(
        dict(version=10, epoch="fake-epoch", full_at=STAMP), session="fake-regression"),
        budget=5, now=NOW)
    return output["text"]


@pytest.mark.parametrize("label", ["Claude fake-login-0x", "Claude fake_login_0x"])
def test_routing_removed_alias_substring_keeps_removal(routing_world, monkeypatch, label):
    key = routing_world["accounts"][0]["key"]
    out = _push_change(routing_world, monkeypatch, "account:" + key, label=label)
    assert agent_view.WITHHELD not in out
    assert "no longer tracked" in out
    assert routing.account_id(key) in out
    assert "fake-login-0" not in out and "fake_login_0" not in out


@pytest.mark.parametrize("label", ["Codex API token", "Codex password", "Codex API_key"])
@pytest.mark.parametrize("state", [routing.UNKNOWN, routing.PROBABLY_OPEN])
def test_routing_label_colon_does_not_become_credential(routing_world, monkeypatch, label, state):
    account = routing_world["accounts"][0]
    account.update(label=label, state=state, until=routing._iso(NOW - timedelta(hours=1)))
    out = _push_change(routing_world, monkeypatch, "account:" + account["key"])
    assert agent_view.WITHHELD not in out
    assert f"[{label}]: {state.replace('_', ' ')}" in out


@pytest.mark.parametrize("label", ["Codex|Grok", "Claude%7CGrok", r"Claude\u007cGrok",
                                   "Claude token: implementation", "Claude API_key: maintenance"])
def test_routing_removed_unsafe_label_uses_safe_id(routing_world, monkeypatch, label):
    key = routing_world["accounts"][0]["key"]
    out = _push_change(routing_world, monkeypatch, "account:" + key, label=label)
    assert agent_view.WITHHELD not in out
    assert routing.account_id(key) in out and "no longer tracked" in out
    assert label not in out


@pytest.mark.parametrize("label,model", [("Example token", "gpt-example"),
                                         ("Example provider", "fake-login-0x")])
def test_routing_provider_fields_project_separately(routing_world, monkeypatch, label, model):
    provider = dict(id="fake-provider", label=label, family="codex", models_allowed=[model],
                    models_never=["gpt-old"], use="on_request")
    assert routing_providers.checked(provider)
    routing_world["definitions"]["providers"]["fake"] = provider
    out = _push_change(routing_world, monkeypatch, "definition:providers")
    assert agent_view.WITHHELD not in out
    assert "models_allowed:" in out and "models_never: [gpt-old]; use: on_request" in out
    assert "fake-login-0" not in out
    if label == "Example token":
        assert "[Example token]:" in out


def test_routing_model_alias_substring_keeps_account_state(routing_world, monkeypatch):
    account = routing_world["accounts"][0]
    account["models"] = [dict(model="fake-login-0x", source="probe", basis="cli",
                              until=routing._iso(NOW + timedelta(hours=1)))]
    out = _push_change(routing_world, monkeypatch, "account:" + account["key"])
    assert agent_view.WITHHELD not in out
    assert "open; checked by Khipu" in out and "limit reached" in out
    assert "fake-login-0" not in out


@pytest.mark.parametrize("words", ["Token: [REDACTED]", "Token: [REDACTED].",
                                   "Token: [REDACTED]:", "'token': '[REDACTED]'",
                                   '"token": "[REDACTED]"'])
def test_routing_canonical_mask_is_not_a_new_secret(routing_world, words):
    assert redact.redact_secrets(words, force=True) == (words, 0)
    assert agent_view.scrub_strict(words) == words


def test_routing_removed_masked_runner_keeps_notice(routing_world, monkeypatch):
    out = _push_change(routing_world, monkeypatch, "runner:Token: [REDACTED]")
    assert "The Token: [REDACTED] job runner folder is no longer read" in out
    assert agent_view.WITHHELD not in out


@pytest.mark.parametrize("words,rendered", [
    ("Token: [REDACTED]", "Token: [REDACTED]"),
    ("'token': '[REDACTED]'", "'token': '[REDACTED]'"),
    ('"token": "[REDACTED]"', "'token': '[REDACTED]'"),
])
@pytest.mark.parametrize("kind", ["rule", "priority"])
def test_routing_masked_pending_words_keep_facts(routing_world, monkeypatch, words, rendered, kind):
    if kind == "rule":
        routing_world["rules"] = [dict(n=1, entity="r-fake", text=words, confirmed=False, pending=True)]
        entity = "rule:r-fake"
    else:
        account = routing_world["accounts"][0]
        routing_world["priority"] = dict(entries=[dict(account=account["key"], label=account["label"],
                                                      role=words)], pending=True, until=None)
        entity = "priority"
    out = _push_change(routing_world, monkeypatch, entity)
    assert agent_view.WITHHELD not in out
    assert rendered in out and "owner has not confirmed" in out


@pytest.mark.parametrize("field", ["label", "model"])
@pytest.mark.parametrize("private", ["api_key=FakePrivateValue123", "codex|private-login-folder",
                                     "prefixfake-login-0suffix"])
def test_routing_private_fields_leave_only_safe_account_facts(routing_world, monkeypatch, field, private):
    account = routing_world["accounts"][0]
    if field == "label":
        account["label"] = private
    else:
        account["models"] = [dict(model=private, source="probe", basis="cli",
                                  until=routing._iso(NOW + timedelta(hours=1)))]
    out = _push_change(routing_world, monkeypatch, "account:" + account["key"])
    assert agent_view.WITHHELD not in out
    assert "open; checked by Khipu" in out
    assert all(private.casefold() not in v.casefold() for v in agent_view._decoded_variants(out))


@pytest.mark.parametrize("words", ["Token: [REDACTED]suffix", "Token: [REDACTED]:ActualPassword123",
                                   "Token: [REDACTED] api_key=FakePrivateValue123",
                                   '"token": "[REDACTED]suffix"', "Token: [redacted]",
                                   "Token: [REDACTED]”ActualPassword123",
                                   "Token: [REDACTED]’ActualPassword123",
                                   "Token: [REDACTED].suffix", "codex|private-login-folder",
                                   "Token: ActualPassword123", "prefixfake-login-0suffix"])
@pytest.mark.parametrize("encoding", ENCODINGS)
def test_routing_mask_and_guard_attacks_stay_withheld(routing_world, words, encoding):
    assert agent_view.scrub_strict(_encode(words, encoding)) == agent_view.WITHHELD


def test_routing_public_choices_use_words_without_exempting_arbitrary_keys(routing_world):
    choices = ["Codex", "Grok"]
    public = "Use " + " or ".join(choices) + " for review."
    assert agent_view.scrub_strict(public) == public
    assert agent_view.scrub_strict("Use Codex|Grok for review.") == agent_view.WITHHELD


@pytest.mark.parametrize("control", range(23))
@pytest.mark.parametrize("encoding", ENCODINGS)
def test_routing_all_115_private_value_controls(routing_world, control, encoding):
    account = routing_world["accounts"][0]
    home = str(Path(account["home"]).parent)
    private = [account["home"], ".fake-login-0", "fake-login-0", home, account["key"],
               "codex|/opt/unknown-private/secret.txt", "x_codex2|/opt/unknown-private/secret.txt",
               home + "/private.txt", "/home/FakePerson/private.txt"]
    tokens = ["sk-" + "Z" * 24, "xai-" + "Z" * 24, "ghp_" + "Z" * 32, "AKIA" + "Z" * 16,
              "xoxb-" + "Z" * 16, "AIza" + "Z" * 32, "ya29." + "Z" * 32,
              "eyJ" + "Z" * 10 + "." + "Z" * 12 + "." + "Z" * 12,
              "Bearer " + "Z" * 20, "Basic " + "Z" * 20, "api_key=" + "Z" * 20,
              "password=" + "Z" * 20, "https://fakeuser:FakePassword@example.invalid",
              "-----BEGIN PRIVATE KEY-----\nFAKEONLY\n-----END PRIVATE KEY-----"]
    payload = (private + tokens)[control]
    out = agent_view.scrub_strict("Fake diagnostic: " + _encode(payload, encoding),
                                  extra_keys=[a["key"] for a in routing_world["accounts"]])
    assert all(payload.casefold() not in v.casefold() for v in agent_view._decoded_variants(out))


# The first value's closing quote is also the next key's opening quote.
# It must remain available even when the first value is already masked.
_ADJACENT_QUOTES = [
    pytest.param(key_quote, shared_quote, value_quote,
                 id="-".join("double" if q == '\"' else "single"
                             for q in (key_quote, shared_quote, value_quote)))
    for key_quote in ('\"', "'")
    for shared_quote in ('\"', "'")
    for value_quote in ('\"', "'")
]
_ADJACENT_KEYS = (
    "apikey", "api_key", "api-key", "secret", "secretkey", "secret_key", "secret-key",
    "accesstoken", "access_token", "access-token", "authtoken", "auth_token", "auth-token",
    "refreshtoken", "refresh_token", "refresh-token", "token", "password", "passwd", "pwd",
    "clientsecret", "client_secret", "client-secret", "privatekey", "private_key", "private-key",
    "accesskey", "access_key", "access-key", "accesskeyid", "accesskey_id", "accesskey-id",
    "access_keyid", "access_key_id", "access_key-id", "access-keyid", "access-key_id", "access-key-id",
    "dsn", "connectionstring", "connection_string", "connection-string", "APP_API_KEY",
)
_ADJACENT_PRIVATE = "hunter2hunter2"


def _adjacent_quoted_secret(key, key_quote, shared_quote, value_quote):
    return (f"{key_quote}token{key_quote}:{shared_quote}[REDACTED]{shared_quote}"
            f"{key}{shared_quote}:{value_quote}{_ADJACENT_PRIVATE}{value_quote}")


@pytest.mark.parametrize("key", _ADJACENT_KEYS)
@pytest.mark.parametrize("key_quote,shared_quote,value_quote", _ADJACENT_QUOTES)
def test_adjacent_quoted_secret_redaction_preserves_quotes_and_is_idempotent(
        key, key_quote, shared_quote, value_quote):
    words = _adjacent_quoted_secret(key, key_quote, shared_quote, value_quote)
    out, count = redact.redact_secrets(words, force=True)
    assert count > 0
    assert _ADJACENT_PRIVATE not in out
    assert out == words.replace(_ADJACENT_PRIVATE, redact.MASK)
    assert redact.redact_secrets(out, force=True) == (out, 0)


@pytest.mark.parametrize("key", _ADJACENT_KEYS)
@pytest.mark.parametrize("key_quote,shared_quote,value_quote", _ADJACENT_QUOTES)
@pytest.mark.parametrize("container", ["str", "dict"])
def test_adjacent_quoted_secret_strict_scrub_withholds_real_value(
        routing_world, key, key_quote, shared_quote, value_quote, container):
    words = _adjacent_quoted_secret(key, key_quote, shared_quote, value_quote)
    payload = words if container == "str" else {"note": words}
    out = agent_view.scrub_strict(payload)
    assert _ADJACENT_PRIVATE not in str(out)
    assert out == (agent_view.WITHHELD if container == "str" else {"note": agent_view.WITHHELD})


@pytest.mark.parametrize("key", ["password", "api_key", "secret", "token"])
@pytest.mark.parametrize("key_quote,shared_quote,value_quote", _ADJACENT_QUOTES)
@pytest.mark.parametrize("field", ["label", "pending_rule"])
def test_adjacent_quoted_secret_routing_push_never_exposes_real_value(
        routing_world, monkeypatch, key, key_quote, shared_quote, value_quote, field):
    words = _adjacent_quoted_secret(key, key_quote, shared_quote, value_quote)
    if field == "label":
        account = routing_world["accounts"][0]
        account["label"] = words
        entity = "account:" + account["key"]
    else:
        routing_world["rules"] = [dict(n=1, entity="r-fake", text=words,
                                       confirmed=False, pending=True)]
        entity = "rule:r-fake"
    out = _push_change(routing_world, monkeypatch, entity)
    assert all(_ADJACENT_PRIVATE not in variant for variant in agent_view._decoded_variants(out))
    if field == "label":
        assert "open; checked by Khipu" in out
    else:
        assert agent_view.WITHHELD in out
