// What the CLI reports for the Cursor and Antigravity cards (slice G), shared by
// the tests and by the throwaway layout page. Fictional paths and dates.
import type { AntigravityProject } from "../harnessFunctions";
import type { HarnessLiveness, RecallProbeStatus, StatusRow, T3Health } from "../IntegrationsPanel";

export const CURSOR_ROW: StatusRow = {
  harness: "cursor",
  detected: true,
  mcp: true,
  hook_stop: true,
  hook_precompact: true,
  hook_sessionstart: true,
  prompt_recall: "installed",
  delegation_push: "installed",
  recall_rule: "project_scoped",
  installed: true,
  proof: {
    mcp: "hermetic",
    capture: "real-run 2026-10-27",
    session_recall: "real-run 2026-10-27",
    prompt_recall: "not-seen 2026-10-27",
    delegation_push: "unproven",
  },
};

export const AG_PROOF = {
  capture: "hermetic",
  start_block: "unproven",
  prompt_recall: "unproven",
  mcp: "hermetic",
  workspace_files: "unproven",
};

export const AG_ABSENT: StatusRow = {
  harness: "antigravity",
  detected: false,
  mcp: false,
  hook_stop: false,
  hook_precompact: false,
  recall_rule: "missing",
  installed: false,
  proof: AG_PROOF,
};

export const AG_ON_MAC: StatusRow = {
  ...AG_ABSENT,
  detected: true,
  mcp: true,
  hook_stop: true,
  hook_sessionstart: true,
  hook_prompt_recall: true,
  recall_rule: "installed",
  extract: "installed",
  installed: true,
};

export const CURSOR_LIVENESS: HarnessLiveness = { ok: true, seen: true, captures: 40, last_captured_age_s: 29 };

export const CURSOR_PROBE: RecallProbeStatus = {
  ok: true,
  harnesses: { cursor: { ok: true, status: "ok", ts: "2026-10-27T13:00:00Z", seconds: 9.8, age_seconds: 3 * 3600 } },
};

export const KHIPU_PROJECT: AntigravityProject = {
  project: "/Users/me/code/Khipu",
  exists: true,
  capture: true,
  mcp: true,
  rule: true,
  prompt_hook: true,
  installed: true,
  added_at: "2026-10-27T15:20:00+00:00",
};

export const T3_PROVIDERS: T3Health = {
  detected: true,
  lookup: { ok: true },
  last_thread_linked_capture_age_s: 720,
  last_thread_linked_capture_harness: "codex",
  thread_linked_captures_today: 14,
  warnings: [],
  providers: [
    {
      id: "cursor",
      driver: "cursor",
      name: "Cursor",
      hooks: "partial",
      functions: { capture: "installed", start_block: "installed", prompt_recall: "installed", delegation: "missing", mcp: "installed" },
    },
    {
      id: "antigravity",
      driver: "antigravity",
      name: "Antigravity",
      hooks: "installed",
      functions: { capture: "installed", start_block: "unsupported", prompt_recall: "unsupported", delegation: "unsupported", mcp: "installed" },
      projects: { registered: 1, installed: 1 },
      runtime: { version: "1.3.0", checked_version: "1.3.0", recheck: false },
    },
  ],
  notes: [
    "T3 runs Antigravity without Khipu's start block (its runtime ignores injected context); routing reaches it through `khipu_route` and the brief",
  ],
};
