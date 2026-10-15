// Shared doctor→attention mapping (docs/plans/2026-09-05-setup-that-cannot-strand-you.md,
// "Finish, keys, harnesses, Docker"). Home (App.tsx) and Welcome's Finish step
// both need "one row per red check with its one action" — this used to live
// only inline in App.tsx's `loadDoctor`, so Finish had no way to reuse it and
// fell back to a second, thinner, action-less mapping (`CHECK_IN_WORDS`) that
// drifted out of sync with what Home actually shows. This is now the ONE
// place a red `*_ok` field becomes a plain-words title, cause and (when the
// app has an honest fix) a button.

import {
  aheadWords,
  brokenRules,
  capitalise,
  longDisagreements,
  quietRunners,
  runnerWords,
  pastWords,
  ruleWords,
  spanWords,
} from "./routing";
import type { RouteView } from "./routing";
import { fmtAgo } from "./time";

/** A named red check, in plain words, with at most one fix. Callers render
 *  one callout per entry; `fix` is omitted where the app has no honest fix to
 *  offer. */
export type Attention = {
  key: string;
  tone: "err" | "warn";
  title: string;
  cause: string;
  fix?: { label: string; kind: "reinstall-hook" | "recall-probe" | "revisions" | "open-harnesses" };
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
      if (h === "gateway") {
        items.push({
          key: "liveness:gateway",
          tone: "err",
          title: "Gateway is not reachable",
          cause: why
            ? `The gateway check failed: ${why}. Open Harnesses to inspect the gateway connection.`
            : "The gateway check failed. Open Harnesses to inspect the gateway connection.",
          fix: { label: "Open Harnesses", kind: "open-harnesses" },
        });
        continue;
      }
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
        "They were recorded while the database was unreachable and are queued on this Mac. After reconnecting, run `khipu outbox drain` to deliver them; nothing is lost meanwhile.",
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
    const cov = (parsed as { embed_coverage?: Record<string, { missing?: number; chunks?: number }>; }).embed_coverage;
    const top = cov as { missing?: unknown; stale?: unknown; pct?: unknown } | undefined;
    const topMissing = typeof top?.missing === "number" ? top.missing : 0;
    const topStale = typeof top?.stale === "number" ? top.stale : 0;
    const topPct = typeof top?.pct === "number" ? top.pct : null;
    const missing = topMissing > 0 || topStale > 0
      ? [topMissing > 0 ? `${topMissing} chunks missing` : "", topStale > 0 ? `${topStale} chunks stale` : "", topPct != null ? `${topPct}% covered` : ""].filter(Boolean).join(", ")
      : cov
      ? Object.entries(cov)
          .filter(([, v]) => v && typeof v === "object" && (v.missing ?? 0) > 0)
          .map(([k, v]) => `${v.missing} ${v.chunks != null ? `${k} chunks` : k}`)
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

  // D1: one nightly-step check per doctor key, each read from its own
  // nightly_steps[<key>] block (error + fix already worded by the CLI).
  const nightlySteps = (
    parsed as {
      nightly_steps?: Record<
        string,
        { ok?: boolean; error?: string | null; fix?: string | null }
      >;
    }
  ).nightly_steps;
  const NIGHTLY_STEP_TITLE: Record<string, string> = {
    notes_reconcile_ok: "The nightly's notes reconcile step did not run cleanly",
    embed_provider_ok: "The nightly's embedding backfill step did not run cleanly",
    commitments_hygiene_ok: "The nightly's commitments hygiene step did not run cleanly",
    mark_stale_ok: "The nightly's stale-commitments step did not run cleanly",
  };
  for (const [key, title] of Object.entries(NIGHTLY_STEP_TITLE)) {
    const step = nightlySteps?.[key];
    if (step && step.ok === false) {
      const why = step.error || "see the nightly log";
      issues.push(`${title}: ${why}`);
      items.push({
        key: key.replace(/_ok$/, ""),
        tone: "err",
        title,
        cause: step.fix ? `${why}. ${step.fix}.` : why,
      });
    }
  }

  if ((parsed as { topics_embed_lag_ok?: boolean }).topics_embed_lag_ok === false) {
    const lag = (parsed as { topics_embed_lag?: { lag_minutes?: number } }).topics_embed_lag;
    const mins = lag?.lag_minutes ?? 0;
    issues.push(`Topic search index ${mins} min behind`);
    items.push({
      key: "topics_embed_lag",
      tone: "warn",
      title: "A note has waited over an hour to become findable by meaning",
      cause: `The oldest unembedded topic has waited ${mins} minutes. It is still findable by its exact words meanwhile.`,
    });
  }

  if ((parsed as { degraded_rate_ok?: boolean }).degraded_rate_ok === false) {
    const dr = (parsed as { degraded_rate?: { rate?: number; degraded?: number; sampled?: number } }).degraded_rate;
    const pct = dr?.rate != null ? Math.round(dr.rate * 100) : 0;
    issues.push(`Search degraded ${pct}% of recent queries`);
    items.push({
      key: "degraded_rate",
      tone: "warn",
      title: "Search has been falling back to keyword-only often",
      cause: `${pct}% of the last ${dr?.sampled ?? 0} searches (${dr?.degraded ?? 0}) could not use meaning-based search — usually a missing or exhausted embedding budget.`,
    });
  }

  // The summariser: its provider can run (key present / CLI found), and it is
  // inside today's call ceiling. Captures wait in the queue either way.
  if ((parsed as { synth_provider_ok?: boolean }).synth_provider_ok === false) {
    const sp = (parsed as { synth_provider?: { provider?: string; binary?: string | null; auth?: string } })
      .synth_provider;
    const login = sp?.auth === "subscription" || sp?.provider === "claude-cli" || sp?.provider === "codex-cli";
    const bin = sp?.provider === "codex-cli" ? "codex" : "claude";
    issues.push(login ? `Summariser CLI ${bin} not found` : "Summariser has no key");
    items.push({
      key: "synth_provider",
      tone: "err",
      title: "The summariser cannot run",
      cause: login
        ? `The ${bin} command was not found on this Mac. Install it and sign in, or pick another summariser under Settings › Capture. Captures wait in the queue meanwhile.`
        : `No key is stored for the ${sp?.provider ?? "chosen"} summariser. Set it under Settings › Capture, next to the provider. Captures wait in the queue meanwhile.`,
    });
  }

  if ((parsed as { synth_budget_ok?: boolean }).synth_budget_ok === false) {
    const sb = (parsed as { synth_budget?: { calls?: number | null; cap?: number | null; error?: string } }).synth_budget;
    issues.push(sb?.error ? `Summariser budget unreadable: ${sb.error}` : "Summariser reached today's call ceiling");
    items.push({
      key: "synth_budget",
      tone: "warn",
      title: sb?.error ? "The summariser's call count cannot be read" : "The summariser reached today's call ceiling",
      cause: sb?.error
        ? `${sb.error}. Captures wait in the queue; nothing is lost.`
        : `${sb?.calls ?? "?"} of ${sb?.cap ?? "?"} calls used. Captures wait in the queue until UTC midnight, nothing is lost; lift it for today under Settings › Capture.`,
    });
  }

  const snap = (parsed as {
    hub_snapshot?: { ok?: boolean; reason?: string; age_seconds?: number | null; refresh_error?: { error?: string } | null };
  }).hub_snapshot;
  if (snap && snap.ok === false) {
    issues.push(`Offline copy is out of date${snap.reason ? `: ${snap.reason}` : ""}`);
    // Only a missing, overdue or failed full rebuild turns this red; being
    // behind the database between rebuilds shows on the health row instead.
    const why =
      snap.reason === "snapshot_refresh_failed"
        ? `The last full rebuild failed${snap.refresh_error?.error ? `: ${snap.refresh_error.error}` : ""}.`
        : snap.reason === "snapshot_refresh_overdue"
          ? `The nightly job rebuilds it in full and last did so ${fmtAgo(snap.age_seconds)}.`
          : snap.reason === "snapshot_missing"
            ? "There is none on this Mac yet; the nightly job builds it."
            : "It has no rebuild time on record.";
    items.push({
      key: "hub_snapshot",
      tone: "warn",
      title: "The offline copy of your memory is out of date",
      cause: `${why} Recall reads it first, and turns to the slower database once a full rebuild is more than a day old.`,
    });
  }

  return { issues, items };
}

// ---- Routing (docs/plans/2026-10-14-routing-truth.md, phase 4) -------------
// Read from `khipu route --json` (the app's `khipu_route_view`), not doctor,
// the same way the Plans item is read from `khipu usage`. Each item warns and
// blocks nothing: the record is guidance, not a gate, so every cause says
// Khipu stopped nothing.

/** The scope's four attention items: a standing rule may be broken, a job
 *  runner stopped reporting (or its files can't be read), sources disagreed
 *  for longer than the warn time, and a limit from a lane not matched to an
 *  account. Pure; `now` decides what counts as long. */
export function routingAttention(view: RouteView | null | undefined, now: Date = new Date()): Attention[] {
  if (!view || !view.record) return [];
  const items: Attention[] = [];
  const groups = brokenRules(view);
  if (groups.length) {
    // One rule, quoted once, then each runner or lane that breaks it, in
    // the approved mock's words: "Your rule says never a -fast tier. Your
    // Project A job runner's settings name ...". With more than one rule
    // broken, each is named by its number so the card's marks match.
    const lines = groups.map(({ rule: n, checks }) => {
      const rule = (view.rules ?? []).find((r) => r.n === n);
      const said = rule
        ? `Your rule${groups.length > 1 ? ` ${rule.n}` : ""} says ${ruleWords(rule.text)}. `
        : "";
      const under = checks.map((c) => {
        let what = c.text.replace(/^A standing rule may be broken: /, "").replace(/\.$/, "");
        // "Project A's settings name..." -> "Your Project A job runner's
        // settings name...", as the card and the mock name a runner.
        if (c.runner && what.startsWith(`${c.runner}'s `)) {
          what = `Your ${c.runner} job runner's ${what.slice(c.runner.length + 3)}`;
        }
        what = capitalise(what);
        // "its" only when the sentence has named the runner already.
        const owner = c.runner && !what.includes(c.runner) ? `your ${c.runner} job runner's` : "its";
        const file =
          c.file === "settings" ? ` in ${owner} settings file` : c.file === "events" ? ` in ${owner} events log` : "";
        const seen = c.at ? ` (seen ${pastWords(c.at, now)}${file})` : "";
        return `${what}${seen}.`;
      });
      return `${said}${under.join(" ")}`;
    });
    items.push({
      key: "routing:rules",
      tone: "warn",
      title: groups.length === 1 ? "A standing rule may be broken" : `${groups.length} standing rules may be broken`,
      cause: `${lines.join(" ")} Khipu stopped nothing; it only reports.`,
    });
  }
  for (const r of quietRunners(view)) {
    items.push({
      key: `routing:runner:${r.name}`,
      tone: "warn",
      title: r.silent ? "A job runner stopped reporting" : "Khipu can't read a job runner's files",
      cause: r.silent
        ? `${r.heartbeat_at ? `Your ${r.name} job runner's last report was ${pastWords(r.heartbeat_at, now)}.` : `Your ${r.name} job runner has not reported yet.`} Limits it finds won't reach your conversations until it runs again. Khipu stopped nothing.`
        : `${runnerWords(r, now)} Limits it finds won't reach your conversations until Khipu can read them again. Khipu stopped nothing.`,
    });
  }
  const span = spanWords(view.warn_after?.disagreement ?? 30 * 60);
  for (const d of longDisagreements(view, now)) {
    items.push({
      key: `routing:disagree:${d.label}`,
      tone: "warn",
      title: `Sources disagree about ${d.label}`,
      cause: `Sources have disagreed about ${d.label} for more than ${span}. The Routing card shows the stronger report and names both; conversations are told both.`,
    });
  }
  for (const u of view.unmatched ?? []) {
    items.push({
      key: `routing:lane:${u.runner}/${u.lane}`,
      tone: "warn",
      title: "A limit came from a lane not matched to an account",
      cause: `A limit came from ${u.runner}'s ${u.lane} lane, which isn't matched to an account${u.until ? `; it ends ${aheadWords(u.until, now)}` : ""}. Conversations are told the lane's name only, never a guessed account.`,
    });
  }
  return items;
}
