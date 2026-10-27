"""Native capture boundaries from Astra's 2026-10-26 memory-pipeline audit
(jobs/2026-10-26/astra-memory-pipeline/report.md, findings 5-8): distinct
capture-now notes keep distinct sources, an equal-length rewrite of the
transcript is read again, a genuinely repeated instruction is a new turn, and
a SubagentStop records its parent in the namespaced form the parent's own
episode carries. Every transcript is synthetic."""
from __future__ import annotations

import json

from khipu import session_capture as sc


def row(role, text):
    return {"type": role, "message": {"role": role, "content": text}}


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def fake_hook(monkeypatch, tmp_path):
    from khipu import identity, synth_cli

    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(synth_cli, "session_ids", lambda: set())
    monkeypatch.setattr(sc, "is_helper_session", lambda cwd: False)
    monkeypatch.setattr(sc, "cache_thread", lambda *a: None)
    monkeypatch.setattr(sc, "_relocated_claude_transcript", lambda *a: None)
    monkeypatch.setattr(sc, "ancestor_pids", lambda: [])
    monkeypatch.setattr(sc, "sweep_login_homes", lambda: 0)
    monkeypatch.setattr(identity, "resolve_repo_root", lambda cwd: {"repo_root": None, "project": None})
    monkeypatch.setattr(identity, "observe_checkout", lambda *a: None)


def invoke(path, cwd):
    return sc.hook_main(json.dumps({"session_id": "native-same-session",
                                    "hook_event_name": "SessionEnd",
                                    "transcript_path": str(path), "cwd": str(cwd)}),
                        harness="claude_code")


def jobs():
    return [json.loads(p.read_text()) for p in sc.queued_jobs()]


def test_successive_capture_now_notes_at_one_offset_have_distinct_sources(monkeypatch, tmp_path):
    from khipu import capture_identity as ci

    fake_hook(monkeypatch, tmp_path)
    p = tmp_path / "session.jsonl"
    write(p, [row("user", "initial question " + "a" * 230), row("assistant", "initial answer " + "b" * 230)])
    assert invoke(p, tmp_path)["due"]
    for note in ("Retain the alpha decision from this request.", "Retain the beta decision from this request."):
        sc.request_capture_now("claude_code", "native-same-session", note=note)
        result = invoke(p, tmp_path)
        assert result["due"] and result["reason"] == "requested", result
    notes = [j for j in jobs() if j.get("capture_note")]
    assert len(notes) == 2
    assert notes[0]["capture_note"] != notes[1]["capture_note"]
    identities = [sc.job_source_identity(j) for j in notes]
    assert ci.source_key(identities[0]) != ci.source_key(identities[1]), identities


def test_capture_now_note_source_is_stable_across_a_retry(monkeypatch, tmp_path):
    """The note's source names its request, so a retry after a crash between
    enqueue and the state save queues the same source again (exact dedup)."""
    from khipu import capture_identity as ci

    fake_hook(monkeypatch, tmp_path)
    p = tmp_path / "session.jsonl"
    write(p, [row("user", "initial question " + "a" * 230), row("assistant", "initial answer " + "b" * 230)])
    assert invoke(p, tmp_path)["due"]
    request = sc.request_capture_now("claude_code", "native-same-session", note="Retain the gamma decision.")
    pending = request.read_text()
    saved = sc.load_state("claude_code", "native-same-session")
    assert invoke(p, tmp_path)["due"]
    # The crash: the job is on disk, the state save and the acknowledgement never happened.
    sc.save_state("claude_code", "native-same-session", saved)
    request.write_text(pending)
    assert invoke(p, tmp_path)["due"]
    notes = [j for j in jobs() if j.get("capture_note")]
    assert len(notes) == 2
    assert len({ci.source_key(sc.job_source_identity(j)) for j in notes}) == 1


def test_equal_length_rewrite_of_the_transcript_is_read_again(monkeypatch, tmp_path):
    fake_hook(monkeypatch, tmp_path)
    p = tmp_path / "session.jsonl"
    write(p, [row("user", "old question " + "a" * 230), row("assistant", "old answer " + "b" * 230)])
    assert invoke(p, tmp_path)["due"]
    old_size = p.stat().st_size
    write(p, [row("user", "new question " + "c" * 230), row("assistant", "new answer " + "d" * 230)])
    assert p.stat().st_size == old_size
    result = invoke(p, tmp_path)
    assert result["due"], result
    assert any("new question" in j["transcript"] for j in jobs())


def test_longer_rewrite_keeps_the_content_before_the_old_offset(monkeypatch, tmp_path):
    fake_hook(monkeypatch, tmp_path)
    p = tmp_path / "session.jsonl"
    write(p, [row("user", "old question " + "a" * 230), row("assistant", "old answer " + "b" * 230)])
    assert invoke(p, tmp_path)["due"]
    write(p, [row("user", "replacement question " + "c" * 230),
              row("assistant", "replacement answer " + "d" * 230),
              row("user", "later question " + "e" * 230), row("assistant", "later answer " + "f" * 230)])
    assert invoke(p, tmp_path)["due"]
    text = "".join(j["transcript"] for j in jobs())
    assert "replacement question" in text and "later question" in text


def test_unchanged_transcript_is_not_read_again(monkeypatch, tmp_path):
    fake_hook(monkeypatch, tmp_path)
    p = tmp_path / "session.jsonl"
    write(p, [row("user", "only question " + "a" * 230), row("assistant", "only answer " + "b" * 230)])
    assert invoke(p, tmp_path)["due"]
    result = invoke(p, tmp_path)
    assert not result["due"] and result["new_chars"] == 0, result
    assert len(jobs()) == 1


def test_genuinely_repeated_instruction_is_a_new_turn(monkeypatch, tmp_path):
    fake_hook(monkeypatch, tmp_path)
    p = tmp_path / "session.jsonl"
    instruction = "Review the release patch and fix every correctness issue before merging it."
    write(p, [row("user", instruction), row("assistant", "Initial review finished. " + "a" * 220)])
    assert invoke(p, tmp_path)["due"]
    with p.open("a") as f:
        f.write(json.dumps(row("user", instruction)) + "\n")
        f.write(json.dumps(row("assistant", "A fresh review found additional defects. " + "b" * 220)) + "\n")
    result = invoke(p, tmp_path)
    assert result["new_turns"] == 1, result
    latest = next(j for j in jobs() if "A fresh review" in j["transcript"])
    assert instruction in latest["transcript"]


def test_reemitted_conversation_after_compaction_is_still_dropped(monkeypatch, tmp_path):
    """The fix for repeated instructions must keep dropping a re-emitted copy:
    Claude Code writes the retained conversation back after a compaction."""
    fake_hook(monkeypatch, tmp_path)
    p = tmp_path / "session.jsonl"
    first = [row("user", "Plan the migration for the billing tables carefully."),
             row("assistant", "Here is the migration plan. " + "a" * 220)]
    write(p, first)
    assert invoke(p, tmp_path)["due"]
    compact = {"type": "user", "isCompactSummary": True,
               "message": {"role": "user", "content": "This session is being continued. " + "s" * 80}}
    with p.open("a") as f:
        f.write(json.dumps(compact) + "\n")
        for r in first:
            f.write(json.dumps(r) + "\n")
        f.write(json.dumps(row("user", "Now apply step one of that plan please.")) + "\n")
        f.write(json.dumps(row("assistant", "Step one applied. " + "c" * 220)) + "\n")
    result = invoke(p, tmp_path)
    assert result["new_turns"] == 1, result
    latest = next(j for j in jobs() if "Step one applied" in j["transcript"])
    assert "Here is the migration plan" not in latest["transcript"]


def test_subagentstop_parent_uses_the_parent_episodes_namespaced_id(monkeypatch, tmp_path):
    fake_hook(monkeypatch, tmp_path)
    p = tmp_path / "child.jsonl"
    write(p, [row("user", "Investigate the parent project issue. " + "c" * 230),
              row("assistant", "Finished the delegated investigation. " + "d" * 230)])
    result = sc.hook_main(json.dumps({"session_id": "parent-native-id",
                                      "hook_event_name": "SubagentStop", "agent_id": "child-native-id",
                                      "agent_transcript_path": str(p), "cwd": str(tmp_path)}),
                          harness="claude_code")
    assert result["due"], result
    job = jobs()[0]
    assert job["parent_session_id"] == "claude_code:parent-native-id", job


def test_equal_length_rewrite_inside_a_large_transcript_is_read_again(monkeypatch, tmp_path):
    """Re-check round 1: the content check covers every byte already read,
    not only the head and the bytes just before the offset."""
    fake_hook(monkeypatch, tmp_path)
    p = tmp_path / "large.jsonl"
    rows = [row("user", "Head question " + "a" * 70000), row("assistant", "Head answer " + "b" * 70000),
            row("user", "MIDDLE OLD REQUIREMENT " + "c" * 250), row("assistant", "Middle answer " + "d" * 250),
            row("user", "Tail question " + "e" * 70000), row("assistant", "Tail answer " + "f" * 70000)]
    write(p, rows)
    assert invoke(p, tmp_path)["due"]
    old_size = p.stat().st_size
    rows[2] = row("user", "MIDDLE NEW REQUIREMENT " + "g" * 250)
    write(p, rows)
    assert p.stat().st_size == old_size
    result = invoke(p, tmp_path)
    assert result["due"], result
    assert any("MIDDLE NEW REQUIREMENT" in j["transcript"] for j in jobs())


def test_an_appended_transcript_keeps_its_checkpoint(monkeypatch, tmp_path):
    fake_hook(monkeypatch, tmp_path)
    p = tmp_path / "session.jsonl"
    write(p, [row("user", "first question " + "a" * 230), row("assistant", "first answer " + "b" * 230)])
    assert invoke(p, tmp_path)["due"]
    with p.open("a") as f:
        f.write(json.dumps(row("user", "second question " + "c" * 230)) + "\n")
        f.write(json.dumps(row("assistant", "second answer " + "d" * 230)) + "\n")
    assert invoke(p, tmp_path)["due"]
    later = [j for j in jobs() if "second question" in j["transcript"]]
    assert len(later) == 1 and "first question" not in later[0]["transcript"]


def test_a_repeated_instruction_and_identical_reply_are_new_turns(monkeypatch, tmp_path):
    """Re-check round 1: identical text with new message identities and no
    compaction ahead of it is a new exchange, not a replay."""
    fake_hook(monkeypatch, tmp_path)
    p = tmp_path / "repeated.jsonl"
    instruction = "Review the release patch and fix every correctness issue before merging it."
    answer = "There are no new findings in this review. " + "a" * 220
    initial = [row("user", instruction), row("assistant", answer)]
    initial[0]["uuid"], initial[1]["uuid"] = "turn1", "turn2"
    write(p, initial)
    assert invoke(p, tmp_path)["due"]
    fresh = [row("user", instruction), row("assistant", answer)]
    fresh[0]["uuid"], fresh[1]["uuid"] = "turn3", "turn4"
    with p.open("a") as f:
        for record in fresh:
            f.write(json.dumps(record) + "\n")
    result = invoke(p, tmp_path)
    assert result["new_turns"] == 1, result


def test_a_copied_reply_with_its_captured_message_id_is_dropped(monkeypatch, tmp_path):
    """Without a compaction summary, an assistant copy that carries the API
    message id it was captured with is proof of a copy; the instruction
    beside it in the copied run goes with it."""
    fake_hook(monkeypatch, tmp_path)
    p = tmp_path / "copied.jsonl"
    instruction = "Review the release patch and fix every correctness issue before merging it."
    reply = {"type": "assistant", "message": {"id": "msg_1", "role": "assistant",
                                              "content": "No new findings in this review. " + "a" * 220}}
    write(p, [row("user", instruction), reply])
    assert invoke(p, tmp_path)["due"]
    with p.open("a") as f:
        for record in (row("user", instruction), reply,
                       row("user", "Now ship it please, after a final check of the notes.")):
            f.write(json.dumps(record) + "\n")
    result = invoke(p, tmp_path)
    assert result["new_turns"] == 1 and result["deduped_msgs"] == 2, result
