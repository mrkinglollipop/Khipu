"""No key value may reach output, logs, JSON or an exception message on any
`khipu secrets verify` path, whatever characters the stored key holds.

Only the lowest transport is faked (``http.client.HTTPSConnection.connect`` and
``HTTPConnection.connect``), so ``urllib.request.Request`` and the real header
building/encoding run exactly as in production.
"""
from __future__ import annotations

import contextlib
import io
import json
import logging

import pytest

from khipu import cli, modelcheck as mc

MARK = "SECRETMARK9731"
BAD_KEYS = {
    "crlf": f"sk-{MARK}\r\nX-Evil: 1",
    "lf": f"sk-{MARK}\nmore",
    "nul": f"sk-{MARK}\x00tail",
    "cyrillic": f"ключ-{MARK}",
    "emoji": f"key-{MARK}-\U0001F511",
    "space": f"sk {MARK} tail",
    "long": MARK + "A" * 100_000,
}
FIXED = "This key has characters a key can't have. Replace it."


@pytest.fixture
def wire(monkeypatch):
    calls = []

    def fake_connect(self):
        calls.append(self.host)
        raise OSError("fake transport down")

    monkeypatch.setattr("http.client.HTTPSConnection.connect", fake_connect)
    monkeypatch.setattr("http.client.HTTPConnection.connect", fake_connect)
    monkeypatch.setattr(mc.embed_mod, "_budget_take", lambda **kw: None)
    mc.embed_mod._KEY_CACHE.clear()
    monkeypatch.setattr(mc, "_active_embed_profile", lambda: mc.embed_mod.PROFILE_2)
    monkeypatch.setattr(
        mc.models_mod, "synth_settings",
        lambda: {"provider": "local", "endpoint": "http://127.0.0.1:9/v1",
                 "model_id": "m"},
    )
    yield calls
    mc.embed_mod._KEY_CACHE.clear()


def _set_all_keys(monkeypatch, key):
    for name in ("resolve_gemini_key", "resolve_anthropic_key",
                 "resolve_openai_key", "resolve_openrouter_key"):
        monkeypatch.setattr(mc.keychain, name, lambda *a, _k=key, **kw: _k)
    monkeypatch.setattr(mc.keychain, "get_openai_compat_key", lambda: key)


def _no_leak(text, key):
    assert MARK not in text
    assert key not in text
    assert "A" * 40 not in text
    assert "ключ" not in text and "\U0001F511" not in text


@pytest.mark.parametrize("name", sorted(BAD_KEYS))
def test_bad_key_every_check_fixed_message_no_request(name, wire, monkeypatch, caplog):
    key = BAD_KEYS[name]
    _set_all_keys(monkeypatch, key)
    caplog.set_level(logging.DEBUG)
    result = mc.check_model_keys(which="all")
    ids = [c["id"] for c in result["checks"]]
    assert ids == list(mc.CHECK_IDS)
    for c in result["checks"]:
        assert c["ok"] is False, c
        assert c["detail"] == FIXED, c
    assert wire == [], "a bad key must not produce a request"
    _no_leak(json.dumps(result), key)
    _no_leak(caplog.text, key)


@pytest.mark.parametrize("name", sorted(BAD_KEYS))
def test_cli_secrets_verify_json_no_leak(name, wire, monkeypatch, caplog):
    key = BAD_KEYS[name]
    _set_all_keys(monkeypatch, key)
    caplog.set_level(logging.DEBUG)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(["secrets", "verify", "--json"])
    assert rc == 1
    payload = json.loads(out.getvalue())
    assert "error" not in payload
    assert [c["id"] for c in payload["checks"]] == list(mc.CHECK_IDS)
    assert all(c["detail"] == FIXED for c in payload["checks"])
    for text in (out.getvalue(), err.getvalue(), caplog.text):
        _no_leak(text, key)


def test_good_key_still_sends_and_reports_network(wire, monkeypatch):
    _set_all_keys(monkeypatch, "sk-goodkey123")
    result = mc.check_model_keys(which="all")
    assert wire, "a valid key must still make its request"
    for c in result["checks"]:
        assert c["detail"] != FIXED
        assert "goodkey" not in json.dumps(c)


def test_unexpected_error_in_one_check_is_contained(monkeypatch):
    def boom(**kw):
        raise ValueError("Invalid header value b'sk-" + MARK + "'")

    monkeypatch.setattr(mc, "_check_anthropic_models", boom)
    for name in ("_check_gemini_embed", "_check_gemini_generate",
                 "_check_openai_compat_generate", "_check_openai_models",
                 "_check_openrouter_models"):
        monkeypatch.setattr(mc, name, lambda **kw: mc._skip("x", "x"))
    result = mc.check_model_keys(which="all")
    bad = [c for c in result["checks"] if c["id"] == "anthropic_models"]
    assert bad and bad[0]["ok"] is False
    assert MARK not in json.dumps(result)


def test_cli_top_level_error_is_fixed_text(monkeypatch):
    def boom(**kw):
        raise RuntimeError("Authorization: Bearer " + MARK)

    monkeypatch.setattr(mc, "check_model_keys", boom)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(["secrets", "verify", "--json"])
    assert rc != 0
    assert MARK not in out.getvalue() + err.getvalue()
