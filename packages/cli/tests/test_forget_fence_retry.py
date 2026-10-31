"""A forget that lands during an embedding retry back-off stops the retry.

The fence runs before the first send; the transport retries the same bytes
after a transient failure, so it must re-check before every attempt
(``embed._post_json(recheck=...)``), or a just-forgotten episode goes out on
the retry.
"""
from __future__ import annotations

import json
import os
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from khipu import embed, hub_snapshot, provider_text, tombstones


@pytest.fixture
def registry(tmp_path, monkeypatch):
    reg = tmp_path / "forgotten_sources.json"

    def publish(ids):
        value = {"version": 1, "source_keys": [], "payload_hashes": [],
                 "legacy_identities": [], "episode_ids": ids}
        tmp = reg.with_suffix(".new")
        tmp.write_text(json.dumps(value))
        os.replace(tmp, reg)

    monkeypatch.setattr(tombstones, "registry_path", lambda: reg)
    monkeypatch.setattr(hub_snapshot, "_pending_erasure_ids", lambda *a, **k: set())
    monkeypatch.setattr(provider_text, "episode_ids_of_topics", lambda cur, refs: {r: [42] for r in refs})
    monkeypatch.setattr(provider_text, "episode_ids_of_commitments", lambda cur, refs: {r: [42] for r in refs})
    publish([])
    return publish


def _send(items, publish_on_first_failure, *, recheck):
    sent: list[bytes] = []

    def transport(url, data, headers, timeout):
        sent.append(data)
        if len(sent) == 1:
            publish_on_first_failure()
            raise urllib.error.URLError("fake transient")
        return b"{}"

    spec = SimpleNamespace(provider="fake", dim=1, normalize="none")
    with patch.object(embed, "resolve_spec", return_value=spec), \
            patch.dict(embed._BUILDERS, {"fake": lambda spec, part, it: ("fake://none", {"texts": part}, {})}), \
            patch.object(embed, "_budget_take", return_value=None), \
            patch.object(embed.time, "sleep", return_value=None), \
            patch.object(embed, "_parse_vectors", return_value=[[1.0]] * len(items)):
        try:
            embed.embed_batch([t for _k, _r, t in items], profile="fake", retries=2,
                              transport=transport, recheck=recheck)
            raised = None
        except embed.WithheldDuringSend as exc:
            raised = exc
    return sent, raised


@pytest.mark.parametrize("kind", ["episode", "commitment", "topic"])
def test_a_forget_during_the_back_off_stops_the_retry(registry, kind):
    items = [(kind, "42", "FAKE_SECRET_42")]
    assert provider_text.fence_chunks(None, items) == ["FAKE_SECRET_42"]
    sent, raised = _send(items, lambda: registry([42]),
                         recheck=provider_text.recheck_chunks(None, items))
    assert len(sent) == 1, "the retry after the forget must not send"
    assert isinstance(raised, embed.WithheldDuringSend)
    assert "FAKE_SECRET" not in str(raised)


def test_an_unreadable_list_during_the_back_off_stops_the_retry(registry, tmp_path):
    items = [("episode", "42", "FAKE_SECRET_42")]

    def break_list():
        (tmp_path / "forgotten_sources.json").write_text("{broken")

    sent, raised = _send(items, break_list, recheck=provider_text.recheck_chunks(None, items))
    assert len(sent) == 1
    assert isinstance(raised, embed.WithheldDuringSend)


def test_a_live_batch_still_retries(registry):
    items = [("episode", "42", "FAKE_SECRET_42")]
    sent, raised = _send(items, lambda: None, recheck=provider_text.recheck_chunks(None, items))
    assert raised is None and len(sent) == 2


def test_recheck_texts_follows_fenced_texts(registry):
    check = provider_text.recheck_texts(["x"], [42], "capture-embed")
    assert check() is True
    registry([42])
    assert check() is False


def test_commitment_matching_stops_its_retry_after_a_forget(registry):
    """The real caller: open-commitment matching embeds capture text through
    ``embed_one``; a forget during its back-off must stop the retry too."""
    from khipu import commitments

    class Cur:
        def execute(self, *a):
            pass

        def fetchall(self):
            return []

    sent: list[bytes] = []

    def transport(url, data, headers, timeout):
        sent.append(data)
        if len(sent) == 1:
            registry([42])
            raise urllib.error.URLError("fake transient")
        return b"{}"

    spec = SimpleNamespace(provider="fake", dim=1, normalize="none")
    with patch.object(embed, "_active_profile", return_value="fake"), \
            patch.object(embed, "resolve_spec", return_value=spec), \
            patch.dict(embed._BUILDERS, {"fake": lambda spec, part, it: ("fake://none", {"texts": part}, {})}), \
            patch.object(embed, "_budget_take", return_value=None), \
            patch.object(embed.time, "sleep", return_value=None), \
            patch.object(embed, "_parse_vectors", return_value=[[1.0]]), \
            patch.object(embed, "_transport", side_effect=transport):
        assert commitments._cosine_scores(Cur(), "FAKE_SECRET_42", [7], episode_id=42) == {}
    assert len(sent) == 1, "the retry after the forget must not send"
