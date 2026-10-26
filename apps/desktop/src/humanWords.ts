/* Plain-words helpers for text the CLI hands the app: plan names, how much is
   left, raw error strings and toasts. Pure functions, so each sentence the
   owner reads is pinned by a test. A raw CLI string never reaches a first
   line; it can still sit behind a "Technical details" disclosure. */

/** The plan word beside an account's name: "max" -> "Max". A plan that is only
 *  digits is an id the CLI put in the wrong field, never a name. */
export function planWord(plan: string | null | undefined): string {
  const p = (plan ?? "").trim();
  if (!p || /^\d+$/.test(p)) return "";
  return p.charAt(0).toUpperCase() + p.slice(1);
}

/** Under this much left, a plan is "almost used up" whatever its pace says. */
export const ALMOST_USED_UP_PCT = 10;

export function almostUsedUp(leftPct: number | null | undefined): boolean {
  return typeof leftPct === "number" && leftPct > 0 && leftPct < ALMOST_USED_UP_PCT;
}

/** "Almost used up: " ahead of a figure that is nearly gone, else nothing. */
export function almostPrefix(leftPct: number | null | undefined): string {
  return almostUsedUp(leftPct) ? "Almost used up: " : "";
}

/** "1.4" -> "1.4%", rounded to a tenth. */
export function pctWords(n: number): string {
  return `${Math.round(n * 10) / 10}%`;
}

/** A span of seconds as "1 day 21 hours", "5 hours", "40 minutes". */
export function longSpan(seconds: number): string {
  const mins = Math.max(1, Math.round(seconds / 60));
  if (mins < 120) return `${mins} ${mins === 1 ? "minute" : "minutes"}`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} ${hours === 1 ? "hour" : "hours"}`;
  const days = Math.floor(hours / 24);
  const rest = hours % 24;
  const d = `${days} ${days === 1 ? "day" : "days"}`;
  return rest ? `${d} ${rest} ${rest === 1 ? "hour" : "hours"}` : d;
}

/** How long something has waited, from minutes; "" when the count is missing
 *  so no sentence ever says "waited 0 minutes" for a value nobody gave. */
export function waitedWords(minutes: number | null | undefined): string {
  if (typeof minutes !== "number" || !Number.isFinite(minutes) || minutes <= 0) return "";
  return longSpan(minutes * 60);
}

const CONNECTION_TIMEOUT = "Khipu couldn't reach the database just now (it took too long). It tries again at the next refresh.";

/** A raw CLI or Python error in the owner's words: what failed and what to do.
 *  Unknown text comes back unchanged apart from a trimmed traceback. */
export function plainError(text: string): string {
  const raw = (text ?? "").trim();
  if (!raw) return raw;
  if (/connection timeout expired|timeout expired/i.test(raw)) return CONNECTION_TIMEOUT;
  const layout = /\(layout (\d+); this version uses (\d+)\)/.exec(raw);
  if (layout) {
    return Number(layout[1]) > Number(layout[2])
      ? "This Mac's offline copy was made by a newer Khipu than the one running. It is rebuilt at the next check."
      : "This Mac's offline copy was made by an older Khipu than the one running. It is rebuilt at the next check.";
  }
  if (/model returned non-JSON/i.test(raw)) {
    return "The summariser sent back something Khipu couldn't read.";
  }
  if (/connection refused|could not connect|connection reset/i.test(raw)) {
    return "Khipu couldn't reach the database. It tries again at the next refresh.";
  }
  if (raw.includes("Traceback (most recent call last)")) {
    const lines = raw.split("\n").filter((l) => l.trim() !== "");
    return plainError((lines[lines.length - 1] ?? raw).trim());
  }
  return raw;
}

/** The callout cause for a capture that keeps failing: the last attempt's raw
 *  error and "N job(s) waiting M min — nothing is draining them" become what
 *  failed and what to do. Anything else passes through `plainError`. */
export function captureCauseWords(why: string): string {
  const parts = why
    .split(/;\s*/)
    .map((p) => p.trim())
    .filter(Boolean);
  const out: string[] = [];
  let failed = false;
  for (const part of parts) {
    if (/^last capture attempt failed/i.test(part)) {
      failed = true;
      continue;
    }
    const waiting = /^(\d+) job\(s\) waiting (\d+) min/i.exec(part);
    if (waiting) {
      const n = Number(waiting[1]);
      const span = waitedWords(Number(waiting[2]));
      out.push(`${n === 1 ? "A session is" : `${n} sessions are`} waiting to be saved${span ? `; ${n === 1 ? "it has" : "they have"} waited ${span}` : ""}`);
      continue;
    }
    out.push(plainError(part));
  }
  if (failed) out.unshift("The last session couldn't be saved: the summariser sent back something Khipu couldn't read");
  return out.join(". ");
}

/** The toast line: a Python traceback is its last line, and a CLI exit
 *  message becomes what failed. The full text stays in the toast's title. */
export function toastWords(text: string): string {
  const raw = (text ?? "").trim();
  const exited = /^khipu exited \d+:\s*(?:error:\s*)?(.*)$/is.exec(raw);
  const inner = plainError(exited ? exited[1] : raw);
  if (exited && inner === exited[1].trim()) {
    return "Khipu couldn't finish that. Try again in a moment; the details are in the tooltip.";
  }
  return inner;
}
