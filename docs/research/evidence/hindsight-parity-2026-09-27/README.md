# Archived verification evidence

These files preserve the 27 September 2026 investigation. The [verification record](../../hindsight-parity-verification-2026-09-27.md) is the authoritative synthesis; raw reviews are supporting evidence and may contain reviewer recommendations beyond the approved scope. No implementation scope has been approved.

- [Khipu core review](khipu-core-verification.md).
- [Harness contract review](harness-contract-verification.md).
- [Hindsight implementation review](hindsight-verification.md).
- [Exact pytest selectors](selectors.json).
- [Executed pytest output](oracle.txt).
- [Original runner](original-offline-runner.py) and [process result](original-oracle-result.json).

The runner and result are byte-for-byte historical copies. Their temporary absolute paths identify the original invocation; those directories are not required to read this packet. Reproduction requires an isolated Khipu checkout at `9d7d7e09ef63d760ce2fd526b7ed4c1785b6c209`, Python 3.11 with pytest and repository dependencies, and a copy of the runner pointing to the archived selectors. Run from `packages/cli`; preserve its database/socket blockers. The original invocation disabled pytest plugin autoload and bytecode writes, used temporary Khipu state, and enforced a 600-second parent process timeout. Reproduction has not been rerun during packaging.

Absolute source paths in the reviewer notes are historical lookup locations. Use the pinned revisions and source links in the verification record rather than relying on those temporary checkouts. The evidence records source inspection and selected offline contracts, not a live product benchmark.
