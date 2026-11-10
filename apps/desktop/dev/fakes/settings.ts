// Settings (slice A9): answers shaped as the CLI prints them. Fictional.
// Switches on window.__khipuFake (set by a dev state):
//   settings: "stale"  the other Mac last reported 40 minutes ago
//   settings: "one"    no other Mac has reported
//   settings: "problem" this Mac has an account to sign in again and a database problem
import type { FakeHandler } from "./index";

const iso = (min: number) => new Date(Date.now() - min * 60000).toISOString();
const AREAS = (db: "ok" | "err") => [
  { key: "saving", state: "ok" }, { key: "finding", state: "ok" }, { key: "routing", state: "ok" },
  { key: "database", state: db }, { key: "jobs", state: "ok" },
];
const row = (name: string, here: boolean, age: number, extra: Record<string, unknown> = {}) => ({
  mac: here ? "a".repeat(32) : "b".repeat(32), name, this_mac: here, version: "0.4.15 (aaaa1111)",
  seen_at: iso(age), age_s: age * 60, stale: age * 60 > 900, saving: true,
  health: { checked_at: iso(age), state: "ok", areas: AREAS("ok"), routing_version: "0123456789ab", free_gb: here ? 80 : 210.5, last_backup_at: iso(60 * 9) },
  running: { count: 2, silent: [] },
  signins: [{ harness: "claude", label: "Work", signed_in: true }, { harness: "codex", label: "Codex", signed_in: true }],
  ...extra,
});

let libsOn = false;

export const settingsFakes: FakeHandler = (cmd, args, flags) => {
  const mode = flags.settings as string | undefined;
  switch (cmd) {
    case "khipu_macs": {
      const me = row("M5", true, 0, mode === "problem" ? { signins: [{ harness: "claude", label: "Work", signed_in: true }, { harness: "codex", label: "Codex", signed_in: false }] } : {});
      const other = row("M1", false, mode === "stale" ? 40 : 1);
      return { checked_at: iso(0), macs: mode === "one" ? [me] : [me, other], shared: true, stale_after_s: 900, ...(mode === "one" ? { note: "No other Mac has reported yet, so only this Mac is shown." } : {}) };
    }
    case "khipu_health": {
      const bad = mode === "problem";
      return {
        checked_at: iso(0), mac: "M5",
        headline: bad ? "Database and disk: The database does not answer." : "Everything is working.",
        areas: [
          { key: "saving", name: "Saving memory", state: "ok", line: "Captures are being saved.", khipu_doing: "", you_do: "Nothing.", link: "where-memory-is-kept" },
          { key: "database", name: "Database and disk", state: bad ? "err" : "ok", line: bad ? "The database does not answer." : "The database answers, the backup is recent and there is room on the disk.", khipu_doing: bad ? "Khipu keeps trying and keeps a copy on this Mac meanwhile." : "", you_do: bad ? "Check this Mac's internet connection." : "Nothing.", link: "backups" },
        ],
        jobs_running: 2, free_gb: 80.0, last_backup_at: iso(60 * 9),
        agent_note: bad
          ? "Khipu health on M5. 1 of 5 areas need a look.\n[Database and disk] hub_ok: the database does not answer. Fix: check the connection with `khipu status`"
          : "Khipu health on M5. Nothing is failing.",
      };
    }
    case "khipu_accounts_status":
      return { ok: true, version: "v1", none: false, auto_track: true, value: null, accounts: [{ harness: "claude", label: "Work", home: null }, { harness: "codex", label: "Codex", home: "~/.codex" }], user_home: "/Users/you", declined: [], request: null };
    case "khipu_embed_profiles_list":
      return { profiles: [{ id: "gem@768", provider: "gemini", model: "gemini-embedding-2", dim: 768, is_active: true, coverage: { memory: { total: 1000, embedded: 990, missing: 10, stale: 0, pct: 99 } } }] };
    case "khipu_library_list":
      return { libraries: [{ name: "Bible", root: "/x", profile: "gem@768", enabled: libsOn, documents: 3, chunks: 100, embedded: 100, missing: 0, stale: 0, pct: 100 }, { name: "Notes", root: "/y", profile: "gem@768", enabled: libsOn, documents: 3, chunks: 100, embedded: 100, missing: 0, stale: 0, pct: 100 }] };
    case "khipu_library_enable": libsOn = true; return { ok: true };
    case "khipu_library_disable": libsOn = false; return { ok: true };
    case "run_khipu": {
      const a = (args?.args as string[] | undefined) ?? [];
      if (a[0] === "models") return { synth: { provider: "anthropic", endpoint: "", model_id: "claude-haiku-5-5" }, embed: { provider: "gemini", endpoint: "", model_id: "gemini-embedding-2" }, vision: { provider: "gemini", endpoint: "", model_id: "x" }, models_error: null };
      return undefined;
    }
    default:
      return undefined;
  }
};
