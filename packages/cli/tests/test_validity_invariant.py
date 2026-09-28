# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""The invariant that matters most (Phase 2, session B brief): with the
``validity_ranking`` switch off (its default) and no superseded or retracted
decision in the data, every recall surface returns the same rows in the same
order with the same rendered text as before this session. Additive keys are
the only permitted difference. Production today holds zero superseded
decisions, so on production data this session is invisible until someone
supersedes something.

Exercised against a synthetic corpus built WITHOUT the mandatory scenario
suite's decision supersessions (``apply_decision_supersessions=False``) —
the "production today" control.
"""
from __future__ import annotations

from unittest import mock

from khipu import features
from khipu import recall_prompt as rp
from khipu import validity
from tests.fixtures import corpus


def _strip_additive_keys(hits: list[dict]) -> list[dict]:
    """What a hit looked like before this session: no ``validity`` key, and
    an episode's ``status`` (only ever set by ``khipu.validity.annotate``)
    removed too."""
    out = []
    for h in hits:
        item = {k: v for k, v in h.items() if k != "validity"}
        if h.get("kind") == "episode":
            item.pop("status", None)
        out.append(item)
    return out


class TestNoSupersededDataInvariant:
    """Not a unittest.TestCase on purpose — plain pytest functions reading
    more directly as one invariant checked several ways, all against the
    SAME corpus/query pair."""

    QUERY = "what did we approve for the widget-batching endpoint pagination"

    def test_switch_off_every_episode_hit_is_current_with_no_status_key(self, tmp_path):
        with mock.patch.object(features, "enabled", return_value=False), \
                corpus.installed_corpus(tmp_path, apply_decision_supersessions=False):
            result = rp.prior_work_for_prompt(self.QUERY, cwd=None)
        episode_hits = [h for h in result["hits"] if h["kind"] == "episode"]
        assert episode_hits, "expected at least one episode hit"
        for h in episode_hits:
            assert h["validity"]["state"] == "current"
            assert "status" not in h

    def test_switch_off_rendered_block_never_shows_a_validity_marker(self, tmp_path):
        with mock.patch.object(features, "enabled", return_value=False), \
                corpus.installed_corpus(tmp_path, apply_decision_supersessions=False):
            result = rp.prior_work_for_prompt(self.QUERY, cwd=None)
        stripped_hits = _strip_additive_keys(result["hits"])
        # render_block reads only kind/id/ts/project/status/snippet/label —
        # stripping the additive keys must not change what it renders.
        assert rp.render_block(result["hits"]) == rp.render_block(stripped_hits)
        # Topics already showed "status <value>" before this session (R6) —
        # only an EPISODE line growing one would be new behavior.
        for line in rp.render_block(result["hits"]).splitlines():
            if line.startswith("- [episode "):
                assert " status " not in line

    def test_switch_off_hit_order_and_scores_match_ranking_disabled_explicitly(self, tmp_path):
        with corpus.installed_corpus(tmp_path, apply_decision_supersessions=False):
            with_switch_helper = rp.prior_work_for_prompt(self.QUERY, cwd=None)
            # Same corpus, same query, but drive the local lane directly and
            # explicitly skip apply_ranking's own switch check by asserting
            # it is a no-op on this exact row set: for a corpus with nothing
            # superseded, apply_ranking(rows, historical=False) must return
            # the identical list even if the switch were somehow on.
            direct = rp._snapshot_search_hits(self.QUERY, project=None)
        ranked = validity.apply_ranking(list(direct["hits"]), historical=False)
        assert [r.get("score") for r in direct["hits"]] == [r.get("score") for r in ranked]
        assert [(r["kind"], r["id"]) for r in with_switch_helper["hits"]] == [
            (r["kind"], r["id"]) for r in direct["hits"][: len(with_switch_helper["hits"])]
        ]

    def test_hit_ids_dedup_keys_are_unaffected_when_nothing_is_superseded(self, tmp_path):
        with corpus.installed_corpus(tmp_path, apply_decision_supersessions=False):
            result = rp.prior_work_for_prompt(self.QUERY, cwd=None)
        # Every key must be the plain "kind:id" shape — no "@token" suffix,
        # since nothing in this corpus is non-current.
        for key in rp._hit_ids(result["hits"]):
            assert "@" not in key
