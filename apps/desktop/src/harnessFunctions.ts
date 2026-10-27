/**
 * Per-function rows for the Cursor and Antigravity cards, and the T3 provider
 * summaries (docs/plans/2026-10-27-cursor-antigravity-hooks.md, 5.4 and 5.5).
 *
 * Everything here is a pure function of what the CLI already reports:
 * `integrations status` (hook flags and the per-function `proof` table), the
 * capture heartbeat, `t3 antigravity list` and `doctor.t3`. Proof words are the
 * CLI's: "real-run <date>" (seen working in the harness), "not-seen <date>"
 * (installed, and the real runs never saw the event), "hermetic" (tested with
 * fixtures only), "unproven".
 *
 * Amber, never red: a function the harness cannot do, or one the owner has not
 * added, is a limit, not a fault.
 */

export type FnMark = "ok" | "warn" | "err" | "off" | "wait" | "na";
export type FnTone = "ok" | "warn" | "err" | "accent" | "neutral";

export type FunctionRow = {
  key: string;
  mark: FnMark;
  /** The row's words, label first: "Recall at session start · once per session". */
  text: string;
  /** A small outlined tag after the words ("proven Oct 27", "not seen"). */
  tag?: { label: string; tone: "neutral" | "warn" };
};

export type Proof = { kind: "proven" | "not-seen" | "unproven" | "hermetic"; date?: string };

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2026-10-27" or an ISO timestamp as "Oct 27"; null when it is neither. */
export function shortDate(raw: string | undefined | null): string | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(raw ?? "");
  if (!m) return null;
  const month = MONTHS[Number(m[2]) - 1];
  return month ? `${month} ${Number(m[3])}` : null;
}

export function parseProof(raw: string | undefined | null): Proof {
  const text = (raw ?? "").trim();
  if (text.startsWith("real-run")) return { kind: "proven", date: text.slice(8).trim() || undefined };
  if (text.startsWith("not-seen")) return { kind: "not-seen", date: text.slice(8).trim() || undefined };
  if (text === "hermetic") return { kind: "hermetic" };
  return { kind: "unproven" };
}

/** One installed-or-not function with its proof, in the card's words. `who` is
 *  the harness ("Cursor"), `detail` what to say once it is in and proven. */
export function functionRow(
  key: string,
  label: string,
  installed: boolean,
  proofRaw: string | undefined | null,
  opts: { who: string; detail?: string; missing: string; packInstalled: boolean },
): FunctionRow {
  if (!installed) {
    return {
      key,
      // Red only when the whole pack is absent, as the capture line already reads.
      mark: opts.packInstalled ? "warn" : "err",
      text: `${label} · ${opts.missing}`,
    };
  }
  const proof = parseProof(proofRaw);
  const detail = opts.detail ? ` · ${opts.detail}` : "";
  if (proof.kind === "proven") {
    const when = shortDate(proof.date);
    return { key, mark: "ok", text: `${label}${detail}`, tag: { label: when ? `proven ${when}` : "proven", tone: "neutral" } };
  }
  if (proof.kind === "not-seen") {
    return {
      key,
      mark: "off",
      text: `${label} · installed; ${opts.who} hasn't run it in a test yet`,
      tag: { label: "not seen", tone: "warn" },
    };
  }
  if (proof.kind === "unproven") {
    return { key, mark: "wait", text: `${label} · installed, waiting for proof` };
  }
  return { key, mark: "ok", text: `${label}${detail}` };
}

type CursorStatusFields = {
  detected?: boolean;
  mcp?: boolean;
  hook_sessionstart?: boolean;
  prompt_recall?: string;
  delegation_push?: string;
  proof?: Record<string, string>;
};

/** The three Cursor rows the generic card does not already have: recall at
 *  session start, recall on each prompt, routing after a delegation. Null from
 *  an older CLI that reports no `proof` table, so the card keeps its old rows. */
export function cursorFunctionRows(row: CursorStatusFields, packInstalled: boolean): FunctionRow[] | null {
  if (!row.proof) return null;
  const common = { who: "Cursor", packInstalled };
  return [
    functionRow("session_recall", "Recall at session start", row.hook_sessionstart === true, row.proof.session_recall, {
      ...common,
      detail: "once per session",
      missing: "Reinstall adds it",
    }),
    functionRow("prompt_recall", "Recall on each prompt", row.prompt_recall === "installed", row.proof.prompt_recall, {
      ...common,
      missing: "Reinstall adds it",
    }),
    functionRow("delegation", "Routing after a delegation", row.delegation_push === "installed", row.proof.delegation_push, {
      ...common,
      missing: "Reinstall adds it",
    }),
  ];
}

/** `integrations status antigravity` fields the card reads. */
export type AntigravityStatusFields = {
  detected: boolean;
  installed?: boolean;
  mcp: boolean;
  hook_sessionstart?: boolean;
  hook_prompt_recall?: boolean;
  extract?: string;
  proof?: Record<string, string>;
};

/** "On this Mac": the global ~/.gemini files, only when Antigravity is there. */
export function antigravityMacRows(
  row: AntigravityStatusFields,
  captureRow: FunctionRow,
): FunctionRow[] {
  if (!row.detected) {
    return [{
      key: "app",
      mark: "off",
      text: "The Antigravity app isn't installed. Install it and Khipu's hooks go in with one click.",
    }];
  }
  const packInstalled = row.installed === true;
  const proof = row.proof ?? {};
  const common = { who: "Antigravity", packInstalled, missing: "Install adds it" };
  return [
    captureRow,
    functionRow("start_block", "Recall at session start", row.hook_sessionstart === true, proof.start_block, common),
    functionRow("prompt_recall", "Recall on each prompt", row.hook_prompt_recall === true, proof.prompt_recall, common),
    functionRow("mcp", "Memory tools (MCP)", row.mcp, proof.mcp, common),
  ];
}

export type T3FunctionState = "installed" | "missing" | "unsupported";
export type T3Functions = Partial<Record<"capture" | "start_block" | "prompt_recall" | "delegation" | "mcp", string>>;

/** One project `khipu t3 antigravity list` reports. */
export type AntigravityProject = {
  project: string;
  exists?: boolean;
  capture?: boolean;
  mcp?: boolean;
  rule?: boolean;
  prompt_hook?: boolean;
  installed?: boolean;
  added_at?: string;
};

const state = (v: string | undefined): T3FunctionState =>
  v === "installed" || v === "unsupported" ? v : "missing";

/** "Inside T3, per project": capture and memory tools come from the project
 *  files; recall cannot be done by T3's runtime, so it reads as a limit. */
export function antigravityT3Rows(
  functions: T3Functions | undefined,
  projects: AntigravityProject[],
): FunctionRow[] {
  const fromProjects: T3Functions = {
    capture: projects.some((p) => p.capture) ? "installed" : "missing",
    mcp: projects.some((p) => p.mcp) ? "installed" : "missing",
    start_block: "unsupported",
    prompt_recall: "unsupported",
    delegation: "unsupported",
  };
  const f = functions ?? fromProjects;
  const none = projects.length === 0;
  const row = (key: "capture" | "mcp", label: string, from: string): FunctionRow => {
    const s = state(f[key]);
    if (s === "installed") return { key, mark: "ok", text: `${label} · ${from}` };
    if (s === "unsupported") return { key, mark: "na", text: `${label} · T3's Antigravity can't take it` };
    return { key, mark: none ? "off" : "warn", text: `${label} · ${none ? "not added to a project yet" : "missing from a project, Add again repairs it"}` };
  };
  const recallLimited = [f.start_block, f.prompt_recall].every((v) => state(v) === "unsupported");
  return [
    row("capture", "Capture", "from the project's files"),
    row("mcp", "Memory tools (MCP)", "from the project's files"),
    recallLimited
      ? {
          key: "recall",
          mark: "na",
          text: "Recall at start and on each prompt · T3's Antigravity can't take them; the rule file tells it to ask Khipu",
        }
      : {
          key: "recall",
          mark: state(f.prompt_recall) === "installed" ? "ok" : "warn",
          text: "Recall at start and on each prompt",
        },
  ];
}

/** `name · added Oct 27`, with what is wrong with its files when something is. */
export function projectLine(p: AntigravityProject): { name: string; note: string; warn: boolean } {
  const name = p.project.split("/").filter(Boolean).pop() ?? p.project;
  const added = shortDate(p.added_at);
  if (p.exists === false) return { name, note: "folder not found", warn: true };
  if (p.installed === false) return { name, note: "some of Khipu's files are missing, Add again repairs them", warn: true };
  return { name, note: added ? `added ${added}` : "added", warn: false };
}

/** The T3 provider shape the app reads (`doctor.t3.providers[]`). */
export type ProviderLike = {
  id?: string;
  driver?: string;
  name?: string;
  hooks?: string;
  functions?: T3Functions;
  projects?: { registered?: number; installed?: number };
  runtime?: { version?: string | null; checked_version?: string | null; recheck?: boolean };
};

const FEATURES = ["capture", "start_block", "prompt_recall", "delegation", "mcp"] as const;

/** A provider with a per-function picture (Cursor, Antigravity). */
export function hasFunctions(p: ProviderLike): boolean {
  return Boolean(p.functions) && (p.driver === "cursor" || p.driver === "antigravity");
}

/** One row of "T3 providers": the name, "N of M features", what the limit is,
 *  and a tag that is amber for anything short of complete, never red. */
export function providerSummary(p: ProviderLike): {
  name: string;
  line: string;
  tag: { tone: FnTone; label: string };
} {
  const f = p.functions ?? {};
  const have = FEATURES.filter((k) => state(f[k]) === "installed").length;
  const limited = FEATURES.some((k) => state(f[k]) === "unsupported");
  const name = p.name || (p.driver === "antigravity" ? "Antigravity" : "Cursor");
  let line = `${name} · ${have} of ${FEATURES.length} features`;
  if (p.driver === "antigravity") {
    const n = p.projects?.installed ?? 0;
    const where = n === 1 ? "1 project" : `${n} projects`;
    const has = state(f.capture) === "installed" || state(f.mcp) === "installed";
    line += has ? `, capture and tools in ${where}; recall not possible in T3` : "; add a project to start";
  } else {
    const missing = FEATURES.filter((k) => state(f[k]) === "missing").length;
    if (missing > 0 && have > 0) line += ", Reinstall adds the rest";
  }
  if (p.hooks === "missing" || have === 0) {
    return { name, line, tag: { tone: "warn", label: p.driver === "antigravity" ? "Not added" : "No hooks" } };
  }
  if (p.hooks === "partial" || limited || have < FEATURES.length) {
    return { name, line, tag: { tone: "warn", label: "Partial" } };
  }
  return { name, line, tag: { tone: "ok", label: "Complete" } };
}

/** A back-end note without the code quoting, for plain reading. */
export function plainNote(note: string): string {
  return note.replace(/`/g, "");
}
