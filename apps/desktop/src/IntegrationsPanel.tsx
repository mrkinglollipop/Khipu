import { useCallback, useEffect, useRef, useState } from "react";
import { Check, Loader2, Minus, RefreshCw, TriangleAlert, X } from "lucide-react";
import { WorkingBanner } from "./WorkingBanner";
import { Callout, Tag } from "./ui";
import { GatewayTokenCard } from "./SettingsControls";
import type { Tone } from "./ui";

/**
 * Harnesses — one card per harness on this Mac (desktop overhaul phase 5,
 * `mocks/main.harness.html`).
 *
 * Thin wrapper over `khipu integrations status|install|verify|uninstall`
 * (packages/cli/khipu/integrations.py), plus two pieces of EVIDENCE the app
 * already reads for Home: the per-harness capture heartbeat
 * (`doctor.capture_liveness`) and the last recorded end-to-end recall probe
 * (`doctor.recall_probe`, written by `khipu doctor --probe` /
 * `integrations verify`).
 *
 * The status tag and the "Verified …" line come from that evidence and
 * nothing else. The pre-overhaul pane derived them from app-local install
 * state, so a pack the CLI had already probed green read "Installed, not yet
 * verified" forever (audit 2026-09-04, phase 5 brief).
 *
 * Nothing here edits a legacy capture hook: Install adds Khipu's entries
 * ALONGSIDE them (dual-write). Removing legacy is a later, soak-gated step
 * and is deliberately not a button on this pane yet.
 */

type HarnessId = "claude_code" | "cursor" | "aegis" | "codex" | "grok_bot";

/** One Claude home in `khipu integrations status claude_code` → `homes[]`
 *  (packages/cli/khipu/integrations.py `_claude_home_status`). Hooks live in
 *  the home's settings.json and the memory tools in its .claude.json, so each
 *  home is installed or not on its own. */
export type ClaudeHomeRow = {
  path: string;
  label: string;
  /** "Claude Code, and T3's “Claude” account" — where Khipu found it. */
  source: string;
  is_default: boolean;
  /** False for a configured folder that is not on disk yet. */
  exists: boolean;
  /** Set when this home's settings.json resolves to another home's file, so
   *  the hooks are that home's. Remove on it only takes out its memory tools. */
  linked_to?: { label: string; path: string } | null;
  /** The config files this row was read from. */
  settings_path?: string;
  mcp_paths?: string[];
  hook_stop?: boolean;
  hook_precompact?: boolean;
  hook_sessionend?: boolean;
  hook_subagentstop?: boolean;
  recall_rule?: string;
  prompt_recall?: string;
  memory_tools_ok: boolean;
  /** All six Khipu hooks present. */
  hooks_ok: boolean;
  /** False when a launcher link this home's config names is broken; absent
   *  from an older CLI, which never said. Install repairs it. */
  launcher_ok?: boolean;
  /** Memory tools + Stop + PreCompact hooks + working launchers. */
  installed: boolean;
  /** Any Khipu entry, including a stale or non-launcher MCP entry. */
  has_khipu?: boolean;
  /** A config file in this home could not be read. */
  error?: string;
};

export type StatusRow = {
  harness: HarnessId;
  detected: boolean;
  mcp: boolean;
  hook_stop: boolean;
  hook_precompact: boolean;
  hook_sessionend?: boolean;
  recall_rule: string;
  /** "legacy" = the harness's existing hooks extract; "installed"/"missing" = Khipu's own (Aegis). */
  extract?: string;
  /** Newest evidence the capture hook actually ran for this harness
   *  (`khipu.integrations._last_beat_at`, ISO 8601) — the auto-verify signal
   *  (docs/plans/2026-09-05-setup-that-cannot-strand-you.md, "Harness
   *  auto-verify"): newer than the moment Install ran means a real session
   *  fired the hook since, so the card can flip to Verified on its own.
   *  `null`/absent means the hook has never run (or, for grok_bot, that
   *  there is no local hook to run). */
  last_beat_at?: string | null;
  /** Claude Code only: one row per Claude home found. The pack-level fields
   *  above are the AND over the homes that exist. */
  homes?: ClaudeHomeRow[];
};

type Probe = {
  ok?: boolean;
  error?: string;
  ms?: number;
  episodes?: number;
  exit?: number;
  chars?: number;
  /** `recall_probe` only (khipu/probe.py run_probe): the end-to-end
   * capture-then-search round trip, plus "skipped" in legacy capture mode. */
  status?: string;
  reason?: string;
  seconds?: number;
  episode_id?: number | null;
};

type VerifyRow = {
  harness: HarnessId;
  detected: boolean;
  ok?: boolean;
  components?: {
    mcp?: Probe;
    hook?: Probe;
    recall?: Probe;
    extract?: Probe;
    /** W6.1: capture a nonce, search for it, forget it. */
    recall_probe?: Probe;
  };
  /** Cursor only: true when this project's .cursor/rules/khipu.mdc no longer
   * matches what this version would write, null when unknowable (no
   * --project was passed) — `note` says which. */
  rule_stale?: boolean | null;
  note?: string;
};

/** One harness's row in `doctor.capture_liveness.harnesses`. */
export type HarnessLiveness = {
  ok?: boolean;
  seen?: boolean;
  reasons?: string[];
  last_dispatch_age_s?: number | null;
  last_captured_age_s?: number | null;
  captures?: number;
  /** D3: turns seen since the last capture was due — "N turns waiting for
   *  capture" on Home's Right now card. */
  pending_turns?: number | null;
  queue_depth?: number | null;
  note?: string | null;
};

export type LivenessPayload = {
  ok?: boolean;
  red?: string[];
  harnesses?: Record<string, HarnessLiveness>;
  /** Summed across harnesses (session_capture.liveness_all()). */
  queue_depth?: number | null;
  /** D3: captures landed today, summed across harnesses. */
  captured_today?: number | null;
};

/** `doctor.recall_probe` — `khipu.probe.status()`: the LAST RECORDED probe,
 *  read-only. One file, one row: `last_probe.harness` says which pack it was
 *  run for, so a card only claims it when the harness matches. */
export type RecallProbeStatus = {
  ok?: boolean;
  reason?: string | null;
  age_seconds?: number | null;
  last_probe?: {
    ts?: string;
    harness?: string;
    ok?: boolean;
    status?: string;
    reason?: string;
    seconds?: number;
    error?: string | null;
  } | null;
  /** Each pack's own last probe (`probe-<harness>.json`), so a card can say
   *  "verified" from its own evidence even when another pack ran last. */
  harnesses?: Record<
    string,
    {
      ok?: boolean;
      status?: string;
      ts?: string;
      seconds?: number;
      error?: string | null;
      reason?: string | null;
      age_seconds?: number | null;
      stale?: boolean;
    }
  >;
};

export const LABEL: Record<HarnessId, string> = {
  claude_code: "Claude Code",
  cursor: "Cursor",
  aegis: "Aegis",
  codex: "Codex",
  grok_bot: "Grok Bot · cloud",
};

const WHERE: Record<HarnessId, string> = {
  claude_code: "~/.claude.json · ~/.claude/settings.json",
  cursor: "~/.cursor/mcp.json · ~/.cursor/hooks.json",
  aegis: "~/.grok/config.toml",
  codex: "~/.codex/config.toml · ~/.codex/hooks.json",
  grok_bot: "per repo: .cursor/mcp.json → your Khipu gateway",
};

/** Check-line marks. `ok`/`err`/`warn` are verdicts; `off` is "nothing to do
 *  here, by design" and must never read as either. */
type Mark = "ok" | "warn" | "err" | "off";

function CheckMark({ mark }: { mark: Mark }) {
  const cls = `ck ${mark}`;
  if (mark === "ok") return <Check size={14} className={cls} aria-hidden />;
  if (mark === "err") return <X size={14} className={cls} aria-hidden />;
  if (mark === "warn") return <TriangleAlert size={14} className={cls} aria-hidden />;
  return <Minus size={14} className={cls} aria-hidden />;
}

function fmtAge(s: number | null | undefined): string {
  if (s == null) return "an unknown time";
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  if (s < 172800) return `${Math.round(s / 3600)} h`;
  return `${Math.round(s / 86400)} days`;
}

/** Round-trip seconds as the mock writes them — one decimal and a space
 *  before the unit ("3.1 s"), not the raw float the probe file carries. */
function fmtSeconds(s: number): string {
  return `${s.toFixed(1)} s`;
}

function firstReason(lv: HarnessLiveness | undefined): string {
  const r = (lv?.reasons ?? [])[0];
  return r ? r.slice(0, 120) : "";
}

type CardStatus = { tone: Tone; label: string };

function unreadableLabel(n: number): string {
  return n === 1 ? "1 home can't be read" : `${n} homes can't be read`;
}

/** The Claude homes that are on disk — the ones the CLI installs into and
 *  ANDs the pack-level fields over. Empty for every other harness. */
export function foundHomes(row: Pick<StatusRow, "homes">): ClaudeHomeRow[] {
  return (row.homes ?? []).filter((h) => h.exists);
}

/** Whether Khipu is in the harness at all. Claude Code asks its homes (the
 *  pack-level fields are the AND, so one missing home would read as nothing
 *  installed); every other harness asks its own config. */
export function installedAnywhere(
  row: Pick<StatusRow, "mcp" | "hook_stop" | "hook_precompact" | "homes">,
): boolean {
  const homes = foundHomes(row);
  if (homes.length > 0) return homes.some((h) => h.installed);
  return row.mcp && row.hook_stop && row.hook_precompact;
}

/** How many found homes have no Khipu — what keeps the card from going green.
 *  A home whose config cannot be read is counted by `homesUnreadable`. */
export function homesNotInstalled(row: Pick<StatusRow, "homes">): number {
  return foundHomes(row).filter((h) => !h.installed && !h.error).length;
}

/** How many found homes have a config file Khipu could not read. */
export function homesUnreadable(row: Pick<StatusRow, "homes">): number {
  return foundHomes(row).filter((h) => h.error).length;
}

/** Whether Khipu has any entry of its own in this home. A linked home's hooks
 *  are the owner's, so only its memory tools count as its own. */
export function hasKhipuEntries(h: ClaudeHomeRow): boolean {
  if (typeof h.has_khipu === "boolean") return h.has_khipu;
  if (h.memory_tools_ok) return true;
  if (h.linked_to) return false;
  return Boolean(h.hook_stop || h.hook_precompact || h.hooks_ok || h.recall_rule === "installed");
}

/** The current user's home folder, from where the default Claude home lives
 *  (`<home>/.claude`). Empty when the CLI listed no default home. */
export function userHomeDir(homes: ClaudeHomeRow[] | undefined): string | undefined {
  const def = (homes ?? []).find((h) => h.is_default);
  if (!def) return undefined;
  const i = def.path.lastIndexOf("/");
  return i > 0 ? def.path.slice(0, i) : undefined;
}

/** A path with the user's home folder as `~`, as the mock writes them. The
 *  webview has no home to ask, so the caller passes the one the CLI showed;
 *  with none, the path stays as it is rather than guessing at /Users/<x>. */
export function tildePath(path: string, homeDir?: string): string {
  if (!homeDir) return path;
  if (path === homeDir) return "~";
  return path.startsWith(`${homeDir}/`) ? `~${path.slice(homeDir.length)}` : path;
}

/** The one line under a home's name: where Khipu found it, then what is true
 *  of its hooks or its sessions. */
export function homeSourceLine(h: ClaudeHomeRow, homeDir?: string): string {
  if (!h.exists) return `${h.source} · folder not found, nothing to install yet`;
  if (h.error) return `${h.source} · couldn't read its config: ${h.error.slice(0, 120)}`;
  if (h.linked_to) {
    return (
      `${h.source} · settings linked to ${tildePath(h.linked_to.path, homeDir)}, ` +
      `so its hooks are the ${h.linked_to.label} home's`
    );
  }
  if (!h.installed) {
    // Partly installed: say what is in, so a row with working hooks never
    // claims its sessions are unrecorded.
    if (h.launcher_ok === false) return `${h.source} · a Khipu launcher link is broken, Install repairs it`;
    if (h.hook_stop && h.hook_precompact) {
      return `${h.source} · its hooks are in but the memory tools are not, Install adds them`;
    }
    if (h.memory_tools_ok) return `${h.source} · its memory tools are in but the hooks are not, Install adds them`;
    return `${h.source} · its sessions run without memory until you install`;
  }
  return h.source;
}

/** What a check mark says, for the reader who cannot see it. */
const MARK_WORD: Record<Mark, string> = {
  ok: "installed",
  warn: "partly installed",
  err: "not installed",
  off: "not checked",
};

/** A home's two checks. Hooks is `warn` when the capture hooks are in but the
 *  rest of the six are not (Reinstall fills them in). */
export function homeChecks(h: ClaudeHomeRow): { hooks: Mark; memoryTools: Mark; launcher?: Mark } {
  if (!h.exists) return { hooks: "off", memoryTools: "off" };
  if (h.error) return { hooks: "err", memoryTools: "err" };
  return {
    hooks: h.hooks_ok ? "ok" : h.hook_stop && h.hook_precompact ? "warn" : "err",
    memoryTools: h.memory_tools_ok ? "ok" : "err",
    // Only shown when it fails: it is the reason an otherwise full row reads
    // Not installed.
    ...(h.launcher_ok === false ? { launcher: "err" as const } : {}),
  };
}

/** The card's one-word verdict, from evidence only:
 *   Not installed — the pack is absent from this harness's config.
 *   Not recording — the capture heartbeat is red for this harness.
 *   N homes not installed / can't be read — Claude Code is in some homes it
 *                   found, not all.
 *   Recording     — the heartbeat shows a capture landing.
 *   Reachable     — the gateway answered (Grok Bot, which has no local hook).
 *  Anything else is "no evidence yet", which is neither a pass nor a failure. */
export function cardStatus(
  row: StatusRow,
  lv: HarnessLiveness | undefined,
  gatewayProbe: Probe | undefined,
): CardStatus {
  if (!row.detected) return { tone: "neutral", label: "Not on this Mac" };
  if (row.harness === "grok_bot") {
    // `mcp` here is only the OPTIONAL per-repo pin; the real configuration
    // lives in the Cursor cloud UI, which this Mac cannot read. "No pin" is
    // therefore not "not installed" — only the gateway probe can say.
    if (gatewayProbe?.ok === true) return { tone: "ok", label: "Reachable" };
    if (gatewayProbe && gatewayProbe.ok === false) {
      return { tone: "err", label: "Not reachable" };
    }
    return { tone: "neutral", label: "Not checked yet" };
  }
  const missing = homesNotInstalled(row);
  const unreadable = homesUnreadable(row);
  if (!installedAnywhere(row)) {
    if (missing === 0 && unreadable > 0) return { tone: "warn", label: unreadableLabel(unreadable) };
    return { tone: "neutral", label: "Not installed" };
  }
  if (lv?.ok === false) return { tone: "err", label: "Not recording" };
  // Never green while a home Khipu found has no Khipu in it: that home's
  // sessions are not recorded, whatever the other homes are doing.
  if (missing > 0 || unreadable > 0) {
    const parts = [
      missing > 0 ? (missing === 1 ? "1 home not installed" : `${missing} homes not installed`) : "",
      unreadable > 0 ? (missing > 0 ? `${unreadable} can't be read` : unreadableLabel(unreadable)) : "",
    ].filter(Boolean);
    return { tone: "warn", label: parts.join(", ") };
  }
  if (lv?.seen && (lv.captures ?? 0) > 0) return { tone: "ok", label: "Recording" };
  return { tone: "neutral", label: "No sessions yet" };
}

/** The "Verified …" line. Prefers this session's own verify result; falls back
 *  to the stored probe, and only when that probe was run FOR THIS HARNESS —
 *  one Mac keeps one probe file, and another pack's round trip is not
 *  evidence about this one. */
export function verifiedLine(
  harness: string,
  fresh: Probe | undefined,
  stored: RecallProbeStatus | null,
  lv?: HarnessLiveness,
): { mark: Mark; text: string } {
  // A live capture beat is real evidence the hook works, even with no timed
  // round-trip probe for this pack yet — only the timing is missing, not the
  // "does it work at all" answer (2026-09-05 pixel-pass finding 3).
  const liveBeat = lv?.ok !== false && lv?.seen && (lv.captures ?? 0) > 0;
  if (fresh) {
    if (fresh.status === "skipped") {
      return {
        mark: "off",
        text: `Recall probe skipped — ${fresh.reason ?? "legacy capture mode writes no database row"}`,
      };
    }
    if (fresh.ok) {
      return {
        mark: "ok",
        text: `Verified just now${fresh.seconds != null ? ` · ${fmtSeconds(fresh.seconds)} round trip` : ""}`,
      };
    }
    return {
      mark: "err",
      text: `Verify failed — ${(fresh.error ?? fresh.reason ?? "the capture never became findable").slice(0, 120)}`,
    };
  }
  const own = stored?.harnesses?.[harness];
  if (own && own.ts) {
    if (own.status === "skipped") {
      return {
        mark: "off",
        text: `Recall probe skipped — ${own.reason ?? "legacy capture mode writes no database row"}`,
      };
    }
    const ownAge = own.age_seconds;
    if (own.ok) {
      return {
        mark: own.stale ? "warn" : "ok",
        text:
          `Verified ${ownAge != null ? `${fmtAge(ownAge)} ago` : "at an unknown time"}` +
          (own.seconds != null ? ` · ${fmtSeconds(own.seconds)} round trip` : "") +
          (own.stale ? " — older than a week, run Verify" : ""),
      };
    }
    return {
      mark: "err",
      text: `Last verify failed${ownAge != null ? ` ${fmtAge(ownAge)} ago` : ""} — ${(own.error ?? own.reason ?? "the capture never became findable").slice(0, 120)}`,
    };
  }
  const last = stored?.last_probe;
  if (!last) {
    if (liveBeat) {
      return {
        mark: "ok",
        text: `Working · hook ran ${fmtAge(lv?.last_captured_age_s)} ago · Verify to time a round trip`,
      };
    }
    return { mark: "off", text: "Not verified on this pack yet — Verify runs it" };
  }
  if (last.harness !== harness) {
    // One Mac keeps one probe file. Another pack's round trip is real evidence
    // about this Mac and none at all about this pack, so it is reported as
    // what it is rather than borrowed as a green tick here.
    const other = LABEL[last.harness as HarnessId] ?? last.harness ?? "another pack";
    if (liveBeat) {
      return {
        mark: "ok",
        text:
          `Working · hook ran ${fmtAge(lv?.last_captured_age_s)} ago · Verify to time a round trip` +
          ` · last timed probe on this Mac was for ${other}`,
      };
    }
    return {
      mark: "off",
      text:
        `Not verified on this pack yet — the last probe on this Mac ran for ${other}` +
        (stored?.age_seconds != null ? `, ${fmtAge(stored.age_seconds)} ago` : ""),
    };
  }
  if (last.status === "skipped") {
    return {
      mark: "off",
      text: `Recall probe skipped — ${last.reason ?? "legacy capture mode writes no database row"}`,
    };
  }
  const age = stored?.age_seconds;
  if (last.ok) {
    return {
      mark: stored?.ok === false ? "warn" : "ok",
      text:
        `Verified ${age != null ? `${fmtAge(age)} ago` : "at an unknown time"}` +
        (last.seconds != null ? ` · ${fmtSeconds(last.seconds)} round trip` : "") +
        (stored?.ok === false ? " — older than a week, run Verify" : ""),
    };
  }
  return {
    mark: "err",
    text: `Last verify failed — ${(last.error ?? stored?.reason ?? "the capture never became findable").slice(0, 120)}`,
  };
}

/** One harness's entry in `integrations install|uninstall`'s JSON. A pack that
 *  could not finish says so here (`ok: false`, `aborted`) and still exits 0. */
type ActResult = {
  harness?: string;
  ok?: boolean;
  aborted?: boolean;
  error?: string;
  /** Per Claude home (`install` and `uninstall` on claude_code). */
  homes?: Array<{ label?: string; hooks_kept?: string }>;
};

/** The results document of an install/uninstall run: install prints it first
 *  and the `{verify: [...]}` document after, and a refused `--home` prints a
 *  single object. Anything unreadable is no results rather than an error. */
export function parseActResults(raw: string): ActResult[] {
  const idx = raw.indexOf('{\n  "verify"');
  try {
    const doc = JSON.parse(idx >= 0 ? raw.slice(0, idx) : raw) as ActResult | ActResult[];
    return Array.isArray(doc) ? doc : [doc];
  } catch {
    return [];
  }
}

/** The first failing probe of a verify run, as one line. Mirrors the CLI's
 *  own exit rule: a detected harness with no `ok` is a failure. */
export function verifyFailure(list: VerifyRow[]): string | null {
  const bad = list.find((v) => (v.ok ?? !v.detected) === false);
  if (!bad) return null;
  const probe = Object.entries(bad.components ?? {}).find(([, p]) => p?.ok === false);
  const detail = probe ? `${probe[0]}${probe[1]?.error ? `: ${probe[1].error}` : ""}` : (bad.note ?? "a check failed");
  return `${bad.harness} ${detail}`.slice(0, 160);
}

export type InstallRun = {
  /** The CLI's error when the install itself did not finish. */
  failed: string | null;
  /** The separate verify run; null when it was skipped or could not be read. */
  verify: VerifyRow[] | null;
  /** Why that verify failed, or could not run; null when it passed or was skipped. */
  verifyFailed: string | null;
};

/** `integrations install` without the CLI's own verify, then `integrations
 *  verify` as its own call. Run together, a failed verify prints two JSON
 *  documents and exits 2, which the runner reports as an error although the
 *  files were written. Rejects only when the install call itself errors. A
 *  per-home install skips the verify: that probe covers the whole pack. */
export async function runInstall(
  runKhipu: (args: string[]) => Promise<string>,
  harness: string,
  home?: string,
): Promise<InstallRun> {
  const raw = await runKhipu([
    "integrations",
    "install",
    harness,
    ...(home ? ["--home", home] : []),
    "--no-verify",
  ]);
  // A pack that stopped (a config it could not read) says so in the JSON, not
  // in the exit code.
  const stopped = parseActResults(raw).find((r) => r.ok === false || r.aborted);
  if (stopped) {
    return { failed: (stopped.error ?? "the pack stopped before finishing").slice(0, 160), verify: null, verifyFailed: null };
  }
  if (home) return { failed: null, verify: null, verifyFailed: null };
  try {
    const verify = JSON.parse(await runKhipu(["integrations", "verify", harness])) as VerifyRow[];
    return { failed: null, verify, verifyFailed: verifyFailure(verify) };
  } catch (e) {
    return { failed: null, verify: null, verifyFailed: String(e).slice(0, 160) };
  }
}

export function IntegrationsPanel({
  runKhipu,
  onToast,
  active,
  liveness,
  recallProbe,
  refreshHealth,
  onAnotherMac,
}: {
  runKhipu: (args: string[]) => Promise<string>;
  onToast: (msg: string) => void;
  active: boolean;
  /** `doctor.capture_liveness` — the recording evidence. */
  liveness: LivenessPayload | null;
  /** `doctor.recall_probe` — the stored round-trip evidence. */
  recallProbe: RecallProbeStatus | null;
  /** Re-read doctor after an install/verify, so the evidence on these cards
   *  is never older than the action the user just took. The panel waits for it
   *  before the buttons come back. */
  refreshHealth: () => Promise<string | null | void> | void;
  onAnotherMac: () => void;
}) {
  const [rows, setRows] = useState<StatusRow[] | null>(null);
  const [verify, setVerify] = useState<Record<string, VerifyRow>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  // "Harness auto-verify": the moment each harness was last Installed, and
  // which harnesses have since flipped to Verified on their own because a
  // real session's hook dispatch (or capture) landed after that moment —
  // docs/plans/2026-09-05-setup-that-cannot-strand-you.md.
  const [installedAt, setInstalledAt] = useState<Record<string, number>>({});
  const [autoVerified, setAutoVerified] = useState<Record<string, boolean>>({});
  // Which home row's action gets keyboard focus once an Install/Remove ends.
  const [focusHome, setFocusHome] = useState<{ path: string; cmd: "install" | "uninstall" } | null>(null);
  const homeButtons = useRef<Record<string, HTMLButtonElement | null>>({});

  const load = useCallback(async () => {
    setLoadError(null);
    setBusy((prev) => prev ?? "load");
    try {
      const raw = await runKhipu(["integrations", "status"]);
      setRows(JSON.parse(raw) as StatusRow[]);
    } catch (e) {
      // A failed status read is NOT "nothing installed" — keep the last rows
      // and surface the failure as its own state.
      setLoadError(String(e));
    } finally {
      setBusy((prev) => (prev === "load" ? null : prev));
    }
  }, [runKhipu]);

  // Panels are CSS-hidden on tab switch, not unmounted, so a mount-only
  // effect never re-fetches — the pane goes stale as soon as something
  // changes elsewhere (audit 2026-08-31).
  useEffect(() => {
    if (!active) return;
    void load();
  }, [active, load]);

  // Poll while the tab is open, and again on window focus (the harness was
  // very possibly restarted and used while this Mac's screen was elsewhere)
  // — the same cadence Owed uses for its own "does it update near
  // immediately?" refresh. No manual click required for a card to notice a
  // fresh capture landed.
  useEffect(() => {
    if (!active) return;
    const every = window.setInterval(() => void load(), 15_000);
    const onFocus = () => void load();
    window.addEventListener("focus", onFocus);
    return () => {
      window.clearInterval(every);
      window.removeEventListener("focus", onFocus);
    };
  }, [active, load]);

  // A card flips to Verified on its own once real evidence lands AFTER
  // Install ran: either `last_beat_at` (the capture hook actually firing) or
  // the stored end-to-end probe for this harness, whichever is newer. Once
  // flipped, stays flipped for the rest of this pane's lifetime — evidence
  // does not un-happen.
  useEffect(() => {
    if (!rows) return;
    setAutoVerified((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const r of rows) {
        if (next[r.harness]) continue;
        const since = installedAt[r.harness];
        if (since == null) continue;
        const beatAt = r.last_beat_at ? Date.parse(r.last_beat_at) : NaN;
        const probeAt = recallProbe?.harnesses?.[r.harness]?.ok
          ? Date.parse(recallProbe.harnesses[r.harness]?.ts ?? "")
          : NaN;
        if ((!Number.isNaN(beatAt) && beatAt > since) || (!Number.isNaN(probeAt) && probeAt > since)) {
          next[r.harness] = true;
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [rows, installedAt, recallProbe]);

  const act = useCallback(
    async (harness: HarnessId | "all", cmd: "install" | "verify" | "uninstall", home?: string) => {
      // `home` narrows a Claude Code install/remove to one of its homes
      // (`homes[].path`); without it the CLI acts on every home it found.
      setBusy(home ? `${cmd}:${harness}:${home}` : `${cmd}:${harness}`);
      let message: string | null = null;
      try {
        if (cmd === "install") {
          const run = await runInstall(runKhipu, harness, home);
          if (run.failed) {
            message = `install failed: ${run.failed}`;
            return;
          }
          const list = run.verify;
          if (list) {
            setVerify((prev) => {
              const next = { ...prev };
              for (const v of list) next[v.harness] = v;
              return next;
            });
          }
          if (home) {
            // One home's install says nothing about the others, so it does not
            // arm auto-verify: a capture from another home must not mark the
            // card Verified.
            const label = rows?.flatMap((r) => r.homes ?? []).find((h) => h.path === home)?.label ?? "that home";
            message = `Installed in ${label}. Restart its sessions to load it; Verify on the card checks it.`;
          } else {
            const installedAtNow = Date.now();
            setInstalledAt((prev) => {
              const next = { ...prev };
              const targets = harness === "all" ? (rows ?? []).map((r) => r.harness) : [harness];
              for (const h of targets) {
                next[h] = installedAtNow;
                setAutoVerified((av) => (av[h] ? { ...av, [h]: false } : av));
              }
              return next;
            });
            message = run.verifyFailed
              ? `Installed; verify failed: ${run.verifyFailed}`
              : "Installed. Restart each harness, then start any session — its card turns green by itself.";
          }
          return;
        }
        const raw = await runKhipu(["integrations", cmd, harness, ...(home ? ["--home", home] : [])]);
        if (cmd === "verify") {
          const list = JSON.parse(raw) as VerifyRow[];
          setVerify((prev) => {
            const next = { ...prev };
            for (const v of list) next[v.harness] = v;
            return next;
          });
          return;
        }
        // Uninstall reports a pack that stopped in the JSON, not in the exit code.
        const results = parseActResults(raw);
        const failed = results.find((r) => r.ok === false || r.aborted);
        if (failed) {
          message = `${cmd} failed: ${(failed.error ?? "the pack stopped before finishing").slice(0, 160)}`;
        } else {
          // One home out of several leaves the others, and the evidence about
          // the harness, in place.
          if (!home) {
            setVerify((prev) => {
              const next = { ...prev };
              if (harness === "all") return {};
              delete next[harness];
              return next;
            });
          }
          const kept = results.flatMap((r) => r.homes ?? []).find((h) => h.hooks_kept)?.hooks_kept;
          message = kept
            ? `Removed the memory tools. The hooks were kept (${kept}) because another home uses them. Backups kept next to each file.`
            : "Removed Khipu entries. Backups kept next to each file.";
        }
      } catch (e) {
        message = `${cmd} failed: ${String(e).slice(0, 160)}`;
      } finally {
        // Whatever happened, the card shows what is on disk now, not what it
        // showed before the click. Verify writes a fresh probe result and
        // install changes what the heartbeat will say next; both live in the
        // doctor payload.
        await load();
        const healthError = await refreshHealth();
        // The doctor read clears the toast, so the message goes up after it,
        // carrying the doctor's own failure when there was one.
        if (message) {
          onToast(
            typeof healthError === "string" && healthError
              ? `${message} The health check then failed: ${healthError.slice(0, 160)}`
              : message,
          );
        }
        setBusy(null);
        // The button that was clicked is gone (Install and Remove swap) or was
        // disabled while the CLI ran; hand focus to the row's action.
        if (home && cmd !== "verify") setFocusHome({ path: home, cmd });
      }
    },
    [runKhipu, load, onToast, refreshHealth, rows],
  );

  // Put keyboard focus back on a home row's action once the rows have reloaded.
  useEffect(() => {
    if (!focusHome || busy != null) return;
    const { path, cmd } = focusHome;
    const other = cmd === "install" ? "uninstall" : "install";
    (homeButtons.current[`${cmd}:${path}`] ?? homeButtons.current[`${other}:${path}`])?.focus();
    setFocusHome(null);
  }, [focusHome, busy]);

  const detected = (rows ?? []).filter((r) => r.detected);

  return (
    <div className="panel-body wide">
      <WorkingBanner
        label={
          busy == null
            ? null
            : busy === "load"
              ? "Checking harnesses…"
              : busy.startsWith("install:")
                ? "Installing harness packs…"
                : busy.startsWith("verify:")
                  ? "Verifying — capturing a throwaway session and searching for it…"
                  : busy.startsWith("uninstall:")
                    ? "Removing Khipu entries…"
                    : "Working…"
        }
      />
      {loadError ? (
        <Callout
          tone="err"
          stripe
          title="Couldn't read the harness configs"
          action={
            <button type="button" disabled={busy != null} onClick={() => void load()}>
              <RefreshCw size={14} strokeWidth={1.75} aria-hidden /> Retry
            </button>
          }
        >
          The CLI didn't answer, so this is the last known state. Check that
          Python 3.11 and the Khipu folder are set under Settings → Advanced.
        </Callout>
      ) : null}

      <div className="inline">
        <button
          type="button"
          className="primary"
          disabled={busy != null || detected.length === 0}
          onClick={() => void act("all", "install")}
        >
          {busy === "install:all" ? <Loader2 size={14} className="spin" aria-hidden /> : null}
          Install all
        </button>
        <button
          type="button"
          disabled={busy != null || detected.length === 0}
          onClick={() => void act("all", "verify")}
        >
          {busy === "verify:all" ? <Loader2 size={14} className="spin" aria-hidden /> : null}
          Verify all
        </button>
        <button type="button" disabled={busy != null} onClick={() => void load()}>
          <RefreshCw size={14} strokeWidth={1.75} aria-hidden /> Refresh
        </button>
        <span className="meta push">
          Verified = a real capture went in and came back out of search on this Mac
        </span>
      </div>

      {rows == null && !loadError ? (
        <p className="muted">Reading harness configs…</p>
      ) : null}

      <div className="hgrid">
        {(rows ?? []).map((r) => {
          const v = verify[r.harness];
          const lv = liveness?.harnesses?.[r.harness];
          const justAutoVerified = autoVerified[r.harness] === true;
          // Claude Code lists its homes whenever one is on disk; a home that
          // is missing keeps the card off green even after a capture lands.
          const homes = r.harness === "claude_code" && r.detected ? (r.homes ?? []) : [];
          const status =
            justAutoVerified && homesNotInstalled(r) === 0 && homesUnreadable(r) === 0
              ? { tone: "ok" as const, label: "Verified" }
              : cardStatus(r, lv, v?.components?.mcp);
          const installed = r.harness === "grok_bot" ? r.mcp : installedAnywhere(r);
          // Grok Bot has nothing local to install, so Verify ("Probe gateway")
          // is what its card offers instead — gated on detection, not on the
          // per-repo pin.
          const canVerify = r.harness === "grok_bot" ? r.detected : installed;
          const verified = justAutoVerified
            ? { mark: "ok" as Mark, text: "Verified — a session ran the hook after Install, no click needed" }
            : verifiedLine(r.harness, v?.components?.recall_probe, recallProbe, lv);
          const awaitingAutoVerify = installedAt[r.harness] != null && !justAutoVerified;

          // 1. Capture hook — the heartbeat, not the config file.
          const hook: { mark: Mark; text: string } =
            r.harness === "grok_bot"
              ? { mark: "off", text: "Capture hook · cloud writes through the memory tool" }
              : !installed
                ? { mark: "err", text: "Capture hook · not installed" }
                : lv?.ok === false
                  ? {
                      mark: "err",
                      text: `Capture hook · ${firstReason(lv) || "stopped recording"}`,
                    }
                  : lv?.seen && (lv.captures ?? 0) > 0
                    ? {
                        mark: "ok",
                        text: `Capture hook · last capture ${fmtAge(lv.last_captured_age_s)} ago`,
                      }
                    : lv?.seen
                      ? { mark: "warn", text: "Capture hook · ran, nothing captured yet" }
                      : lv
                        ? { mark: "off", text: "Capture hook · no session has run it yet" }
                        : { mark: "off", text: "Capture hook · recording not checked yet" };

          // 2. Recall at session start.
          const ruleStale = v?.rule_stale;
          // With homes, the pack-level rule is the AND, so one missing home
          // would turn the shared evidence red; each home's own Hooks check
          // carries that instead.
          const recallRule = homes.length > 0
            ? foundHomes(r).some((h) => h.recall_rule === "installed") ? "installed" : "missing"
            : r.recall_rule;
          const recall: { mark: Mark; text: string } =
            recallRule === "n/a"
              ? { mark: "off", text: "Recall at start · not available in this harness" }
              : recallRule === "mcp_instructions"
                ? { mark: "ok", text: "Recall at start · served with the memory tools" }
                : recallRule === "project_scoped"
                  ? ruleStale === true
                    ? { mark: "warn", text: "Recall rule out of date in this project" }
                    : { mark: "ok", text: "Recall rule · one per project" }
                  : recallRule === "installed"
                    ? { mark: "ok", text: "Recall at session start" }
                    : { mark: "err", text: "Recall at session start · not installed" };

          // 3. Memory tools (MCP). A passing grok_bot gateway probe proves the
          // gateway answers this Mac, not that the cloud agent points at it.
          const mcpProbe = v?.components?.mcp;
          const mcp: { mark: Mark; text: string } =
            r.harness === "grok_bot"
              ? mcpProbe?.ok
                ? { mark: "ok", text: "Gateway answered this Mac · token accepted" }
                : mcpProbe
                  ? { mark: "err", text: `Gateway · ${(mcpProbe.error ?? "no answer").slice(0, 110)}` }
                  : { mark: "off", text: "Memory tools (MCP) · set in the Cursor cloud, not readable here" }
              : mcpProbe?.ok
                ? {
                    mark: "ok",
                    text: `Memory tools (MCP) · ${mcpProbe.ms} ms, ${mcpProbe.episodes} episodes`,
                  }
                : mcpProbe
                  ? { mark: "err", text: `Memory tools (MCP) · ${(mcpProbe.error ?? "failed").slice(0, 110)}` }
                  : r.mcp
                    ? { mark: "ok", text: "Memory tools (MCP)" }
                    : { mark: "err", text: "Memory tools (MCP) · not installed" };

          // With homes, memory tools are installed per home and shown there;
          // the shared line appears only once Verify has probed the handshake.
          const checks = homes.length > 0 && !mcpProbe ? [hook, recall, verified] : [hook, recall, mcp, verified];
          const notRecording = status.label === "Not recording";
          const found = foundHomes(r);
          const homeDir = userHomeDir(r.homes);
          return (
            <div className={homes.length > 0 ? "hcard homes-card" : "hcard"} key={r.harness}>
              <div className="top">
                <span className="name">{LABEL[r.harness]}</span>
                <Tag tone={status.tone} dot>
                  {status.label}
                </Tag>
              </div>
              <div className="checks">
                {checks.map((c, i) => (
                  <div key={i}>
                    <CheckMark mark={c.mark} />
                    <span>{c.text}</span>
                  </div>
                ))}
              </div>
              {homes.length > 0 ? (
                <div className="homes">
                  <div className="homes-head">
                    Claude homes <span className="meta">{found.length} found</span>
                  </div>
                  <div role="list" aria-label="Claude homes">
                    {homes.map((h) => {
                      const hc = homeChecks(h);
                      const tag: { tone: Tone; label: string } = !h.exists
                        ? { tone: "neutral", label: "Not found" }
                        : h.error
                          ? { tone: "err", label: "Can't read" }
                          : h.installed
                            ? { tone: "ok", label: "Installed" }
                            : { tone: "warn", label: "Not installed" };
                      const canAct = h.exists && !h.error;
                      // Remove is offered for anything of Khipu's in the row,
                      // so a half-installed home can be cleared as well as repaired.
                      const canRemove = canAct && (h.installed || hasKhipuEntries(h));
                      return (
                        <div className="home" role="listitem" key={h.path}>
                          <div className="home-id">
                            <div className="home-line">
                              <span className="home-name">{h.label}</span>
                              <span className="home-path">{tildePath(h.path, homeDir)}</span>
                            </div>
                            <span className="home-src">{homeSourceLine(h, homeDir)}</span>
                          </div>
                          <div className="home-checks">
                            <span>
                              <CheckMark mark={hc.hooks} />
                              Hooks
                              <span className="sr-only">: {MARK_WORD[hc.hooks]}</span>
                            </span>
                            <span>
                              <CheckMark mark={hc.memoryTools} />
                              Memory tools
                              <span className="sr-only">: {MARK_WORD[hc.memoryTools]}</span>
                            </span>
                            {hc.launcher ? (
                              <span>
                                <CheckMark mark={hc.launcher} />
                                Launcher
                                <span className="sr-only">: broken</span>
                              </span>
                            ) : null}
                          </div>
                          <div className="home-acts">
                            <Tag tone={tag.tone} dot>
                              {tag.label}
                            </Tag>
                            {canRemove ? (
                              <button
                                type="button"
                                className="sm link"
                                aria-label={`Remove Khipu from ${h.label}`}
                                disabled={busy != null}
                                ref={(el) => {
                                  homeButtons.current[`uninstall:${h.path}`] = el;
                                }}
                                onClick={() => void act(r.harness, "uninstall", h.path)}
                              >
                                {busy === `uninstall:${r.harness}:${h.path}` ? (
                                  <Loader2 size={14} className="spin" aria-hidden />
                                ) : null}
                                Remove
                              </button>
                            ) : null}
                            {canAct && !h.installed ? (
                              <button
                                type="button"
                                className="sm primary"
                                aria-label={`Install Khipu in ${h.label}`}
                                disabled={busy != null}
                                ref={(el) => {
                                  homeButtons.current[`install:${h.path}`] = el;
                                }}
                                onClick={() => void act(r.harness, "install", h.path)}
                              >
                                {busy === `install:${r.harness}:${h.path}` ? (
                                  <Loader2 size={14} className="spin" aria-hidden />
                                ) : null}
                                Install
                              </button>
                            ) : null}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              ) : null}
              {ruleStale === true ? (
                <div className="note">Re-run Install for this repo to refresh its rule.</div>
              ) : awaitingAutoVerify ? (
                <div className="note">
                  Installed. Restart {LABEL[r.harness]}, then start any session — this
                  card turns green by itself.
                </div>
              ) : r.harness === "grok_bot" ? (
                <div className="note" title={WHERE[r.harness]}>
                  One install per repo Grok Bot works in; the token lives in Cursor
                  cloud secrets, never in the repo.
                </div>
              ) : !r.detected ? (
                <div className="note">Config folder not found — nothing to install.</div>
              ) : homes.length > 0 ? (
                <div className="note wrap">
                  Khipu finds Claude homes in ~/.claude, CLAUDE_CONFIG_DIR and T3's settings.
                  It reads T3's settings and never changes them.
                </div>
              ) : (
                <div className="note mono" title={WHERE[r.harness]}>
                  {WHERE[r.harness]}
                </div>
              )}
              <div className="hacts">
                {r.detected ? (
                  <>
                    <button
                      type="button"
                      className={notRecording || !installed ? "primary sm" : "sm"}
                      disabled={busy != null || r.harness === "grok_bot"}
                      onClick={() => void act(r.harness, "install")}
                    >
                      {busy === `install:${r.harness}` ? (
                        <Loader2 size={14} className="spin" aria-hidden />
                      ) : null}
                      {!installed
                        ? "Install"
                        : notRecording
                          ? "Reinstall hook"
                          : "Reinstall"}
                    </button>
                    <button
                      type="button"
                      className="sm"
                      disabled={busy != null || !canVerify}
                      onClick={() => void act(r.harness, "verify")}
                    >
                      {busy === `verify:${r.harness}` ? (
                        <Loader2 size={14} className="spin" aria-hidden />
                      ) : null}
                      {r.harness === "grok_bot" ? "Probe gateway" : "Verify"}
                    </button>
                    {homes.length > 0 ? null : (
                      <button
                        type="button"
                        className="sm link push"
                        disabled={busy != null || !installed}
                        onClick={() => void act(r.harness, "uninstall")}
                      >
                        Remove
                      </button>
                    )}
                  </>
                ) : (
                  <span className="meta">Nothing to install here.</span>
                )}
              </div>
            </div>
          );
        })}

        <div className="hcard dashed">
          <b>Another Mac</b>
          <span className="meta wrap">
            Save a join kit here and open it on the new Mac.
          </span>
          <button type="button" className="sm" onClick={onAnotherMac}>
            Save join kit…
          </button>
        </div>
      </div>

      <GatewayTokenCard active={active} />

      <p className="muted">
        Install writes Khipu's own entries next to whatever is already there and
        backs each file up first. Your existing capture hooks keep running.
        Restart a harness after installing so it loads the change.
      </p>
      {liveness == null ? (
        <p className="muted">
          <TriangleAlert size={14} strokeWidth={1.75} aria-hidden className="warn" />{" "}
          Recording evidence comes from the health report — open Home once, or
          Refresh here, to fill in the capture and verified lines.
        </p>
      ) : null}
    </div>
  );
}
