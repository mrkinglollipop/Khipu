# Routing received-context acceptance

Read `docs/plans/2026-10-20-routing-first-class.md`, sections 4 and 12, first.
This is slice 2b's rig. Cases 7a, 7b and 7c remain **strict** expected failures
owned by slices 8a, 8b and 8c. An unexpected routing delivery is a matrix
contradiction and fails normally, even inside an expected-failure case.

## Run it deliberately

From `packages/cli`, with the repository's Python dependencies on `PYTHONPATH`:

```sh
python3.11 -m pytest -q -rsx -p no:cacheprovider \
  tests/acceptance/routing_harness --run-routing-harness \
  --routing-auth-root /private/tmp/routing-subscription-auth \
  --routing-evidence /private/tmp/routing-received-evidence
```

The `routing_received` marker identifies these tests. Without the explicit
`--run-routing-harness` flag they skip before starting any harness. The normal
`tests/test_routing*.py` oracle runs the rig's hermetic reader/budget tests only.
Do **not** set `KHIPU_LIVE_TESTS=1`.

Provision only the necessary subscription credential files, locally, without
printing their contents:

```text
routing-subscription-auth/
  codex/auth.json
  claude/.credentials.json
```

No credentials are required for hermetic tests or receipt replay. These are
credential-only inputs, not vendor configuration homes. The rig copies the one
credential into a fresh private home, never decodes or logs it, and removes the
copy after the process exits or times out. On Macs where Claude authentication
is only in Keychain, this file-based credential transport is unavailable; the
test skips with that prerequisite named. The rig does not export Keychain data.

The fresh home contains no installed user hooks, imported vendor packs, live
MCP server, project instructions, hub DSN or inherited API keys. Only the routing
hook entry points are wired. They read a synthetic local record; network access
inside those hook processes is forbidden and recorded as a rig failure. There
is no pack install, promotion, live routing write or live hub call.

## Bounds and native readers

Each pytest session starts at most **one scenario per harness**, including any
parent and child needed to measure it. Claude uses Haiku; Codex uses Luna with
low effort. Both parent and child use that tier. No fast tier or paid xAI API
is used. Results, missing prerequisites, launch failures and timeouts are all
cached. There are no retries or automatic model fallbacks. A scenario has a
120-second timeout; timeout kills the entire process group, including children.
Run at most once per slice touching the matrix; inspect saved receipts to fix
the reader instead of buying another run.

Claude runs `claude -p` with a single Haiku child and one `Bash(cat probe.txt)` call. Its four
synthetic rules exceed the start budget together, while each rule fits in a
child block. PreToolUse uses the production installer's delegation matcher.
Codex runs `codex exec` with a single Luna `spawn_agent` child and
one read of the same known file. Both use persistent native transcripts;
`exec --json`, hook lifecycle output, debug logs and assistant answers are not
received-input proof.

The parent must contain a random known hook sentinel in its received context
before its first model step. The child must contain known probe-file contents
in a recorded tool-result input. This also works with Codex versions that
encrypt their native child task messages: the rig does not decrypt them.
Calibration runs in fixture setup, outside the xfail assertions. A malformed
receipt, missing sentinel or incomplete scenario fails; it cannot xfail.

The readers retain input channels and line order. They accept Claude native
user/tool-result messages, Codex persisted `response_item` developer messages
and tool outputs, and Messages-format native input envelopes. Assistant text,
tool-call arguments, ACP UI updates and hook stdout cannot satisfy calibration.
Tool arguments are associated with their returned body only to verify the
T3 provider-switch acknowledgement, never treated as received context.

## Optional clients

T3 and Aegis have no headless binary on the build host. Grok Build is installed,
but a safe isolated native-input capture driver is not provisioned. They skip
with those precise reasons; they are not claimed as verified integrations.
The rig does not guess CLI flags, import a Claude pack into Aegis, inspect a
live home, spend Grok's allowance to find its limit, or use the paid xAI API.

Provision a reviewed local driver with `--routing-t3-adapter`,
`--routing-grok-adapter` or `--routing-aegis-adapter` when that client's native
headless transport is available. A driver is an executable called once with
`HARNESS_BINARY EVIDENCE_ROOT`. It receives the isolated environment and
`ROUTING_RIG_SENTINEL` / `ROUTING_RIG_CHILD_SENTINEL`. It must use the cheapest
non-fast subscription model, one minimal scenario, no fallback or retries,
and native received-input capture. Aegis must use its own pack. This driver
contract is a provisioning requirement, not runtime enforcement of a model's
pricing or a script's internal run count.

It writes `receipts.json` under its evidence root, pointing only within that
root to unmodified native received transcripts:

```json
{
  "parents": [{"format": "claude", "path": "parent.jsonl", "channel": "user"}],
  "children": [{"format": "codex", "path": "child.jsonl", "channel": "user"}]
}
```

Formats are `claude`, `codex` and `messages`. Every parent and child must pass
its own sentinel check. For pull-only clients, a sentinel in a native received
MCP result is valid (`channel: "tool"`); it need not arrive before the first
model step. A T3 driver records two parent attachments in order (Claude Code,
then Codex), both with the same synthetic `t3:` session id. The second must
acknowledge the first's batch. It also starts a Codex child with no Khipu hooks
and records the parent's received doctor result naming those missing hooks.
Missing transport evidence is a rig error, not a future-feature xfail.

## Replay without model calls

```sh
python3.11 -m pytest -q -rsx -p no:cacheprovider \
  tests/acceptance/routing_harness --run-routing-harness \
  --routing-replay-evidence /private/tmp/routing-received-evidence
```

Native Claude/Codex replay reads `run.json`, `exit.json` and the original native
transcripts. No harness binary, credential or model is used. Optional client
drivers must provide their own native replay capability; an absent saved
scenario is named as skipped. Keep all evidence private and outside Git. Raw
rollouts contain native metadata even though the test inputs are synthetic.
