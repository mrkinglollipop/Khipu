// Slice A2: `khipu_health` as `khipu health --json` prints it. Fictional.
// Switches (set by a dev state): healthProblem, healthUnknown, healthNone
// (the check fails, with nothing stored yet).
import type { FakeHandler } from "./index";

const iso = (minAgo: number) => new Date(Date.now() - minAgo * 60000).toISOString();
const area = (key: string, name: string, link: string, line: string, extra: Record<string, unknown> = {}) => ({
  key, name, state: "ok", line, khipu_doing: "", you_do: "Nothing.", link, ...extra,
});

function health(flags: Record<string, unknown>) {
  const areas = [
    area("saving", "Saving memory", "where-memory-is-kept", "Captures are being saved."),
    area("finding", "Finding things", "search", "Search is up to date."),
    area("routing", "Routing", "accounts", "Your routing record is fresh on this Mac."),
    area("database", "Database and disk", "backups", "The database answers, the backup is recent and there is room on the disk."),
    area("jobs", "Jobs and other checks", "running-now", "2 jobs are running."),
  ];
  let jobs = 2;
  let headline = "Everything is working.";
  let silent: unknown[] = [];
  if (flags.healthProblem) {
    Object.assign(areas[0], { state: "warn", line: "M1 hasn’t saved memory since 9:40 this morning.", khipu_doing: "Khipu keeps trying, and keeps a copy on this Mac meanwhile. Nothing is lost.", you_do: "Nothing yet." });
    Object.assign(areas[4], { state: "warn", line: "A job runner, gear-tracker, has stopped reporting.", khipu_doing: "Khipu keeps checking.", you_do: "Nothing." });
    jobs = 1;
    silent = [{ name: "gear-tracker", mac: "M1", since: iso(60 * 20) }];
    headline = "Saving memory: M1 hasn’t saved memory since 9:40 this morning.";
  }
  if (flags.healthUnknown) {
    areas[1].state = "unknown";
    areas[2].state = "unknown";
  }
  return {
    checked_at: iso(2), mac: "M1", headline, areas, jobs_running: jobs, silent_runners: silent,
    free_gb: 188.0, last_backup_at: iso(60 * 9),
    agent_note: flags.healthProblem
      ? "Khipu health on M1. 2 of 5 areas need a look.\n[Saving memory] outbox_ok: nothing saved since 9:40.\n[Jobs and other checks] runner gear-tracker silent."
      : "Khipu health on M1. Nothing is failing.",
  };
}

export const todayFakes: FakeHandler = (cmd, _args, flags) => {
  if (cmd !== "khipu_health") return undefined;
  if (flags.healthNone) throw new Error("the check could not run");
  return health(flags);
};
