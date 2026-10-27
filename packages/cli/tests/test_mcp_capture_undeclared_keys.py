"""khipu_capture never lets a tool caller declare a session end."""
from __future__ import annotations

from unittest import mock

import pytest

from khipu import mcp_server as srv


def _captured(args: dict) -> dict:
    with mock.patch.object(srv, "_stdio_hook_owns_capture", return_value=False), \
            mock.patch.object(srv, "_capture_mode", return_value="hub"), \
            mock.patch("khipu.capture.capture", return_value=0) as capture, \
            mock.patch.object(srv, "_captured_episode_id", return_value=None):
        srv._tool_capture(args)
    return capture.call_args.args[0]


@pytest.mark.parametrize("extra", [{"event": "sessionend"}, {"scope": "claude sessionend"}])
def test_a_tool_call_cannot_close_a_named_sessions_items(extra):
    from khipu.commitments import _payload_event, SESSION_END_EVENTS

    payload = _captured({"summary": "done", "session_id": "claude:OTHER", **extra})
    assert "event" not in payload, sorted(payload)
    assert _payload_event(payload) not in SESSION_END_EVENTS, payload.get("scope")
    assert payload["summary"] == "done" and payload["session_id"] == "claude:OTHER"


def test_a_scope_naming_a_session_end_anywhere_is_not_kept():
    payload = _captured({"summary": "done", "session_id": "claude:OTHER", "scope": "sessionend notes"})
    assert "sessionend" not in str(payload.get("scope") or "").lower(), payload.get("scope")


def test_only_declared_keys_pass_and_tool_callers_cannot_assign_or_close():
    payload = _captured({
        "summary": "done", "session_id": "claude:OTHER",
        "parent_session_id": "claude:PARENT", "session_origin": "job", "surprise": 1,
        "open_loops": ["plain", {"text": "Review the report", "owner": "user", "owner_label": "Matt"}],
        "closed_loops": [{"text": "Completed the audit", "commitment_id": 42}],
        "decision_details": [{"text": "Keep it", "by": "user"}],
    })
    for key in ("parent_session_id", "session_origin", "surprise"):
        assert key not in payload, sorted(payload)
    assert payload["open_loops"] == ["plain", {"text": "Review the report"}]
    assert payload["closed_loops"] == [{"text": "Completed the audit"}]
    assert payload["decision_details"] == [{"text": "Keep it"}]
