/** Post-update notices — one-time messages shown after an in-app update lands
 * on a version that changed behavior a user needs to act on (not every
 * release; most versions add nothing here). Persisted so a notice is shown
 * once per install, not on every launch (see `khipu.lastNoticedVersion`).
 */

export type PostUpdateNotice = {
  /** The app version this notice targets — shown once when the running
   * version crosses into (previouslyNoticed, thisVersion]. */
  version: string;
  title: string;
  body: string;
  /** "integrations" wires an "Open Integrations" button that switches the
   * app to the Integrations pane. Omit for a plain "Got it" notice. */
  action?: "integrations";
};

export const POST_UPDATE_NOTICES: PostUpdateNotice[] = [
  {
    version: "0.3.16",
    title: "Cursor users: re-install the Khipu pack once",
    body:
      "This version changed the Cursor recall rule (hybrid search default, khipu_owed). " +
      "Hooks and the MCP server updated automatically with the app, but Cursor's " +
      "per-project rule file only refreshes when you re-run Install on the Integrations " +
      "pane for each project.",
    action: "integrations",
  },
];

const STORAGE_KEY = "khipu.lastNoticedVersion";

/** localStorage may be unavailable (private mode, disabled site data); every
 * access here is best-effort and never throws. */
export function readLastNoticedVersion(): string {
  try {
    return window.localStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function writeLastNoticedVersion(version: string): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, version);
  } catch {
    // best effort — a missed write just means the notice may show again
  }
}

function parseVersion(v: string): number[] {
  return v.split(".").map((part) => parseInt(part, 10) || 0);
}

/** True when `a` is a strictly newer version than `b`. Plain numeric
 * dot-segment comparison — Khipu versions are always `MAJOR.MINOR.PATCH`. */
export function versionGreaterThan(a: string, b: string): boolean {
  const pa = parseVersion(a);
  const pb = parseVersion(b);
  const len = Math.max(pa.length, pb.length);
  for (let i = 0; i < len; i++) {
    const x = pa[i] ?? 0;
    const y = pb[i] ?? 0;
    if (x !== y) return x > y;
  }
  return false;
}

/** The single notice to show for this launch, or null. `storedVersion` is
 * `readLastNoticedVersion()`'s return (empty string when never set — treated
 * as "older than everything", so an upgrade from a pre-feature release still
 * surfaces any notice up to the current version). When more than one notice
 * falls in (storedVersion, currentVersion], the highest-versioned one wins —
 * only one dialog shows per launch. */
export function noticeForUpgrade(
  storedVersion: string,
  currentVersion: string,
): PostUpdateNotice | null {
  if (!currentVersion || !versionGreaterThan(currentVersion, storedVersion)) {
    return null;
  }
  const eligible = POST_UPDATE_NOTICES.filter(
    (n) =>
      versionGreaterThan(n.version, storedVersion) &&
      !versionGreaterThan(n.version, currentVersion),
  );
  if (eligible.length === 0) return null;
  return eligible.reduce((best, n) => (versionGreaterThan(n.version, best.version) ? n : best));
}
