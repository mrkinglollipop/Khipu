"""The per-prompt replica lane keeps as many candidates as explicit search.

With 16 cosine and 8 keyword rows, a row the cosine leg ranked first, and
that named more of the prompt than any other row, lost the push whenever the
keyword cut fell before it: the rows the keyword leg did return were voted for
twice (their own list and the token-overlap union) and outranked it. The
golden set showed it on 11 of 120 prompts (2026-10-30), where explicit search
ranked the expected memory first and the push left it out.

Everything is faked: no replica, no embedding call. The keyword stand-in
honours the limit it is asked for, the way ``search_snapshot`` does.
"""
from __future__ import annotations

import unittest

from khipu import recall_prompt as rp
from tests.test_prompt_lane_provisional import _lane

PROMPT = "gateway restart rollout failure"
TARGET = "900"


def _episode(rid: str, text: str, **extra) -> dict:
    return {"kind": "episode", "id": rid, "label": text, "snippet": text, **extra}


# Eleven keyword rows naming three of the four prompt words, then the target,
# which names all four but sits twelfth in the keyword search's own order.
KEYWORD_ROWS = [_episode(str(100 + i), f"gateway restart rollout notes {i}") for i in range(11)]
KEYWORD_ROWS.append(_episode(TARGET, "gateway restart rollout failure traced and fixed"))

# The cosine leg puts the target first and three of the keyword rows next.
COSINE_ROWS = [
    {**_episode(TARGET, "gateway restart rollout failure traced and fixed"),
     "chunk_idx": 0, "score": 0.84, "rank_text": "gateway restart rollout failure traced and fixed"},
    *({**_episode(str(100 + i), f"gateway restart rollout notes {i}"), "chunk_idx": 0,
       "score": 0.80 - i / 100, "rank_text": f"gateway restart rollout notes {i}"} for i in range(3)),
]


class ReplicaDepthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.asked: dict[str, int] = {}

    def _keyword(self, _prompt, limit, **_k):
        self.asked["keyword"] = limit
        return [dict(r) for r in KEYWORD_ROWS[:limit]]

    def _cosine(self, _vec, _profile, *, limit, **_k):
        self.asked["cosine"] = limit
        return [dict(r) for r in COSINE_ROWS[:limit]]

    def test_both_legs_ask_for_explicit_search_depth(self) -> None:
        with _lane(keyword=self._keyword, cosine=self._cosine):
            rp._search_hits(PROMPT, cwd=None)
        self.assertEqual(self.asked, {"keyword": rp._REPLICA_DEPTH, "cosine": rp._REPLICA_DEPTH})
        self.assertGreaterEqual(rp._REPLICA_DEPTH, 50)

    def test_cosine_first_row_past_the_old_keyword_cut_is_pushed_first(self) -> None:
        with _lane(keyword=self._keyword, cosine=self._cosine):
            out = rp.prior_work_for_prompt(PROMPT)
        ids = [h["id"] for h in out["hits"]]
        self.assertEqual(ids[0], TARGET)
        self.assertLessEqual(len(ids), rp.TOP_N)

    def test_the_push_still_never_exceeds_top_n(self) -> None:
        many = [_episode(str(300 + i), "gateway restart rollout failure again") for i in range(60)]
        with _lane(keyword=lambda _p, limit, **_k: [dict(r) for r in many[:limit]],
                   cosine=self._cosine):
            out = rp.prior_work_for_prompt(PROMPT)
        self.assertLessEqual(len(out["hits"]), rp.TOP_N)

    def test_a_tail_row_the_graph_returns_keeps_its_evidence_and_votes_once(self) -> None:
        from unittest import mock

        seen: dict = {}

        def candidates(_con, seeds, deadline=None):
            seen["seeds"] = [s["id"] for s in seeds]
            return [{"kind": "episode", "id": "105", "via": {"seed": "topic:x", "relation": "r"},
                     "label": "linked", "snippet": "linked"}], False

        def keep(_con, rows):
            return rows

        captured: dict = {}

        def metadata(_con, fused):
            captured["rows"] = [dict(r) for r in fused]
            return fused

        with _lane(keyword=self._keyword,
                   cosine=lambda *_a, **_k: [{**_episode("105", "gateway restart rollout notes 5"),
                                              "chunk_idx": 0, "score": 0.7, "cosine": 0.7,
                                              "rank_text": "gateway restart rollout notes 5"}],
                   metadata=metadata), \
                mock.patch("khipu.features.enabled", side_effect=lambda n: n == "graph_candidates"), \
                mock.patch("khipu.graph_candidates.replica_candidates", side_effect=candidates), \
                mock.patch("khipu.hub_snapshot.snapshot_filter_live_sources", side_effect=keep):
            rp._snapshot_search_hits(PROMPT, project=None, started=__import__("time").monotonic(),
                                     progress={})
        rows = [r for r in captured["rows"] if r["id"] == "105"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["via"], "topic:x")
        self.assertEqual(rows[0]["cosine"], 0.7)
        self.assertGreaterEqual(rows[0].get("lexical_hits", 0), 1)


if __name__ == "__main__":
    unittest.main()
