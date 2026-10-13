"""Count how much of a transcript the capture's re-emission rule would drop.

Usage: python3 tests/tools/reemission_count.py <transcript.jsonl>

Read-only. Prints counts only, never transcript content (a transcript can hold
secrets). Applies the same cleaning as session_capture.read_window, streamed
line by line so a 400 MB transcript does not have to fit in memory, and the
same fingerprint rule (sha1 of role + text, first 16 hex chars) over the whole
file as one session.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from khipu import session_capture as sc  # noqa: E402


def _lines(path: Path):
    with path.open("rb") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if isinstance(d, dict):
                yield d


def count(path: Path) -> dict:
    # Codex rollouts: event_msg turns win when the rollout has any.
    has_event_turns = any(
        role in ("user", "assistant") and d.get("type") == "event_msg"
        for d in _lines(path) if not d.get("isMeta") for role, _ in sc._parse_line(d))
    c = {"compact_summaries": 0}
    for role in ("user", "assistant"):
        c.update({f"{role}_msgs": 0, f"{role}_dropped": 0, f"{role}_chars_kept": 0, f"{role}_chars_dropped": 0})
    seen: set[str] = set()
    for d in _lines(path):
        if d.get("isCompactSummary"):
            c["compact_summaries"] += 1
        if d.get("isMeta"):
            continue
        streamed = isinstance(d.get("params"), dict)
        for role, text in sc._parse_line(d):
            if role.startswith("codex:"):
                if has_event_turns:
                    continue
                role = role[6:]
            if role == "user":
                text = sc._clean_user_text(text)
                if not text.strip():
                    continue
                text = sc._machine_user_message(text) or text
            if role not in ("user", "assistant"):
                continue
            c[f"{role}_msgs"] += 1
            fp = sc.text_fingerprint(role, text)
            if not streamed and fp in seen:
                c[f"{role}_dropped"] += 1
                c[f"{role}_chars_dropped"] += len(text)
            else:
                seen.add(fp)
                c[f"{role}_chars_kept"] += len(text)
    return c


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    c = count(Path(argv[1]))
    for role in ("user", "assistant"):
        print(f"{role}: {c[f'{role}_msgs']:,} messages, {c[f'{role}_dropped']:,} would be dropped; "
              f"chars kept {c[f'{role}_chars_kept']:,}, dropped {c[f'{role}_chars_dropped']:,}")
    kept = c["user_chars_kept"] + c["assistant_chars_kept"]
    dropped = c["user_chars_dropped"] + c["assistant_chars_dropped"]
    total = kept + dropped
    print(f"total: chars kept {kept:,}, dropped {dropped:,} ({100 * dropped / total if total else 0:.1f}%)")
    print(f"isCompactSummary lines: {c['compact_summaries']:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
