"""Unit tests for khipu.cli — P2a / audit F4 (graph hops symmetry) and F7
(search ordering / per-kind fairness / ILIKE escaping).

Pure-arithmetic / pure-string tests (escaping, fair-share split) need no
database. The query-shape tests connect read-only to the live Khipu Postgres
(same DSN resolution as the CLI) and skip cleanly if it's unreachable —
never a hard failure, never a write.
"""
from __future__ import annotations

import argparse
import unittest
from unittest import mock

from khipu.cli import (
    _EPISODE_ILIKE_COLUMNS,
    _episode_rank_text,
    _escape_like,
    _fair_shares,
    _graph_query,
    _id_shaped,
    _search_query,
    _token_match_sql,
)

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


class IdShapedTest(unittest.TestCase):
    def test_colon_is_id_shaped(self) -> None:
        self.assertTrue(_id_shaped("topic:khipu"))

    def test_dunder_is_id_shaped(self) -> None:
        self.assertTrue(_id_shaped("module__foo_bar"))

    def test_plain_words_are_not(self) -> None:
        self.assertFalse(_id_shaped("what did we decide"))

    def test_empty_or_none(self) -> None:
        self.assertFalse(_id_shaped(""))
        self.assertFalse(_id_shaped(None))


class TokenMatchSqlTest(unittest.TestCase):
    def test_two_tokens_or_and_score(self) -> None:
        where, score = _token_match_sql(("summary",), 2)
        self.assertIn("summary ILIKE %(t0)s", where)
        self.assertIn(" OR ", where)
        self.assertIn("CASE WHEN", score)

    def test_episode_columns_include_extract_json(self) -> None:
        blob = " ".join(_EPISODE_ILIKE_COLUMNS)
        for need in (
            "summary",
            "topics::text",
            "decisions::text",
            "preferences::text",
            "people::text",
        ):
            self.assertIn(need, blob)
        where, _score = _token_match_sql(_EPISODE_ILIKE_COLUMNS, 1)
        self.assertIn("topics::text", where)
        self.assertIn("people::text", where)


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

    def test_nodes_excluded_by_default_for_a_non_id_shaped_query(self) -> None:
        from khipu.db import connect

        with connect() as conn:
            with conn.cursor() as cur:
                results = _search_query(cur, "e", 30)
        self.assertNotIn("node", {r["kind"] for r in results})

    def test_kind_node_still_returns_nodes(self) -> None:
        from khipu.db import connect

        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM nodes WHERE id ILIKE %(q)s ESCAPE '\\'",
                            {"q": "%e%"})
                nodes_total = cur.fetchone()[0]
                if nodes_total == 0:
                    self.skipTest("no matching nodes in live hub")
                results = _search_query(cur, "e", 10, kind="node")
        self.assertTrue(results)
        self.assertTrue(all(r["kind"] == "node" for r in results))

    def test_id_shaped_query_reaches_nodes_without_kind(self) -> None:
        from khipu.db import connect

        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM nodes LIMIT 1")
                row = cur.fetchone()
                if row is None:
                    self.skipTest("no nodes in live hub")
                node_id = row[0]
                if not _id_shaped(node_id):
                    self.skipTest(f"sample node id {node_id!r} is not id-shaped")
                results = _search_query(cur, node_id, 10)
        self.assertTrue(any(r["kind"] == "node" for r in results), results)

    def test_kind_filter_restricts_to_one_kind(self) -> None:
        from khipu.db import connect

        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM topics")
                if cur.fetchone()[0] == 0:
                    self.skipTest("no topics in live hub")
                results = _search_query(cur, "e", 10, kind="topic")
        self.assertTrue(all(r["kind"] == "topic" for r in results))

    def test_invalid_kind_raises(self) -> None:
        from khipu.db import connect

        with connect() as conn:
            with conn.cursor() as cur:
                with self.assertRaises(ValueError):
                    _search_query(cur, "e", 10, kind="bogus")

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


class SearchStaleFallbackForwardingTest(unittest.TestCase):
    """fix 7: cmd_search's hub-unreachable fallback must forward project/
    session_id/harness to search_stale_payload, same as the MCP tool."""

    def test_forwards_project_session_id_harness_to_the_snapshot_fallback(self):
        from khipu import cli as climod

        captured = {}

        def fake_stale(query, limit, *, semantic, kind, since, until,
                        project, session_id, harness):
            captured.update(project=project, session_id=session_id, harness=harness)
            return {"query": query, "mode": "literal", "results": [], "filters_dropped": []}

        args = argparse.Namespace(
            query="khipu", limit=10, kind=None, project="acme/widget",
            since=None, until=None, session_id="claude_code:host-1", harness="claude_code",
        )
        with mock.patch("khipu.embed.hybrid_search",
                         side_effect=RuntimeError("connection refused")), \
                mock.patch("khipu.hub_snapshot.hub_connection_failed", return_value=True), \
                mock.patch("khipu.hub_snapshot.search_stale_payload", fake_stale), \
                mock.patch("khipu.query_log.log_query"):
            rc = climod.cmd_search(args)
        self.assertEqual(rc, 0)
        self.assertEqual(captured["project"], "acme/widget")
        self.assertEqual(captured["session_id"], "claude_code:host-1")
        self.assertEqual(captured["harness"], "claude_code")


class EpisodeRankTextDelegatesToEmbedTest(unittest.TestCase):
    """cheap dedup: cli._episode_rank_text calls embed.episode_text rather
    than reimplementing the same assembly."""

    def test_matches_embed_episode_text(self):
        from khipu.embed import episode_text

        row = {"summary": "did a thing", "topics": ["a", "b"], "decisions": ["d1"],
               "preferences": ["p1"], "people": ["Matt"]}
        expected = episode_text(row)
        actual = _episode_rank_text(
            row["summary"], row["topics"], row["decisions"], row["preferences"], row["people"]
        )
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()


class ForgottenEpisodesStayForgottenTest(unittest.TestCase):
    """Audit 2026-09-04: the topic branches of every keyword query excluded
    tombstones (`deleted_at IS NULL`) but the EPISODE branches did not, so an
    episode dropped by `khipu episode --forget` came straight back on the next
    search. Gated on the column, like the rest of the pre-0010 handling."""

    class _Cur:
        def __init__(self, has_deleted_at=True):
            self.has_deleted_at = has_deleted_at
            self.statements: list[str] = []
            self._result: list[tuple] = []

        def execute(self, sql, params=None):
            s = " ".join(sql.split())
            self.statements.append(s)
            if "information_schema.columns" in s:
                cols = ["id", "ts", "summary", "session_id", "scope", "topics",
                        "people", "decisions", "preferences", "project", "harness"]
                if self.has_deleted_at:
                    cols.append("deleted_at")
                self._result = [(c,) for c in cols]
            else:
                self._result = []

        def fetchall(self):
            return list(self._result)

        def fetchone(self):
            return self._result[0] if self._result else None

    def _episode_sql(self, statements):
        return [s for s in statements if "FROM episodes" in s]

    def test_search_query_excludes_tombstoned_episodes(self):
        cur = self._Cur()
        _search_query(cur, "some query", 10, kind="episode")
        sql = self._episode_sql(cur.statements)
        self.assertTrue(sql)
        self.assertIn("WHERE deleted_at IS NULL AND", sql[0])

    def test_literal_candidates_excludes_tombstoned_episodes(self):
        from khipu.cli import _literal_candidates

        cur = self._Cur()
        _literal_candidates(cur, "some query", 10, kind="episode")
        sql = self._episode_sql(cur.statements)
        self.assertTrue(sql)
        self.assertIn("WHERE deleted_at IS NULL AND", sql[0])

    def test_literal_candidates_single_token_path_excludes_them_too(self):
        from khipu.cli import _literal_candidates

        cur = self._Cur()
        _literal_candidates(cur, "%", 10, kind="episode")   # no tokens -> raw-term path
        sql = self._episode_sql(cur.statements)
        self.assertTrue(sql)
        self.assertIn("WHERE deleted_at IS NULL AND", sql[0])

    def test_a_pre_migration_hub_still_queries_cleanly(self):
        cur = self._Cur(has_deleted_at=False)
        _search_query(cur, "some query", 10, kind="episode")
        sql = self._episode_sql(cur.statements)
        self.assertTrue(sql)
        self.assertNotIn("deleted_at", sql[0])


class ConfigFloatSettingsTest(unittest.TestCase):
    """Audit 2026-09-04: `khipu config --set dedup_similarity 0.8` went through
    set_path_setting, which stored an expanduser()'d STRING; float_setting only
    accepts int/float, so it silently kept the default and the knob did nothing."""

    def _run(self, key, value):
        import json

        from khipu.cli import cmd_config

        args = argparse.Namespace(set=[key, value], unset=None, set_gateway_url=None,
                                  set_capture_mode=None)
        with mock.patch("khipu.config.set_float_setting") as m_set, \
                mock.patch("khipu.config.set_path_setting") as m_path, \
                mock.patch("khipu.config.float_setting", return_value=0.8), \
                mock.patch("builtins.print") as m_print:
            rc = cmd_config(args)
        payload = json.loads(m_print.call_args.args[0]) if m_print.call_args else {}
        return rc, payload, m_set, m_path

    def test_a_similarity_knob_is_stored_as_a_float(self):
        rc, payload, m_set, m_path = self._run("dedup_similarity", "0.8")
        self.assertEqual(rc, 0)
        m_set.assert_called_once_with("dedup_similarity", 0.8)
        m_path.assert_not_called()
        self.assertTrue(payload["ok"])

    def test_the_close_similarity_knob_takes_the_same_route(self):
        rc, _payload, m_set, _m_path = self._run("commitment_close_similarity", "0.9")
        self.assertEqual(rc, 0)
        m_set.assert_called_once_with("commitment_close_similarity", 0.9)

    def test_out_of_range_is_rejected(self):
        rc, payload, m_set, _ = self._run("dedup_similarity", "1.5")
        self.assertEqual(rc, 2)
        m_set.assert_not_called()
        self.assertIn("between 0 and 1", payload["error"])

    def test_non_numeric_is_rejected(self):
        rc, payload, m_set, _ = self._run("dedup_similarity", "~/nope")
        self.assertEqual(rc, 2)
        m_set.assert_not_called()
        self.assertIn("must be a number", payload["error"])

    def test_a_path_setting_still_goes_to_set_path_setting(self):
        _rc, _payload, m_set, m_path = self._run("memory_root", "/tmp/x")
        m_set.assert_not_called()
        m_path.assert_called_once_with("memory_root", "/tmp/x")


class IntegrationsProjectForwardingTest(unittest.TestCase):
    """Audit 2026-09-04: --project reached only grok_bot, so
    `khipu integrations verify cursor --project X` silently skipped the
    per-project Cursor stale-rule check it exists for."""

    def test_verify_forwards_project_to_every_harness(self):
        import json

        from khipu.cli import cmd_integrations

        args = argparse.Namespace(harness="cursor", project="acme/widget",
                                  integ_cmd="verify", dry_run=False, no_verify=True)
        with mock.patch("khipu.integrations.verify",
                        return_value={"harness": "cursor", "detected": False}) as m_verify, \
                mock.patch("builtins.print"):
            cmd_integrations(args)
        m_verify.assert_called_once_with("cursor", project="acme/widget")

    def test_status_forwards_project_to_every_harness(self):
        from khipu.cli import cmd_integrations

        args = argparse.Namespace(harness="codex", project="acme/widget",
                                  integ_cmd="status", dry_run=False, no_verify=True)
        with mock.patch("khipu.integrations.status",
                        return_value={"harness": "codex", "detected": False}) as m_status, \
                mock.patch("builtins.print"):
            cmd_integrations(args)
        m_status.assert_called_once_with("codex", project="acme/widget")
