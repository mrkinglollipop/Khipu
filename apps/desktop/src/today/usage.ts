// Today reads the same account/window words as Accounts. Only visibility is local.
import { accountWindows, orderedAccounts, readLine, rowModel, tightest } from "../accounts/accountsModel";
import type { RowTone } from "../accounts/accountsModel";
import type { UsageReport } from "../PlansCard";
import type { RouteAccount, RouteView } from "../routing";

export const USAGE_CHOICES_KEY = "khipu.today.usage.accounts";
export type UsageChoices = Record<string, boolean>;
export type UsageTile = {
  key: string; label: string; word: string; tone: RowTone;
  left: number | null; line: string; window: string; detail: string | null;
  ended: boolean; planEnd: string | null; shown: boolean;
};

export function readUsageChoices(): UsageChoices {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(USAGE_CHOICES_KEY) ?? "{}");
    return value && typeof value === "object" && !Array.isArray(value)
      ? Object.fromEntries(Object.entries(value).filter(([, v]) => typeof v === "boolean")) : {};
  } catch { return {}; }
}
export function saveUsageChoices(choices: UsageChoices): void {
  try { localStorage.setItem(USAGE_CHOICES_KEY, JSON.stringify(choices)); } catch { /* This screen still works without storage. */ }
}

/** Date-only terms name a calendar day, not midnight UTC. Keep the tile through
 * that whole local day; timestamps name the day they fall on for this viewer. */
export function planEnd(a: RouteAccount, now: Date): { ended: boolean; line: string } | null {
  const raw = a.terms?.ends;
  if (!raw) return null;
  const dateOnly = /^(\d{4})-(\d{2})-(\d{2})$/.exec(raw);
  const end = dateOnly ? new Date(+dateOnly[1], +dateOnly[2] - 1, +dateOnly[3]) : new Date(raw);
  if (!Number.isFinite(end.getTime())) return null;
  const day = new Date(end.getFullYear(), end.getMonth(), end.getDate());
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const tomorrow = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
  const ended = day.getTime() < today.getTime();
  const date = end.toLocaleDateString("en-GB", { day: "numeric", month: "short", ...(end.getFullYear() !== now.getFullYear() ? { year: "numeric" } : {}) });
  const relative = day.getTime() === today.getTime() ? "today, " : day.getTime() === tomorrow.getTime() ? "tomorrow, " : "";
  return { ended, line: ended ? `Plan ended ${date}` : day.getTime() === today.getTime() ? "Plan ends today" : `Plan ends ${relative}${date}` };
}

export function usageTiles(view: RouteView | null | undefined, plans: UsageReport | null | undefined, choices: UsageChoices, now: Date): UsageTile[] {
  return orderedAccounts(view).map((a) => {
    const row = rowModel(a, view, plans, 0, now);
    const windows = ["room", "paused", "limited"].includes(row.kind) ? accountWindows(a, view, now) : [];
    const w = tightest(windows);
    const usage = plans?.accounts?.find((r) => r.key === a.key);
    // Missing windows and no_plan_limits are unreadable, never evidence of no limit.
    const noLimit = row.kind === "noreading" && usage?.ok === true && usage.credits?.unlimited === true;
    const end = planEnd(a, now);
    const at = view?.plan_facts?.[a.key]?.figures_at ?? a.advice?.newest_reading_at ?? usage?.observed_at ?? usage?.probed_at ?? view?.checked_at;
    const age = at ? now.getTime() - new Date(at).getTime() : 0;
    const reading = at && (view?.plan_facts?.[a.key]?.stale || usage?.stale || age > 5 * 60_000)
      ? readLine({ checked_at: at }, now)?.replace(/\.$/, "") ?? null : null;
    const second = windows.find((x) => x !== w);
    return {
      key: a.key, label: a.label, word: noLimit ? "No limit" : row.word, tone: row.tone,
      left: w?.left ?? null,
      line: w ? `${w.left}% left` : noLimit ? "Pay as you go" : row.line,
      window: w ? `${w.name.toLowerCase()}${w.resets ? ` · resets ${w.resets}` : ""}` : "",
      detail: end?.line ?? reading ?? (second ? `${second.name}: ${second.left}% left` : null),
      ended: end?.ended ?? false, planEnd: end?.line ?? null,
      shown: Object.prototype.hasOwnProperty.call(choices, a.key) ? choices[a.key] : !end?.ended,
    };
  });
}
