// Shared doctor→attention mapping (docs/plans/2026-09-05-setup-that-cannot-strand-you.md,
// "Finish, keys, harnesses, Docker"). Home (App.tsx) and Welcome's Finish step
// both need "one row per red check with its one action" — this used to live
// only inline in App.tsx's `loadDoctor`, so Finish had no way to reuse it and
// fell back to a second, thinner, action-less mapping (`CHECK_IN_WORDS`) that
// drifted out of sync with what Home actually shows. This is now the ONE
// place a red `*_ok` field becomes a plain-words title, cause and (when the
// app has an honest fix) a button.

/** A named red check, in plain words, with at most one fix. Callers render
 *  one callout per entry; `fix` is omitted where the app has no honest fix to
 *  offer. */
export type Attention = {
  key: string;
  tone: "err" | "warn";
  title: string;
  cause: string;
  fix?: { label: string; kind: "reinstall-hook" | "recall-probe" | "revisions" };
  harness?: string;
};

export const HARNESS_LABEL: Record<string, string> = {
  claude_code: "Claude Code",
  cursor: "Cursor",
  aegis: "Aegis",
  codex: "Codex",
  grok_bot: "Grok Bot",
};

export function harnessLabel(id: string): string {
  return HARNESS_LABEL[id] ?? id;
}

type LivenessPayload = {
  ok?: boolean;
  red?: string[];
  harnesses?: Record<string, { reasons?: string[] }>;
};

/** Builds the same `issues` (one short line per red check, for a plain-text
 *  summary) and `items` (one `Attention` per red check, for the Home
 *  callouts and the Finish step's rows) that `App.tsx`'s `loadDoctor` used to
 *  build inline. Pure and side-effect free: callers still own their own
 *  `setX` calls for the other doctor fields (embed coverage, jobs, ...) this
 *  function does not touch. */
export function buildAttention(parsed: Record<string, unknown>): {
  issues: string[];
  items: Attention[];
} {
  const issues: string[] = [];
  const items: Attention[] = [];

  const lv = (parsed as { capture_liveness?: LivenessPayload }).capture_liveness;
  if (lv && lv.ok === false) {
    for (const h of lv.red ?? []) {
      const why = (lv.harnesses?.[h]?.reasons ?? []).join("; ");
      issues.push(`Not recording ${h}: ${why || "see report"}`);
      items.push({
        key: `liveness:${h}`,
        tone: "err",
        title: `${harnessLabel(h)} has stopped recording sessions`,
        cause: why
          ? `${why}. Reinstalling the hook puts it back without losing the session.`
          : "Its capture hook is no longer reporting. Reinstalling the hook puts it back without losing the session.",
        fix: { label: "Reinstall hook", kind: "reinstall-hook" },
        harness: h,
      });
    }
  }

  const gs = (parsed as { git_sync?: { ok?: boolean; reasons?: string[] } }).git_sync;
  if (gs && gs.ok === false) {
    const why = (gs.reasons ?? []).join("; ");
    issues.push(`Git sync not landing: ${why || "see report"}`);
    items.push({
      key: "git_sync",
      tone: "warn",
      title: "The nightly copy of your notes is not reaching GitHub",
      cause: why
        ? `${why}. Everything is still in the database; only the off-site copy of the note files is behind.`
        : "Everything is still in the database; only the off-site copy of the note files is behind.",
    });
  }

  if ((parsed as { drift_ok?: boolean }).drift_ok === false) {
    issues.push("Out-of-sync files");
    items.push({
      key: "drift",
      tone: "warn",
      title: "Some note files no longer match the database",
      cause:
        "A file was edited in two places, so one version is not the one search returns. Conflicting edits lists them.",
      fix: { label: "Conflicting edits", kind: "revisions" },
    });
  }

  if ((parsed as { graph_drift_ok?: boolean }).graph_drift_ok === false) {
    issues.push("Graph mirror drift");
    items.push({
      key: "graph_drift",
      tone: "warn",
      title: "The connections index is behind its source",
      cause:
        "The graph builder's own copy and the database disagree. The next graph build reconciles them; run it from the health report below.",
    });
  }

  if ((parsed as { outbox_ok?: boolean }).outbox_ok === false) {
    issues.push("Captures waiting to sync");
    items.push({
      key: "outbox",
      tone: "warn",
      title: "Some captures are still waiting to reach the database",
      cause:
        "They were recorded while the database was unreachable and are queued on this Mac. Refresh retries them; nothing is lost meanwhile.",
    });
  }

  if ((parsed as { backup_ok?: boolean }).backup_ok === false) {
    issues.push("Backup test");
    items.push({
      key: "backup",
      tone: "err",
      title: "The newest backup is older than it should be",
      cause: "Backups run on a schedule off this Mac. Until one lands, a restore would lose recent sessions.",
    });
  }

  if ((parsed as { graph_backup_ok?: boolean }).graph_backup_ok === false) {
    issues.push("Graph snapshot");
    items.push({
      key: "graph_backup",
      tone: "warn",
      title: "The connections snapshot is stale",
      cause: "The last saved copy of the connections index is older than its limit.",
    });
  }

  if ((parsed as { graph_offsite_ok?: boolean }).graph_offsite_ok === false) {
    issues.push("Graph offsite");
    items.push({
      key: "graph_offsite",
      tone: "warn",
      title: "The off-site copy of the connections index is stale",
      cause: "The most recent copy sent off this Mac is older than its limit.",
    });
  }

  if ((parsed as { index_freshness_ok?: boolean }).index_freshness_ok === false) {
    issues.push("Memory index stale vs nightly");
    items.push({
      key: "index_freshness",
      tone: "warn",
      title: "The memory index has not been rebuilt since the last nightly run",
      cause: "Recall still works; the summary index an agent reads first is a day behind.",
    });
  }

  if ((parsed as { embed_coverage_ok?: boolean }).embed_coverage_ok === false) {
    issues.push("Search index catching up");
    const cov = (parsed as { embed_coverage?: Record<string, { missing?: number }> }).embed_coverage;
    const missing = cov
      ? Object.entries(cov)
          .filter(([, v]) => v && typeof v === "object" && (v.missing ?? 0) > 0)
          .map(([k, v]) => `${v.missing} ${k}`)
          .join(", ")
      : "";
    items.push({
      key: "embed_coverage",
      tone: "warn",
      title: "The search index is behind",
      cause: missing
        ? `${missing} not indexed yet. They are still findable by their exact words; search by meaning misses them until the nightly catches up.`
        : "Some rows are not indexed yet. They are still findable by their exact words until the nightly catches up.",
    });
  }

  // Every red field doctor aggregates into `ok` needs a named issue here, or
  // the card says "Issues found" over an empty list and the person is sent to
  // the raw JSON to find out what broke (audit 2026-09-04).
  if ((parsed as { recall_probe_ok?: boolean }).recall_probe_ok === false) {
    const rp = (parsed as { recall_probe?: { reason?: string; error?: string } }).recall_probe;
    const why = rp?.reason || rp?.error;
    issues.push(`Recall probe failed or is stale: run a probe${why ? ` (${why})` : ""}`);
    items.push({
      key: "recall_probe",
      tone: "err",
      title: "Nothing has proved that recall works end to end lately",
      cause: why
        ? `${why}. The probe records a session, searches for it and removes it again.`
        : "The check that records a session, searches for it and removes it again has not run in the last seven days.",
      fix: { label: "Run recall probe", kind: "recall-probe" },
    });
  }

  if ((parsed as { bundle_seal_ok?: boolean }).bundle_seal_ok === false) {
    issues.push("App bundle signature is broken: reinstall from the DMG");
    items.push({
      key: "bundle_seal",
      tone: "err",
      title: "This copy of Khipu has been altered since it was signed",
      cause: "macOS will refuse to open it after the next restart. Reinstall from the downloaded disk image to repair it.",
    });
  }

  if ((parsed as { dsn_file_ok?: boolean }).dsn_file_ok === false) {
    issues.push("Database connection file missing or unreadable");
    items.push({
      key: "dsn_file",
      tone: "err",
      title: "The saved database connection cannot be read",
      cause: "Harnesses that cannot reach the Keychain fall back to this file. Set the connection again under Settings.",
    });
  }

  const snap = (parsed as { hub_snapshot?: { ok?: boolean; reason?: string } }).hub_snapshot;
  if (snap && snap.ok === false) {
    issues.push(`Offline copy is stale${snap.reason ? `: ${snap.reason}` : ""}`);
    items.push({
      key: "hub_snapshot",
      tone: "warn",
      title: "The offline copy of your memory is behind",
      cause: "It is what recall falls back to when the database is unreachable. Refreshing it is a Settings action.",
    });
  }

  return { issues, items };
}
