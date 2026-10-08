import type { LivenessPayload, T3Health } from "./IntegrationsPanel";

/** `doctor.claude_homes` (khipu.integrations.claude_homes_report): one row per
 *  Claude home found. The app already loads doctor, so the rail and the
 *  Harnesses badge read this instead of polling the Harnesses status. */
export type ClaudeHomesReport = {
  homes?: Array<{ exists?: boolean; installed?: boolean; error?: string }>;
  error?: string;
};

export type ClaudeHomesGap = { missing: number; unreadable: number; checkFailed?: boolean };

export type T3HealthGap = { warning: boolean; lookupFailed: boolean };

/** T3 is optional, but once its settings file exists a failed thread lookup
 * or a recent unlinked run is actionable and must not sit behind a green rail. */
export function t3HealthGap(t3: T3Health | null | undefined): T3HealthGap {
  if (!t3?.detected) return { warning: false, lookupFailed: false };
  const lookupFailed = t3.lookup?.ok === false;
  return { warning: lookupFailed || (t3.warnings?.length ?? 0) > 0, lookupFailed };
}

/** Found Claude homes with no Khipu in them, and found homes whose config
 *  could not be read. Both are sessions that are not being recorded. A block
 *  that carries `error` means the check itself failed, so nothing is known. */
export function claudeHomesGap(report: ClaudeHomesReport | null | undefined): ClaudeHomesGap {
  if (report?.error) return { missing: 0, unreadable: 0, checkFailed: true };
  const live = (report?.homes ?? []).filter((h) => h.exists);
  return {
    missing: live.filter((h) => !h.installed && !h.error).length,
    unreadable: live.filter((h) => h.error).length,
  };
}

function homesWord(n: number): string {
  return `${n} Claude home${n === 1 ? "" : "s"}`;
}

/** The rail's health line. A database that does not answer and a heartbeat
 *  that is red both outrank a Claude home with no Khipu in it, which is a
 *  warning, never a green "All harnesses recording". */
export function railHealthLine(
  dsnOk: boolean | null,
  liveness: LivenessPayload | null,
  gap: ClaudeHomesGap,
  t3: T3HealthGap = { warning: false, lookupFailed: false },
): { tone: "ok" | "warn" | "err"; text: string } {
  const red = liveness?.red ?? [];
  if (dsnOk === false) return { tone: "err", text: "Database not reachable" };
  if (red.length > 0) {
    return { tone: "err", text: `${red.length} harness${red.length === 1 ? "" : "es"} not recording` };
  }
  if (liveness == null) return { tone: "warn", text: "Checking harnesses…" };
  if (gap.checkFailed) return { tone: "warn", text: "Couldn't check Claude homes" };
  if (gap.missing > 0) return { tone: "warn", text: `${homesWord(gap.missing)} not installed` };
  if (gap.unreadable > 0) return { tone: "warn", text: `${homesWord(gap.unreadable)} can't be read` };
  if (t3.lookupFailed) return { tone: "warn", text: "T3 threads not linking" };
  if (t3.warning) return { tone: "warn", text: "T3 needs a linked capture" };
  return { tone: "ok", text: "All harnesses recording" };
}

/** The count on the Harnesses nav item: red harnesses first, else the Claude
 *  homes that need attention. */
export function harnessesBadge(
  liveness: LivenessPayload | null,
  gap: ClaudeHomesGap,
  t3: T3HealthGap = { warning: false, lookupFailed: false },
): { n: number; quiet: false } | null {
  const red = liveness?.red ?? [];
  if (red.length > 0) return { n: red.length, quiet: false };
  const homes = gap.missing + gap.unreadable + (gap.checkFailed ? 1 : 0);
  const attention = homes + (t3.warning ? 1 : 0);
  return attention > 0 ? { n: attention, quiet: false } : null;
}
