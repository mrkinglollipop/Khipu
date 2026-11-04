"""Finish a queued owner action against the test's in-process fake providers."""
from khipu import owner_background


def finish_owner_job(answer):
    ok, result = answer
    if result.get("started"):
        assert ok and result["applied"] is False
        return owner_background.run(result["request"])
    return answer
