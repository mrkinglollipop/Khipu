"""A T3 Code hand-over wrapper as it reaches a non-Codex provider, built from the
format in docs/plans/2026-10-07-khipu-t3.md ("Hand-over delivery", incl. the
lines seen on this Mac 2026-10-07): ContextHandoffDelivery opens it,
ContextHandoffBudget adds the thread header, the source range, the
t3_thread_read pointer and the selection summary, then `[Historical ...]` items;
ProviderTurnStartService appends `\\n\\nUser message:\\n` and the typed text."""
from __future__ import annotations

THREAD = "9d1c7a52-4b0e-4f86-a3c1-5f2e8b7d0a14"


def _item(role: str, kind: str, n: int, body: str, status: str = "completed") -> str:
    return (f"[Historical {role}; {kind}; thread={THREAD}; run=run-{n}; item=item-{n}; "
            f"provider-thread=pt-{n}; status={status}]\n{body}")


def handoff_wrapper(typed: str, *, strategy: str = "full_thread_summary") -> str:
    items = [
        _item("user", "message", 1, "Let's wire the Khipu hooks into the second account."),
        _item("assistant", "message", 2, "Linked settings.json to ~/.claude; the hooks run from there."),
        _item("assistant", "command", 3, "$ ls ~/.claude-t3-second\nexit 0\nCLAUDE.md agents commands"),
        _item("user", "message", 4, "Now check the recall block shows up on a prompt."),
        _item("assistant", "message", 5, "Done: SessionStart hook_success, MCP delta adds khipu."),
    ]
    header = "\n".join([
        f"Context handoff ({strategy}):",
        f"Provider context handoff. Thread: {THREAD}. Covered app runs: run-1-run-5.",
        "Source item range: item-1..item-68 (57 older items not shown).",
        f"Recover omitted history using t3_thread_read(threadId=\"{THREAD}\", fromItem=\"item-1\") "
        "before relying on anything not shown below.",
        "Selected 5 intact items; omitted 57 items. Reasoning, todos, diffs and tool results are not included.",
    ])
    return header + "\n\n" + "\n\n".join(items) + "\n\nUser message:\n" + typed
