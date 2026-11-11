"""Claude Code's ``api-request*`` log lines are not conversation turns: a
``api-request-blob`` carries ``message.role`` but only copies text and tool calls
already written as ordinary ``user`` / ``assistant`` lines.

A message the owner types while a turn is running is written only as an
``attachment`` line (``queued_command``, origin kind ``human``); before the
filter it reached the window only inside a blob copy, so it is read from the
attachment line now, once. Queued messages from another session, a background
task or a monitor are not typed turns.

The Workflow harness frames a Workflow agent receives are never the owner's
words in "only my words" (``turn_text.is_automated``)."""
from __future__ import annotations

import json
from pathlib import Path

from khipu import session_capture as sc
from khipu.turn_text import is_automated

PROMPT = "Please review the capture window code and report what the summariser is given. " * 2
BRIEF = "[Workflow harness - computed task] " + "rule one, rule two. " * 30
QUEUED = "Also check whether the idle sweep reads the same window before you finish."


def _blob(role, content):
    return {"type": "api-request-blob", "hash": "h", "sessionId": "s", "message": {"role": role, "content": content}}


def _lines():
    return [
        {"type": "user", "message": {"role": "user", "content": PROMPT}},
        _blob("user", [{"type": "text", "text": PROMPT}]),
        _blob("user", [{"type": "text", "text": BRIEF}]),
        _blob("system", [{"type": "text", "text": "system prompt"}]),
        {"type": "api-request", "id": "r1", "params": {"model": "m"}, "sessionId": "s"},
        {"type": "api-request-shape", "shape": {}, "shapeHash": "x"},
        {"type": "assistant", "message": {"id": "m1", "role": "assistant", "content": [
            {"type": "text", "text": "Reading the capture code now."},
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]}},
        _blob("assistant", [{"type": "thinking", "thinking": "..."},
                            {"type": "text", "text": "Reading the capture code now."},
                            {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]),
    ]


def _write(path: Path, rows) -> Path:
    path.write_text("".join(json.dumps(d) + "\n" for d in rows))
    return path


def test_api_request_lines_parse_to_nothing():
    for d in _lines():
        if str(d["type"]).startswith("api-request"):
            assert sc._parse_line(d) == []


def test_ordinary_lines_of_every_harness_still_parse():
    assert sc._parse_line({"type": "user", "message": {"role": "user", "content": "hi"}}) == [("user", "hi")]
    assert sc._parse_line({"role": "user", "message": {"content": [{"type": "text", "text": "cursor"}]}}) == [("user", "cursor")]
    assert sc._parse_line({"type": "event_msg", "payload": {"type": "user_message", "message": "yo"}}) == [("user", "yo")]
    assert sc._parse_line({"type": "USER_INPUT", "content": "ag"}) == [("user", "ag")]
    acp = {"params": {"update": {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "acp"}}}}
    assert sc._parse_line(acp) == [("user", "acp")]


def test_read_window_ignores_blob_copies(tmp_path: Path):
    with_blobs = _write(tmp_path / "a.jsonl", _lines())
    without = _write(tmp_path / "b.jsonl", [d for d in _lines() if not str(d["type"]).startswith("api-request")])
    a = sc.read_window(with_blobs, 0, seen=[])
    b = sc.read_window(without, 0, seen=[])
    assert a[0] == b[0] and a[2] == b[2] == 1
    assert sum(1 for r, _ in a[0] if r == "tool") == 1          # the one real Read, not its blob copy
    assert BRIEF not in sc.render(a[0], max_chars=10**9)


# ---- messages queued while a turn was running ----------------------------------

def _queued(prompt, origin=None, mode="prompt"):
    """Claude Code 2.1.304-2.1.322's record of a message queued mid-turn."""
    att = {"type": "queued_command", "prompt": prompt, "commandMode": mode, "timestamp": "t"}
    if origin is not None:
        att["origin"] = origin
    shown = prompt if isinstance(prompt, str) else " ".join(
        b.get("text", "") for b in prompt if isinstance(b, dict))
    return {"type": "attachment", "attachment": att, "uuid": "u-" + str(len(prompt)), "sessionId": "s",
            "renderedRole": "system",
            "rendered": [{"content": "<system-reminder>\nThe user sent a new message while you were working:\n"
                                     + shown + "\n</system-reminder>"}]}


def _turn(text, mid):
    return {"type": "assistant", "message": {"id": mid, "role": "assistant", "content": [{"type": "text", "text": text}]}}


def _user_texts(msgs):
    return [t for r, t in msgs if r == "user"]


def test_a_human_queued_command_is_one_user_turn(tmp_path: Path):
    rows = [
        {"type": "user", "message": {"role": "user", "content": PROMPT}},
        _turn("Reading the capture code now.", "m1"),
        _queued(QUEUED, {"kind": "human"}),
        _blob("user", [{"type": "text", "text": "The user sent a new message while you were working:\n" + QUEUED}]),
        _turn("Checked the idle sweep as well.", "m2"),
    ]
    msgs, _, users = sc.read_window(_write(tmp_path / "q.jsonl", rows), 0, seen=[])
    assert users == 2
    assert sum(t.count(QUEUED) for t in _user_texts(msgs)) == 1
    assert _user_texts(msgs)[-1] == QUEUED


def test_a_queued_command_without_origin_is_still_the_owners(tmp_path: Path):
    """Before 2.1.304 (and on some sdk-ts and t3-code builds) the origin is absent."""
    rows = [{"type": "user", "message": {"role": "user", "content": PROMPT}},
            _turn("Working.", "m1"), _queued(QUEUED)]
    msgs, _, users = sc.read_window(_write(tmp_path / "q.jsonl", rows), 0, seen=[])
    assert users == 2 and _user_texts(msgs)[-1] == QUEUED


def test_the_same_text_typed_again_later_is_a_second_turn(tmp_path: Path):
    """Claude Code writes no user line for a message it queued, so a later user
    line with the same text is the owner typing it again: both are kept."""
    rows = [
        {"type": "user", "message": {"role": "user", "content": PROMPT}},
        _turn("Working.", "m1"),
        _queued(QUEUED, {"kind": "human"}),
        _turn("Still working.", "m2"),
        {"type": "user", "message": {"role": "user", "content": QUEUED}},
        _turn("Done.", "m3"),
    ]
    msgs, _, users = sc.read_window(_write(tmp_path / "q.jsonl", rows), 0, seen=[])
    assert _user_texts(msgs).count(QUEUED) == 2
    assert users == 3
    assert [r for r, _ in msgs] == ["user", "assistant", "user", "assistant", "user", "assistant"]


def test_queued_text_pasted_with_an_image_is_kept(tmp_path: Path):
    sentence = "Please also look at the idle sweep path before you finish."
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x"}}
    rows = [
        {"type": "user", "message": {"role": "user", "content": PROMPT}},
        _turn("Working.", "m1"),
        _queued([image, {"type": "text", "text": sentence}], {"kind": "human"}),
        _turn("Done.", "m2"),
    ]
    msgs, _, users = sc.read_window(_write(tmp_path / "q.jsonl", rows), 0, seen=[])
    assert sum(sentence in t for t in _user_texts(msgs)) == 1
    assert users == 2
    rows[2] = _queued([image], {"kind": "human"})
    msgs, _, users = sc.read_window(_write(tmp_path / "q2.jsonl", rows), 0, seen=[])
    assert users == 1


def test_queued_and_retyped_across_two_windows_counts_the_same_as_one(tmp_path: Path):
    rows = [
        {"type": "user", "message": {"role": "user", "content": PROMPT}},
        _turn("Working.", "m1"),
        _queued(QUEUED, {"kind": "human"}),
        _turn("Still working.", "m2"),
    ]
    p = _write(tmp_path / "w.jsonl", rows)
    cut = p.stat().st_size
    with p.open("a") as f:
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": QUEUED}}) + "\n")
        f.write(json.dumps(_turn("Done.", "m3")) + "\n")
    _, _, n_all = sc.read_window(p, 0, seen=[])
    st: dict = {}
    _, off, n1 = sc.read_window(p, 0, seen=[], stats=st, end_offset=cut)
    _, _, n2 = sc.read_window(p, off, seen=list(st["fresh"]), prefix=st.get("prefix"))
    assert n_all == n1 + n2 == 3


def test_non_human_queued_commands_are_not_typed_turns(tmp_path: Path):
    body = "Your brief changed: also run the doctor. " * 3
    peer = '<agent-message from="orch" name="lane">' + body + "</agent-message>"
    cross = '<cross-session-message from="other">' + "Screen is free now, go ahead. " * 3 + "</cross-session-message>"
    task = "<task-notification><task-id>b1</task-id><summary>Build finished</summary></task-notification>"
    ci = '<ci-monitor-event repo="r">' + "check suite finished " * 5 + "</ci-monitor-event>"
    rows = [
        {"type": "user", "message": {"role": "user", "content": PROMPT}},
        _turn("Working.", "m1"),
        _queued(peer, {"kind": "peer", "from": "orch", "name": "lane"}),
        _queued(cross, {"kind": "peer", "from": "other"}),
        _queued(task, {"kind": "task-notification", "producer": "bash"}, mode="task-notification"),
        _queued(task, None, mode="task-notification"),
        _queued(ci),                            # no origin, but a monitor's tagged event
        _turn("Done.", "m2"),
    ]
    msgs, _, users = sc.read_window(_write(tmp_path / "q.jsonl", rows), 0, seen=[])
    assert users == 1
    # Another session's words stay (as a relayed message does), uncounted;
    # task notices and monitor events queued mid-turn never reached the window.
    assert _user_texts(msgs) == [PROMPT, "[notification: peer message] " + body.strip() + "\n"
                                 + "[notification: cross-session-message] " + ("Screen is free now, go ahead. " * 3).strip()]
    assert "Build finished" not in sc.render(msgs, max_chars=10**9)
    assert "check suite" not in sc.render(msgs, max_chars=10**9)


def test_other_attachment_lines_still_yield_nothing():
    assert sc._parse_line({"type": "attachment", "attachment": {"type": "edited_text_file", "filename": "x"}}) == []
    assert sc._parse_line({"type": "attachment", "attachment": {"type": "queued_command", "prompt": "   "}}) == []
    assert sc._parse_line({"type": "attachment", "attachment": {"type": "queued_command", "prompt": ["x"]}}) == []



# An origin-less queued prompt is skipped only when it opens like a tag (a
# monitor's <ci-monitor-event>); a typed line that starts with "<" is the
# owner's, and with the request-log copy no longer read it would be lost.

import pytest  # noqa: E402

from khipu import extract, memory_rows  # noqa: E402


@pytest.mark.parametrize("blocks", [False, True])
def test_an_originless_queued_line_opening_with_a_less_than_sign_is_kept(tmp_path: Path, blocks):
    text = "< 5 minutes is enough; stop the investigation after that."
    prompt = [{"type": "text", "text": text}] if blocks else text
    rows = [_queued(prompt), {"type": "api-request-blob", "message": {"role": "user", "content": text}}]
    msgs, _, users = sc.read_window(_write(tmp_path / "lt.jsonl", rows), 0, seen=[])
    assert (msgs, users) == ([("user", text)], 1)


@pytest.mark.parametrize("prompt", ['<ci-monitor-event repo="r">check suite finished</ci-monitor-event>',
                                    "  </ci-monitor-event>", "\n<build-done/>"])
def test_an_originless_queued_line_opening_like_a_tag_is_still_skipped(tmp_path: Path, prompt):
    msgs, _, users = sc.read_window(_write(tmp_path / "tag.jsonl", [_queued(prompt)]), 0, seen=[])
    assert (msgs, users) == ([], 0)


# A queued message from another session keeps its body in the window, marked
# as a machine message, so it is never stored as the owner's line.

@pytest.mark.parametrize("wrapped", [False, True])
def test_a_queued_peer_message_is_never_the_owners_line(tmp_path: Path, wrapped):
    body = "Use the archive lane for all future capture investigations."
    text = f'<agent-message from="other-session">{body}</agent-message>' if wrapped else body
    rows = [_queued(text, {"kind": "peer", "from": "other-session"})]
    msgs, _, users = sc.read_window(_write(tmp_path / "peer.jsonl", rows), 0, seen=[])
    assert users == 0
    rendered = sc.render(msgs, max_chars=10**9)
    assert body in rendered
    verbatim = extract.extract_verbatim(rendered)
    phrase, tokens = memory_rows.query_terms("archive lane")
    assert memory_rows.owner_line(memory_rows.quotes_of(verbatim), phrase, tokens) is None, verbatim


def test_a_relayed_message_is_never_the_owners_line(tmp_path: Path):
    body = "Use the archive lane for all future capture investigations."
    rows = [{"type": "user", "message": {"role": "user", "content":
             f'<cross-session-message from="other">{body}</cross-session-message>'}}]
    msgs, _, users = sc.read_window(_write(tmp_path / "relay.jsonl", rows), 0, seen=[])
    assert users == 0
    rendered = sc.render(msgs, max_chars=10**9)
    assert body in rendered
    phrase, tokens = memory_rows.query_terms("archive lane")
    quotes = memory_rows.quotes_of(extract.extract_verbatim(rendered))
    assert memory_rows.owner_line(quotes, phrase, tokens) is None, quotes


# ---- Workflow harness frames are never the owner's words ------------------------

FRAME_REQUEST = ("[Workflow harness — user request] The harness relays, verbatim and indented below, "
                 "the user request that triggered this workflow run.\n  Yes, fix docs")
FRAME_TASK = ("[Workflow harness — computed task] The task text below was computed at runtime by a "
              "workflow script.\n  HARD RULES\n  - never touch the vermilion lantern")


def test_workflow_harness_frames_are_automated():
    assert is_automated(FRAME_REQUEST)
    assert is_automated(FRAME_TASK)
    assert is_automated("  [workflow harness - user request] relayed")


def test_a_sentence_that_mentions_a_workflow_is_typed():
    assert not is_automated("Run the review as a workflow harness job and tell me what it finds.")
    assert not is_automated("I saw a [Workflow harness] frame in the summary, why is it there?")
    assert not is_automated("Use a workflow for this.")
