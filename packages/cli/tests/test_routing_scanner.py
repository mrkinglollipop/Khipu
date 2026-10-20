"""Slice 13t: section 10 / acceptance 14, synthetic inputs only.

Interface under test: khipu.routing_scanner.scan_text(path: str, text: str)
returns a list of dicts with file (the supplied path), line (1-based physical
line), account (a nonempty account/provider/lane mention), kind (state or
ordering), and words (a verbatim source excerpt containing the claim cue).
No account configuration, filesystem discovery, hub, or model is required.

Recall is exercised through the existing note parser and render_block, not a
mock renderer. Missing behavior is a strict xfail; unexpected exceptions and
malformed result shapes remain failures. Remove the relevant marker on XPASS.
"""
from __future__ import annotations

import copy
import importlib
import socket
import subprocess

import pytest

from khipu import notes, recall_prompt


MARK = "routing state: superseded by Khipu's record"


class ContractGap(AssertionError):
    """One observed missing contract, rather than a setup/runtime failure."""


def expect(actual, expected, reason):
    if actual != expected:
        raise ContractGap(reason)


scanner_pending = pytest.mark.xfail(
    strict=True, raises=(ImportError, ContractGap), reason="slice 13: scanner contract"
)
recall_pending = pytest.mark.xfail(
    strict=True, raises=ContractGap, reason="slice 13: imported note superseded mark"
)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    attempted = []

    def forbidden(*args, **kwargs):
        attempted.append(True)
        raise AssertionError("scanner tests attempted a connection or subprocess")

    from khipu import db
    monkeypatch.setattr(db, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    yield
    assert not attempted, "production swallowed a forbidden external operation"


def scan(path, text):
    # Only absence of this module is an import gap. Broken dependencies must fail.
    try:
        module = importlib.import_module("khipu.routing_scanner")
    except ImportError as exc:
        if not isinstance(exc, ModuleNotFoundError) or exc.name != "khipu.routing_scanner":
            raise RuntimeError("scanner dependency missing") from exc
        raise
    result = module.scan_text(str(path), text)
    assert isinstance(result, list)
    for claim in result:
        assert isinstance(claim, dict)
        assert {"file", "line", "account", "kind", "words"} <= claim.keys()
        assert claim["file"] == str(path)
        assert type(claim["line"]) is int and 1 <= claim["line"] <= len(text.splitlines())
        assert claim["kind"] in {"state", "ordering"}
        assert isinstance(claim["account"], str) and claim["account"].strip()
        assert isinstance(claim["words"], str) and claim["words"].strip()
        assert claim["words"] in text.splitlines()[claim["line"] - 1]
    return result


def one_claim(path, text, *, account, kind, cue, line=1):
    claims = scan(path, text)
    expect(len(claims), 1, "one claim for this single-account line")
    claim = claims[0]
    expect(claim["line"], line, "physical source line, including blank lines and frontmatter")
    expect(claim["kind"], kind, "state versus ordering claim")
    expect(account.lower() in claim["account"].lower(), True, "identified account/provider/lane")
    expect(cue.lower() in claim["words"].lower(), True, "evidence includes the claim cue")


@pytest.mark.parametrize("state", ["open", "limited", "paused", "held", "tight", "low", "down"])
@pytest.mark.parametrize("qualifier", ["as of 2031-04-12", "with 18% left"])
@scanner_pending
def test_state_word_requires_date_or_level(tmp_path, state, qualifier):
    one_claim(tmp_path / "AGENTS.md", f"Codex is {state} {qualifier}.",
              account="Codex", kind="state", cue=state)


@pytest.mark.parametrize("text,cue", [
    ("Claude first for dispatch.", "first"),
    ("Claude second for dispatch.", "second"),
    ("Claude before other work.", "before"),
    ("Claude after other work.", "after"),
    ("Dispatch order: Claude.", "order"),
    ("Dispatch priority: Claude.", "priority"),
    ("Prefer Claude for dispatch.", "Prefer"),
    ("Claude is the main dispatch account.", "main"),
    ("Claude only for dispatch.", "only"),
    ("Claude is not used for dispatch.", "not used"),
])
@scanner_pending
def test_ordering_needs_neither_date_nor_level(tmp_path, text, cue):
    one_claim(tmp_path / "CLAUDE.md", text, account="Claude", kind="ordering", cue=cue)


@pytest.mark.parametrize("text,account,kind,cue", [
    ("Main account tight until Fri 2031-04-18", "main", "state", "tight"),
    ("Delegation order: second Claude account first", "Claude", "ordering", "first"),
    ("OpenAI is limited as of 2031-04-12.", "OpenAI", "state", "limited"),
    ("The codex lane is down with 0% left.", "codex", "state", "down"),
])
@scanner_pending
def test_live_example_shapes_and_provider_lane_mentions(tmp_path, text, account, kind, cue):
    one_claim(tmp_path / "synthetic-note.md", text, account=account, kind=kind, cue=cue)


@scanner_pending
def test_numbered_account_ranking_without_state_date_or_level(tmp_path):
    text = "# Dispatch ranking\n\n1. Codex\n2. Grok\n3. Claude\n"
    claims = scan(tmp_path / "AGENTS.md", text)
    expect([(c["line"], c["kind"]) for c in claims],
           [(3, "ordering"), (4, "ordering"), (5, "ordering")], "numbered account ranking")
    for claim, account in zip(claims, ["Codex", "Grok", "Claude"]):
        expect(account.lower() in claim["account"].lower(), True, "ranked account identity")


@scanner_pending
def test_original_line_numbers_after_frontmatter_quotes_and_blank_lines(tmp_path):
    text = ("---\nname: Synthetic dispatch note\n---\n\n"
            "> Codex is paused as of 2031-04-12.\n\n"
            "Codex is low with 9% left.\n\nPrefer Grok for dispatch.\n")
    claims = scan(tmp_path / "synthetic-note.md", text)
    expect([(c["line"], c["kind"]) for c in claims],
           [(7, "state"), (9, "ordering")], "quotes do not shift physical line numbers")


CLEAN = [
    pytest.param("", id="empty"),
    pytest.param("The queue is down as of 2031-04-12.", id="state-without-account"),
    pytest.param("Prefer small patches first.", id="ordering-without-account"),
    pytest.param("Codex was measured on 2031-04-12 at 18%.", id="date-level-without-state"),
    *[pytest.param(f"Codex is {state}.", id=f"unqualified-{state}")
      for state in ["open", "limited", "paused", "held", "tight", "low", "down"]],
    pytest.param("| Codex | Good for builds | Weak at narration |", id="capability-row"),
    pytest.param("| Codex | Good for builds | Evidence 2031-04-12 |", id="capability-evidence-date"),
    pytest.param("| Model | Good for | Weak at | Evidence |\n|---|---|---|---|\n"
                 "| Codex | Ordering tasks | Low-level parsing | 2031-04-12 |",
                 id="capability-table-state-like-words"),
    pytest.param("| Model | Good for | Weak at | Evidence |\n|---|---|---|---|\n"
                 "| Codex | Only small patches; first drafts | Long narration | 2031-04-12 |",
                 id="capability-table-ordering-words"),
    pytest.param("> Codex is paused as of 2031-04-12.\n> Prefer Claude first.\n",
                 id="quote-block-state-and-order"),
    pytest.param("History: on 2031-04-12 the operator paused Claude.", id="past-tense-history"),
    pytest.param("History: Codex was down on 2031-04-12.", id="past-state-with-date"),
    pytest.param("History: we preferred Claude first on 2031-04-12.", id="past-ordering"),
    pytest.param("Run codex exec to reach the Codex lane.", id="lane-howto"),
    pytest.param("First run codex exec, then enter the prompt.", id="lane-howto-first"),
    pytest.param("Use --help before starting the Codex lane.", id="lane-howto-before"),
]


@pytest.mark.parametrize("text", CLEAN)
@scanner_pending
def test_false_positive_classes_pass_clean(tmp_path, text):
    expect(scan(tmp_path / "AGENTS.md", text), [], "durable capability/how-to/history is not live routing")


@pytest.mark.parametrize("relative", [
    "global/CLAUDE.md", "global/AGENTS.md", "project/CLAUDE.md",
    "project/AGENTS.md", "memory/synthetic-note.md",
])
@scanner_pending
def test_path_is_evidence_not_a_file_to_read(tmp_path, relative):
    path = tmp_path / "absent" / relative
    assert not path.exists()
    one_claim(path, "Grok is held as of 2031-04-12.", account="Grok", kind="state", cue="held")
    assert not path.exists()


def imported_hit(tmp_path, body):
    path = tmp_path / "synthetic-note.md"
    raw = "---\nname: Synthetic dispatch note\ntype: reference\n---\n" + body
    path.write_text(raw, encoding="utf-8")
    topic = notes._note_topic_dict(path, project="synthetic/project")
    assert topic is not None
    assert topic["body"] == body
    hit = dict(topic, kind="topic", id=topic["slug"], label=topic["title"], snippet=body)
    return path, raw, hit


@pytest.mark.parametrize("body", [
    "Main account tight until Fri 2031-04-18",
    "Delegation order: second Claude account first",
    "Capability notes for routine builds. " * 12 + "\nCodex is low with 9% left.",
], ids=["state", "undated-order", "claim-beyond-snippet"])
@recall_pending
def test_imported_claim_has_superseded_mark_in_prior_work(tmp_path, body):
    path, raw, hit = imported_hit(tmp_path, body)
    before = copy.deepcopy(hit)
    block = recall_prompt.render_block([hit])
    expect(MARK in block, True, "prior-work block must label an imported routing claim")
    assert hit["id"] in block
    assert hit == before, "rendering is advisory, not a mutation of imported notes"
    assert path.read_text(encoding="utf-8") == raw
    assert hit["status"] != "superseded", "routing mark is distinct from topic lifecycle"


@pytest.mark.parametrize("body", CLEAN)
def test_clean_imported_note_has_no_routing_superseded_mark(tmp_path, body):
    path, raw, hit = imported_hit(tmp_path, body)
    before = copy.deepcopy(hit)
    assert MARK not in recall_prompt.render_block([hit])
    assert hit == before
    assert path.read_text(encoding="utf-8") == raw


@recall_pending
def test_recall_mark_belongs_only_to_claim_note(tmp_path):
    _, _, claim = imported_hit(tmp_path, "Prefer Claude for dispatch.")
    clean = dict(claim, id="note:synthetic-capability", body="Codex is good for builds.",
                 snippet="Codex is good for builds.", frontmatter={})
    block = recall_prompt.render_block([clean, claim])
    clean_line = next(line for line in block.splitlines() if clean["id"] in line)
    claim_line = next(line for line in block.splitlines() if claim["id"] in line)
    expect(MARK in claim_line, True, "mark belongs to the routing note's own prior-work line")
    assert MARK not in clean_line
