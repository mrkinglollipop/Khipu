// Slice A5: `khipu_running` as `khipu running --json` prints it. Fictional.
// Switches (set from a dev state in main.tsx), all on top of `running: true`:
//   runningSilent  a runner on M5 stopped reporting; its job is Not known
//   runningNone    no job runner reports on either Mac
//   runningStale   M1's last report is hours old
//   runningFail    the read fails (with nothing stored yet: "couldn't read")
import type { FakeHandler } from "./index";

const iso = (minAgo: number) => new Date(Date.now() - minAgo * 60000).toISOString();

const job = (summary: string, state: string, extra: Record<string, unknown> = {}) => ({
  summary, now_line: null, now_at: null, state, quiet: false, quiet_for_s: null,
  account_key: null, account_label: null, model: null,
  started_at: iso(60), updated_at: iso(1), ended_at: null, ...extra,
});

function running(flags: Record<string, unknown>) {
  const none = !!flags.runningNone;
  const m5Runners = none ? [] : [
    {
      name: "khipu-lanes", heartbeat_at: iso(1), silent: false, silent_since: null, last_finished_at: iso(190),
      jobs: [
        job("Build the running-work mocks", "running", { now_line: "Taking the screenshots at 1440 wide", now_at: iso(1), account_label: "Claude", account_key: "claude|", model: "Opus 5.5", started_at: iso(95) }),
        job("Check pull request #504", "running", { now_line: "Reading the change to the search check", now_at: iso(2), account_label: "Codex", account_key: "codex|", model: "gpt-6.1-sol", started_at: iso(75) }),
        job("Fix the paging test", "running", { quiet: true, quiet_for_s: 22 * 60, now_line: "Running the tests", now_at: iso(22), account_label: "Claude second", account_key: "claude|~/.claude-second", model: "Sonnet 5.5", started_at: iso(130) }),
        job("Look again at the search check", "queued", { account_label: "Grok", account_key: "grok|", started_at: iso(20) }),
      ],
    },
    ...(flags.runningSilent ? [{
      name: "gear-tracker", heartbeat_at: iso(60 * 20), silent: true, silent_since: iso(60 * 20), last_finished_at: null,
      jobs: [job("Import the backpack weight list", "unknown", { now_line: "Reading the gear list", now_at: iso(60 * 20), account_label: "Kimi", account_key: "kimi|", model: "K3.1 Pro", started_at: iso(60 * 21) })],
    }] : [{
      name: "church-slides", heartbeat_at: iso(2), silent: false, silent_since: null, last_finished_at: null,
      jobs: [job("Lay out Sunday’s slides", "running", { now_line: "Verse text set to 44 points", now_at: iso(3), account_label: "Claude second", account_key: "claude|~/.claude-second", model: "Sonnet 5.5", started_at: iso(100) })],
    }]),
  ];
  const m1Runners = none || flags.runningStale ? [] : [
    { name: "nightly", heartbeat_at: iso(1), silent: false, silent_since: null, last_finished_at: iso(35), jobs: [] },
  ];
  const macs = [
    { mac: "b".repeat(32), name: "M5", this_mac: true, as_of: iso(0), stale: false, runners: m5Runners },
    { mac: "a".repeat(32), name: "M1", this_mac: false, as_of: flags.runningStale ? iso(60 * 5) : iso(1), stale: !!flags.runningStale, runners: m1Runners },
  ];
  return { updated_at: iso(1), macs, counts: { running: 0, waiting: 0 }, shared: true };
}

export const runningFakes: FakeHandler = (cmd, _args, flags) => {
  if (cmd !== "khipu_running") return undefined;
  if (flags.runningFail) throw new Error("the read could not run");
  return running(flags);
};
