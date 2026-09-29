# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""Consumer contract tests (Phase 1, session B). Regenerates each public
payload with today's code against ``tests.fixtures.corpus`` (no database, no
network) and asserts it is a structural SUPERSET of the matching fixture
under ``docs/compat/contracts/`` — every fixture key present with the same
JSON type, list items existentially matched (a scalar list is a subset
check by value, e.g. every documented capability string must still be
present; a list of objects is matched shape-by-shape so different documented
row shapes, like an episode search hit vs a topic one, are checked
independently). A fixture value of ``null`` is a nullable wildcard: it
documents that the key MAY be null, not that this exact call must return
null. Adding a key to a real payload never fails this test; removing,
renaming, or retyping a documented one does.
"""
from __future__ import annotations

import json
from pathlib import Path

from khipu import commitments as co
from khipu import mcp_server as ms
from khipu import recall_prompt as rp
from tests.fixtures import corpus
from tests.test_commitments import _CommitmentsCursor

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACTS_DIR = REPO_ROOT / "docs" / "compat" / "contracts"


def _load(name: str) -> dict:
    return json.loads((CONTRACTS_DIR / name).read_text(encoding="utf-8"))


def _same_scalar_type(actual, expected) -> bool:
    if expected is None:
        return True  # nullable wildcard: documents "may be null", not "is null here"
    if isinstance(expected, bool):
        return isinstance(actual, bool)
    if isinstance(expected, (int, float)):
        return isinstance(actual, (int, float)) and not isinstance(actual, bool)
    if isinstance(expected, str):
        return isinstance(actual, str)
    return type(actual) is type(expected)


def _fits(actual, expected) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and _fits(actual[k], v) for k, v in expected.items()
        )
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return False
        if not expected:
            return True
        if all(not isinstance(v, (dict, list)) for v in expected):
            # A scalar list documents a required SUBSET by value (e.g. every
            # capability string named here must still be advertised).
            return all(v in actual for v in expected)
        # A list of objects documents one or more independent row SHAPES —
        # each must be matched by at least one actual row, not by position.
        return all(any(_fits(item, v) for item in actual) for v in expected)
    return _same_scalar_type(actual, expected)


def _assert_superset(actual, expected, *, label: str) -> None:
    assert _fits(actual, expected), (
        f"{label}: today's payload is not a superset of its contract fixture "
        f"(missing key, mismatched type, or a dropped list item).\n"
        f"--- fixture ---\n{json.dumps(expected, indent=2, default=str)[:2000]}\n"
        f"--- actual ---\n{json.dumps(actual, indent=2, default=str)[:2000]}"
    )


# ---- khipu_search -------------------------------------------------------

def test_khipu_search_result_is_a_superset_of_the_fixture(tmp_path):
    fixture = _load("khipu_search.result.json")
    with corpus.installed_corpus(tmp_path):
        actual = ms._tool_search({"query": "widget-batching endpoint cursor-based pagination"})
    _assert_superset(actual, fixture, label="khipu_search.result.json")


# ---- khipu_status (light: prompt given, full not set) --------------------

def test_khipu_status_light_items_variant(tmp_path):
    fixture = _load("khipu_status.light.json")["variants"]["items"]
    with corpus.installed_corpus(tmp_path):
        actual = ms._tool_status_light({"prompt": "what did we approve for widget-batching"})
    _assert_superset(actual, fixture, label="khipu_status.light.json[items]")


def test_khipu_status_light_empty_variant(tmp_path):
    fixture = _load("khipu_status.light.json")["variants"]["empty"]
    with corpus.installed_corpus(tmp_path, active_profile=False):
        actual = ms._tool_status_light(
            {"prompt": "spreadsheet macro recalculation freeze pane workbook"}
        )
    _assert_superset(actual, fixture, label="khipu_status.light.json[empty]")
    assert actual["prior_work"] == []


def test_khipu_status_light_null_variant(tmp_path):
    fixture = _load("khipu_status.light.json")["variants"]["null"]
    with corpus.installed_corpus(tmp_path):
        actual = ms._tool_status_light({"prompt": "ok"})
    _assert_superset(actual, fixture, label="khipu_status.light.json[null]")
    assert actual["prior_work"] is None


# ---- khipu_status (full) — key names only --------------------------------

def test_khipu_status_full_keys_are_a_superset_of_the_fixture(tmp_path):
    expected_keys = set(_load("khipu_status.full.keys.json"))
    with corpus.installed_corpus(tmp_path):
        actual = ms._tool_status({})
    missing = expected_keys - set(actual.keys())
    assert not missing, f"khipu_status.full.keys.json: missing keys {sorted(missing)}"


# ---- prompt block ----------------------------------------------------------

def test_prompt_block_keeps_its_heading_line_shape_footer_and_budget():
    fixture_text = (CONTRACTS_DIR / "prompt_block.txt").read_text(encoding="utf-8").rstrip("\n")
    hits = [
        {"kind": "episode", "id": "1", "ts": "2026-09-26", "project": "acme/widget",
         "snippet": "Approved: ship the widget-batching endpoint using cursor-based pagination."},
        {"kind": "topic", "id": "widget-batching", "ts": "2026-09-28", "status": "active",
         "snippet": "The widget-batching endpoint batches writes for the acme/widget API."},
        {"kind": "episode", "id": "9", "ts": "2026-09-25", "project": "acme/widget",
         "snippet": "Ran `khipu snapshot refresh --profile pseudo-768` ... FileNotFoundError."},
    ]
    rendered = rp.render_block(hits)
    assert rendered == fixture_text
    assert rendered.startswith(rp._HEADING)
    assert rendered.endswith(rp._FOOTER)
    assert len(rendered) <= rp.BLOCK_CHAR_BUDGET


# ---- khipu_get (episode) ---------------------------------------------------

def test_khipu_get_episode_is_a_superset_of_the_fixture(tmp_path):
    fixture = _load("khipu_get.episode.json")
    with corpus.installed_corpus(tmp_path):
        actual = ms._tool_get({"id": "1"})
    _assert_superset(actual, fixture, label="khipu_get.episode.json")


# ---- khipu_owed -------------------------------------------------------------

def test_khipu_owed_result_is_a_superset_of_the_fixture():
    fixture = _load("khipu_owed.result.json")
    cur = _CommitmentsCursor(migrated=True)
    payload = {"project": "acme/widget", "open_loops": [
        {"text": "Ship the widget-batching rollout once the staging soak finishes.",
         "kind": "followup", "owner": "assistant"},
    ]}
    assert co.open_from_episode(cur, payload, 501) == 1
    actual = co.list_owed(cur, project="acme/widget")[0]
    _assert_superset(actual, fixture, label="khipu_owed.result.json")
