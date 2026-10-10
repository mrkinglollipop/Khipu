/** One relative-time and one clock format for the whole app. Callers append
 *  " ago" themselves: "45s ago", "5 min ago", "7 h ago", "9 days ago". */
export function fmtAge(s: number | null | undefined): string {
  if (s == null) return "an unknown time";
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  if (s < 172800) return `${Math.round(s / 3600)} h`;
  return `${Math.round(s / 86400)} days`;
}

/** Age of an ISO timestamp in `fmtAge`'s register; null when it will not parse. */
export function fmtAgeSince(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const t = new Date(String(iso).replace(" ", "T")).getTime();
  if (Number.isNaN(t)) return null;
  return fmtAge((Date.now() - t) / 1000);
}

/** Locale clock time, e.g. "9:06 AM". */
export function fmtClock(d: Date): string {
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}
