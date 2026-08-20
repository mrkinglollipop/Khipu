"""Unit tests for khipu.cli — P2a / audit F4 (graph hops symmetry) and F7
(search ordering / per-kind fairness / ILIKE escaping).

Pure-arithmetic / pure-string tests (escaping, fair-share split) need no
database. The query-shape tests connect read-only to the live Khipu Postgres
(same DSN resolution as the CLI) and skip cleanly if it's unreachable —
never a hard failure, never a write.
"""
from __future__ import annotations

import unittest

from khipu.cli import _escape_like, _fair_shares, _graph_query, _search_query, _token_match_sql

# The audit's own counterexample node (ops/notes/p2-audit-2026-08-09.md F4):
# hops=1 returns 91 distinct neighbors, including
# concept:module_skills__shared_predictive_gates_scripts_signals_py__doc and
# concept:smry__skills__shared_predictive_gates_scripts_signals_py — both
# reachable only via an *inbound* edge, which the old outbound-only recursive
# CTE silently dropped once hops >= 2.
COUNTEREXAMPLE_NODE = "module:skills__shared_predictive_gates_scripts_signals_py"

# Generous enough that LIMIT never binds for the counterexample node's 91
# neighbors, so the comparison isn't confounded by truncation.
VERIFY_LIMIT = 500


def _pg_available() -> bool:
    try:
        from khipu.db import connect

        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
    except Exception:
        return False
    return True


PG_AVAILABLE = _pg_available()


class EscapeLikeTest(unittest.TestCase):
    def test_percent_and_underscore_escaped(self) -> None:
        self.assertEqual(_escape_like("50%_off"), "50\\%\\_off")

    def test_backslash_escaped_first(self) -> None:
        # If backslash weren't escaped first, a literal "\%" in user input
        # would become an unescaped wildcard after the second .replace().
        self.assertEqual(_escape_like("a\\b"), "a\\\\b")
        self.assertEqual(_escape_like("50\\%"), "50\\\\\\%")

    def test_plain_text_unchanged(self) -> None:
        self.assertEqual(_escape_like("hello world"), "hello world")

    def test_empty_string(self) -> None:
        self.assertEqual(_escape_like(""), "")


class FairSharesTest(unittest.TestCase):
    def test_even_split(self) -> None:
        self.assertEqual(_fair_shares(9, 3), [3, 3, 3])

    def test_remainder_goes_to_first_shares(self) -> None:
        self.assertEqual(_fair_shares(20, 3), [7, 7, 6])

    def test_smaller_than_n(self) -> None:
        self.assertEqual(_fair_shares(1, 3), [1, 0, 0])
        self.assertEqual(_fair_shares(2, 3), [1, 1, 0])

    def test_zero_or_negative_clamped_to_zero(self) -> None:
        self.assertEqual(_fair_shares(0, 3), [0, 0, 0])
        self.assertEqual(_fair_shares(-5, 3), [0, 0, 0])

    def test_shares_sum_to_total_when_evenly_divisible(self) -> None:
        shares = _fair_shares(30, 3)
        self.assertEqual(sum(shares), 30)


class TokenMatchSqlTest(unittest.TestCase):
    def test_two_tokens_or_and_score(self) -> None:
        where, score = _token_match_sql(("summary",), 2)
        self.assertIn("summary ILIKE %(t0)s", where)
        self.assertIn(" OR ", where)
        self.assertIn("CASE WHEN", score)


@unittest.skipUnless(PG_AVAILABLE, "Postgres unreachable; skipping live query-shape tests")
class GraphHopsSymmetryTest(unittest.TestCase):
    """F4: hops >= 2 must return a superset of hops == 1's neighbors."""

    @staticmethod
    def _hop1_neighbor_ids(edges_result: dict, node_id: str) -> set[str]:
        return {
            (e["dst"] if e["src"] == node_id else e["src"])
            for e in edges_result["edges"]
        }

    def test_hops2_superset_of_hops1_counterexample_node(self) -> None:
        from khipu.db import connect

        with connect() as conn:
            with conn.cursor() as cur:
                hop1 = _graph_query(cur, COUNTEREXAMPLE_NODE, 1, VERIFY_LIMIT)
                hop2 = _graph_query(cur, COUNTEREXAMPLE_NODE, 2, VERIFY_LIMIT)

        hop1_ids = self._hop1_neighbor_ids(hop1, COUNTEREXAMPLE_NODE)
        if not hop1_ids:
            self.skipTest(f"{COUNTEREXAMPLE_NODE} has no hop=1 edges in current data")
        hop2_ids = {row["node_id"] for row in hop2["walk"]}

        missing = hop1_ids - hop2_ids
        self.assertFalse(missing, f"hops=2 dropped hop=1 neighbors: {sorted(missing)}")
        # The audit's specific evidence: these two are reachable only inbound.
        for expected in (
            "concept:module_skills__shared_predictive_gates_scripts_signals_py__doc",
            "concept:smry__skills__shared_predictive_gates_scripts_signals_py",
        ):
            if expected in hop1_ids:
                self.assertIn(expected, hop2_ids)

    def test_hops2_superset_of_hops1_generic_node(self) -> None:
        """Same property on whatever node the live edges table happens to
        have first — guards against the fix being an artifact of one node."""
        from khipu.db import connect

        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT src FROM edges LIMIT 1")
                row = cur.fetchone()
                if row is None:
                    self.skipTest("no edges in database")
                node_id = row[0]
                hop1 = _graph_query(cur, node_id, 1, VERIFY_LIMIT)
                hop2 = _graph_query(cur, node_id, 2, VERIFY_LIMIT)

        hop1_ids = self._hop1_neighbor_ids(hop1, node_id)
        hop2_ids = {row["node_id"] for row in hop2["walk"]}
        self.assertTrue(
            hop1_ids <= hop2_ids,
            f"hops=2 not a superset of hops=1 for {node_id}: missing {hop1_ids - hop2_ids}",
        )


@unittest.skipUnless(PG_AVAILABLE, "Postgres unreachable; skipping live query-shape tests")
class SearchQueryTest(unittest.TestCase):
    """F7: escaping actually reaches the query, and no kind can starve the
    others under a small shared limit."""

    def test_literal_percent_does_not_match_everything(self) -> None:
        from khipu.db import connect

        with connect() as conn:
            with conn.cursor() as cur:
                # A bare "%" ILIKE'd unescaped matches every non-null row in
                # each table; escaped, it should behave like a literal char
                # and (barring real data containing "%") match far fewer.
                literal_percent_hits = _search_query(cur, "%", 999)
                cur.execute("SELECT COUNT(*) FROM topics")
                topic_count = cur.fetchone()[0]
        if topic_count == 0:
            self.skipTest("no topics in database to distinguish escaped vs. wildcard")
        self.assertLess(len(literal_percent_hits), topic_count)

    def test_no_kind_starves_the_others_under_small_limit(self) -> None:
        from khipu.db import connect

        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM topics")
                topics_total = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM episodes")
                episodes_total = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM nodes")
                nodes_total = cur.fetchone()[0]
                if min(topics_total, episodes_total, nodes_total) == 0:
                    self.skipTest("not all three kinds have data to compare fairness against")
                # "e" is broad enough to plausibly match rows in all three
                # kinds on real Khipu data (episode summaries, topic bodies,
                # node names/ids all contain the letter e).
                results = _search_query(cur, "e", 9)
        kinds = {r["kind"] for r in results}
        self.assertGreaterEqual(
            len(kinds), 2, f"expected results from multiple kinds, got only {kinds}"
        )

    def test_multi_token_query_is_not_one_giant_substring(self) -> None:
        from khipu.db import connect

        with connect() as conn:
            with conn.cursor() as cur:
                hits = _search_query(cur, "openbot ingest PR 36", 12)
        snippets = " ".join(
            (r.get("snippet") or r.get("label") or "").lower() for r in hits
        )
        if "openbot" not in snippets:
            self.skipTest("no OpenBot rows in live hub")
        self.assertTrue(
            any(r["kind"] == "episode" for r in hits),
            hits,
        )

    def test_digit_id_graph_uses_episode_topics(self) -> None:
        from khipu.db import connect

        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM episodes WHERE topics IS NOT NULL "
                    "AND jsonb_array_length(topics) > 0 "
                    "ORDER BY id DESC LIMIT 1"
                )
                row = cur.fetchone()
                if row is None:
                    self.skipTest("no episode with topics")
                eid = str(row[0])
                out = _graph_query(cur, eid, 1, 25)
        self.assertIn("episode", out)
        self.assertFalse(out["episode"]["missing"])
        self.assertTrue(out["episode"]["topics"])
        tagged = [e for e in out["edges"] if e.get("type") == "capture_topic"]
        self.assertEqual(len(tagged), len(out["episode"]["topics"]))


if __name__ == "__main__":
    unittest.main()
