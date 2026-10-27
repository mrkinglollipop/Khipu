/** One relative-time and one clock format for the whole app. `fmtAge` is the
 *  bare duration ("45s", "5 min", "7 h", "9 days"); `fmtAgo` is the whole
 *  phrase ("45s ago", "just now"). An age is never negative: a time a little
 *  in the future (another machine's clock running ahead) reads "just now". */
export function fmtAge(s: number | null | undefined): string {
  if (s == null) return "an unknown time";
  s = Math.max(0, s);
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  if (s < 172800) return `${Math.round(s / 3600)} h`;
  return `${Math.round(s / 86400)} days`;
}

/** "45s ago", "2 days ago", or "just now" for anything under a second,
 *  including a negative age. */
export function fmtAgo(s: number | null | undefined): string {
  if (s != null && s < 1) return "just now";
  return `${fmtAge(s)} ago`;
}

/** A harness whose last capture is older than this is idle, not "Recording".
 *  The same week the stored recall probe uses (`MAX_PROBE_AGE_DAYS` in
 *  packages/cli/khipu/probe.py) before it reads as stale. Doctor itself never
 *  goes red on idleness, so this only changes the word, never the colour red. */
export const IDLE_CAPTURE_AFTER_S = 7 * 86400;

/** True when a harness's last capture is more than a week old. Unknown age
 *  is not stale: nothing is claimed either way. */
export function captureIsIdle(lastCapturedAgeS: number | null | undefined): boolean {
  return lastCapturedAgeS != null && Number.isFinite(lastCapturedAgeS) && lastCapturedAgeS > IDLE_CAPTURE_AFTER_S;
}

/** `fmtAgo` for an ISO timestamp aged against this Mac's clock; null when it
 *  will not parse. Use it only for times this Mac wrote, or where no
 *  hub-side age exists. */
export function fmtAgoSince(iso: string | null | undefined, nowMs: number = Date.now()): string | null {
  if (!iso) return null;
  const t = new Date(String(iso).replace(" ", "T")).getTime();
  if (Number.isNaN(t)) return null;
  return fmtAgo((nowMs - t) / 1000);
}

/** An age the hub counted on its own clock (`ageAtRead`, seconds at the
 *  read) plus the seconds since this Mac read it, so the two clocks never
 *  meet in one subtraction. Falls back to `fmtAgoSince(iso)` when the hub
 *  sent no age. */
export function fmtAgoFromRead(
  ageAtRead: number | null | undefined,
  readAtMs: number | null | undefined,
  iso: string | null | undefined,
  nowMs: number = Date.now(),
): string | null {
  if (ageAtRead == null || !Number.isFinite(ageAtRead)) return fmtAgoSince(iso, nowMs);
  const since = readAtMs != null ? Math.max(0, (nowMs - readAtMs) / 1000) : 0;
  return fmtAgo(ageAtRead + since);
}

/** Locale clock time, e.g. "9:06 AM". */
export function fmtClock(d: Date): string {
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}
