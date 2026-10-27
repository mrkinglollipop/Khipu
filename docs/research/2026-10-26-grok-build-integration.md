# Grok Build integration feasibility

**Verdict — 2026-10-26:** Grok Build can support a local `grok_build` Khipu
pack. It has a separate capture identity, an isolated MCP registration, and a
project `AGENTS.md` recall instruction. The pack refuses to coexist with
Khipu's existing Aegis capture hooks because both use Grok's event stream.

## Evidence

| Finding | Evidence |
| --- | --- |
| Installed client | `grok version` in a made-up home reported `grok 1.0.52 (0920c8169e27)`. |
| Config and MCP scopes | `grok mcp add --help` names user `~/.grok/config.toml` and project `./.grok/config.toml`; package `bin/postinstall.js:221-246` uses `GROK_HOME/config.toml`. |
| Model-visible project instruction | A standalone made-up Git project with `AGENTS.md`, made-up `HOME` and `GROK_HOME`, then `grok --trust --cwd <project> inspect --json`, returned a project `agents_md` instruction. Embedded 1.0.52 documentation at native-binary offset `122411697` names `AGENTS.md` and says its content is injected as a project-instructions message. The same macOS inspect output renders its path as `Agents.md`. |
| Trust requirement | The same offline run wrote `trusted_folders.toml` with a canonical project-path entry. Embedded docs at offset `122624186` say project instructions, hooks, and MCP require folder trust. |
| Capture location | `packages/cli/khipu/session_capture.py:362-371` resolves Grok updates at `~/.grok/sessions/<percent-encoded cwd>/<session id>/updates.jsonl`. |
| Existing collision | `integrations.py:_aegis_blocks()` registers Grok Stop, PreCompact, and SessionEnd hooks with `KHIPU_HARNESS=aegis`; a second hook would capture the same session under two identities. |
| MCP instructions are insufficient proof by themselves | `mcp_server.py:3150-3171` emits `initialize.result.instructions`, but the selected recall route is the client-confirmed `AGENTS.md` channel. Aegis's UserPromptSubmit output is discarded (`recall_prompt.py:1-25`). |

## Design and conflict behavior

`grok_build` registers the MCP server as `khipu_grok_build`, leaving Aegis's
`khipu` table untouched. It owns only its marked Stop, PreCompact, and
SessionEnd hooks, all using `KHIPU_HARNESS=grok_build`, plus the managed block
in the project's documented `AGENTS.md`. Capture explicitly accepts the
Grok transcript fallback under the `grok_build` identity.

The pack honors `GROK_HOME` for its config and trust file; otherwise it uses
the ordinary Grok home. Install refuses before changing config or project
instructions if managed Aegis capture hooks are present, if another Grok Build
capture hook is present, or if the same named MCP table belongs to someone
else. Aegis install applies the reciprocal refusal. Uninstall removes only a
Khipu-owned `khipu_grok_build` command, its recognized hooks, and the exact
managed `AGENTS.md` block; unrelated MCP tables and project text remain.

Grok itself must trust the project. Install deliberately does not grant trust;
status and verify report `untrusted` until the local client trust entry exists.
The configured instruction route is proven offline. Whether a remote provider
model follows the instruction cannot be established without a prohibited live
provider call.

## Use

From the project to be recalled, grant Grok's normal local folder trust, then
run:

Initialize Grok Build normally first so its config exists. If the shared home
already has Khipu's Aegis capture pack, choose which integration owns it:
explicitly uninstall that pack before switching, or use a separately initialized
`GROK_HOME`. Installing both capture packs in one home is refused.

```sh
grok --trust --cwd /path/to/project inspect --json
khipu integrations install grok_build --project /path/to/project --no-verify
khipu integrations status grok_build --project /path/to/project
khipu integrations verify grok_build --project /path/to/project
khipu integrations uninstall grok_build --project /path/to/project
```

`verify` checks the locally enabled MCP and hook extractor plus the trusted,
managed instruction block. Recall is instruction-led: on Grok's first
substantive prompt, the managed rule tells it to call `khipu_status(prompt=…)`
and inspect `prior_work` and named topics; it is not an automatic hook-context
push. It labels provider-model compliance as unasserted.

## Validation

Focused hermetic coverage is in `packages/cli/tests/test_integrations.py`,
`packages/cli/tests/test_session_capture.py`, and
`packages/cli/tests/test_capture_session_resolve.py`: install/uninstall/idempotence,
foreign MCP and `AGENTS.md` preservation, Aegis conflict refusal, untrusted and
modified recall instructions, `GROK_HOME`, explicit capture identity, and Grok
transcript routing, including the installed capture launcher processing synthetic
Grok ACP updates. All 264 portable tests and 30 subtests passed in the final
combined run.

The final combined gate nevertheless exited 1 because an additional local-only
installed-client probe required literal path equality: the installer wrote the
documented `AGENTS.md`, while Grok's `inspect` reported `Agents.md`. On this Mac
the client accepted the generated config, reported `projectTrusted: true`, and
reported the instruction as `fileType: agents_md`, `sizeBytes: 577`. The failed
assertion was the pathname spelling; later assertions in that probe did not run.
Its final pytest output was:

```text
FAILED tests/test_grok_build_offline_local_acceptance.py::test_installed_grok_loads_generated_pack_without_provider_calls
1 failed, 264 passed, 30 subtests passed in 17.53s
```

That local diagnostic is excluded from the portable committed suite. Three
oracle runs were used; no rerun was performed after the final failure. The two
earlier runs exposed and then narrowed an installer-idempotence bug; its strict
regression now passes. The test conftest, including its `TMPDIR` assignment, is
unchanged. No live database, Khipu service, provider call, or real configuration
write was used. Provider-model compliance and case-sensitive-platform instruction
loading remain unverified.
