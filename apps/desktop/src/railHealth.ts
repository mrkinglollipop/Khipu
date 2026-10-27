import type { LivenessPayload, T3Health } from "./IntegrationsPanel";

/** `doctor.claude_homes` (khipu.integrations.claude_homes_report): one row per
 *  Claude home found. The app already loads doctor, so the rail and the
 *  Harnesses badge read this instead of polling the Harnesses status. */
export type ClaudeHomesReport = {
  homes?: Array<{ exists?: boolean; installed?: boolean; error?: string }>;
  error?: string;
};

export type ClaudeHomesGap = { missing: number; unreadable: number; checkFailed?: boolean };

/** `reason` says what the warning is about, so the rail and the card never call
 *  a provider-hooks warning "needs a linked capture". */
export type T3WarningReason = "lookup" | "unlinked" | "hooks" | "partial" | "other";
export type T3HealthGap = { warning: boolean; lookupFailed: boolean; reason?: T3WarningReason };

/** Which kind of T3 warning is showing. The CLI sends plain sentences, so the
 *  "used without a thread-linked capture" one is matched by its wording; a
 *  provider with `hooks: "missing"` (or `"partial"`, amber) is the other known
 *  kind. A lookup failure outranks both, an unlinked run outranks missing hooks.
 *  A provider that is partial on its own raises nothing: Antigravity in T3 is
 *  partial for good (its runtime cannot take injected context), so only a
 *  warning the CLI actually sent makes it a reason. */
export function t3WarningReason(t3: T3Health | null | undefined): T3WarningReason | null {
  if (!t3?.detected) return null;
  if (t3.lookup?.ok === false) return "lookup";
  const warnings = t3.warnings ?? [];
  if (warnings.length === 0) return null;
  if (warnings.some((w) => /thread-linked capture/i.test(w))) return "unlinked";
  if ((t3.providers ?? []).some((p) => p.hooks === "missing")) return "hooks";
  // A provider with some features in and some not is amber, never "without hooks":
  // the CLI only warns for it when a feature the harness can carry is missing.
  if ((t3.providers ?? []).some((p) => p.hooks === "partial")) return "partial";
  return "other";
}

/** T3 is optional, but once its settings file exists a failed thread lookup
 * or a recent unlinked run is actionable and must not sit behind a green rail. */
export function t3HealthGap(t3: T3Health | null | undefined): T3HealthGap {
  const reason = t3WarningReason(t3);
  if (reason == null) return { warning: false, lookupFailed: false };
  return { warning: true, lookupFailed: reason === "lookup", reason };
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
  if (liveness?.ok === false || typeof (liveness as { error?: unknown } | null)?.error === "string") return { tone: "err", text: "Couldn't check recording" };
  if (liveness == null) return { tone: "warn", text: "Checking harnesses…" };
  if (gap.checkFailed) return { tone: "warn", text: "Couldn't check Claude homes" };
  if (gap.missing > 0) return { tone: "warn", text: `${homesWord(gap.missing)} not installed` };
  if (gap.unreadable > 0) return { tone: "warn", text: `${homesWord(gap.unreadable)} can't be read` };
  if (t3.lookupFailed) return { tone: "warn", text: "T3 threads not linking" };
  if (t3.warning) {
    return {
      tone: "warn",
      text: t3.reason === "hooks" ? "T3 providers without hooks"
        : t3.reason === "partial" ? "T3 providers partly set up"
        : t3.reason === "other" ? "T3 needs a look"
        : "T3 needs a linked capture",
    };
  }
  const harnesses = Object.values(liveness.harnesses ?? {});
  if (harnesses.length === 0 || harnesses.some((h) => h.seen !== true || h.ok !== true)) {
    return { tone: "warn", text: "Recording not checked" };
  }
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
  if (
    liveness?.ok === false ||
    typeof (liveness as { error?: unknown } | null)?.error === "string"
  ) return { n: 1, quiet: false };
  const homes = gap.missing + gap.unreadable + (gap.checkFailed ? 1 : 0);
  const attention = homes + (t3.warning ? 1 : 0);
  return attention > 0 ? { n: attention, quiet: false } : null;
}
