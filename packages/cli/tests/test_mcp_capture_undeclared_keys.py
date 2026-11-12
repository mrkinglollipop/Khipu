"""khipu_capture accepts only its declared metadata."""
from __future__ import annotations

from unittest import mock

from khipu import mcp_server as srv


def _captured(args: dict) -> dict:
    with mock.patch.object(srv, "_stdio_hook_owns_capture", return_value=False), \
            mock.patch.object(srv, "_capture_mode", return_value="hub"), \
            mock.patch("khipu.capture.capture", return_value=0) as capture, \
            mock.patch.object(srv, "_captured_episode_id", return_value=None):
        srv._tool_capture(args)
    return capture.call_args.args[0]


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
    assert "decision_details" not in payload
