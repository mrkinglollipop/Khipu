"""Replay graded prompts through automatic push and score what it pushes.

DATASET is JSONL of {n, prompt, cwd}; GRADES is one or more JSONL files of
{n, needs_memory, ideal, pushed_then/pushed_now}, joined on n (see
khipu.recall_eval's graded replay section). It searches whatever replica and
hub this environment's Khipu reads, read-only, with no session id. Keep both
files outside the repo: they hold the owner's prompts.
Run from packages/cli:
  python scripts/recall_replay_grade.py DATASET GRADES [GRADES ...] [--json] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from khipu import recall_eval  # noqa: E402


def main(argv: list[str] | None = None, *, push=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dataset", type=Path)
    parser.add_argument("grades", type=Path, nargs="+")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    parser.add_argument("--limit", type=int, default=0, help="replay only the first N rows")
    args = parser.parse_args(argv)
    rows = recall_eval.load_jsonl(args.dataset)
    if args.limit > 0:
        rows = rows[:args.limit]
    grades = recall_eval.index_grades(
        [g for path in args.grades for g in recall_eval.load_jsonl(path)])
    report = recall_eval.run_graded_replay(rows, grades, push=push)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(recall_eval.format_graded_summary(report["summary"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
