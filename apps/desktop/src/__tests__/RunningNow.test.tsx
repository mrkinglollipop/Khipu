// Accounts › Running now (slice A5): read-only; a stale or absent Mac is said to be
// stale or absent, never "nothing running"; a job whose runner went quiet is "Not known";
// the last answer shows at once and a slow or failed refresh keeps it. Every value is made up.
(globalThis as unknown as { process: { env: Record<string, string> } }).process.env.TZ = "America/New_York";

import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RunningHere, RunningNow } from "../accounts/RunningNow";
import { headline, jobWords, parseRunning, useRunning } from "../accounts/running";
import type { RunJob, RunRunner, Running, RunMac } from "../accounts/running";

const invokeMock = vi.hoisted(() => vi.fn());
vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));

const NOW = new Date("2026-11-10T19:00:00Z"); // 2:00 PM in New York
const ago = (min: number) => new Date(NOW.getTime() - min * 60_000).toISOString();

const job = (over: Partial<RunJob> = {}): RunJob => ({
  summary: "Fix the paging test", now_line: "Running the tests", now_at: ago(1), state: "running", quiet: false, quiet_for_s: null,
  account_key: "claude|", account_label: "Claude", model: "Sonnet 5.5", started_at: ago(60), updated_at: ago(1), ended_at: null, ...over,
});
const runner = (over: Partial<RunRunner> = {}): RunRunner => ({
  name: "lanes", heartbeat_at: ago(1), silent: false, silent_since: null, last_finished_at: null, jobs: [job()], ...over,
});
const mac = (over: Partial<RunMac> = {}): RunMac => ({
  mac: "m5", name: "M5", this_mac: true, as_of: ago(0), stale: false, runners: [runner()], ...over,
});
const data = (macs: RunMac[], over: Partial<Running> = {}): Running => ({ updated_at: ago(1), macs, note: null, ...over });
const hook = (d: Running | null, unread = false) => ({ data: d, unread, answeredAt: d?.updated_at ? Date.parse(d.updated_at) : null });

const store = new Map<string, string>();
beforeEach(() => {
  invokeMock.mockReset();
  store.clear();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  });
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("words for a job", () => {
  it("names each state in plain words", () => {
    const r = runner();
    expect(jobWords(job({ state: "running" }), r, NOW).word).toBe("Running");
    expect(jobWords(job({ state: "waiting" }), r, NOW).word).toBe("Waiting");
    expect(jobWords(job({ state: "queued" }), r, NOW).word).toBe("Waiting to start");
    expect(jobWords(job({ state: "failed", ended_at: ago(5) }), r, NOW)).toMatchObject({ word: "Stopped with a problem", dot: "err" });
    expect(jobWords(job({ quiet: true, quiet_for_s: 22 * 60 }), r, NOW)).toMatchObject({ word: "No update for 22 min", dot: "warn" });
  });
  it("shows a state it does not know as Not known, never as running", () => {
    const parsed = parseRunning(JSON.stringify({ macs: [{ name: "M5", stale: false, runners: [{ name: "x", jobs: [{ summary: "s", state: "flying" }] }] }] }));
    expect(parsed?.macs[0].runners[0].jobs[0].state).toBe("unknown");
  });
  it("shows a live job on a silent runner as Not known even if the row says running", () => {
    const w = jobWords(job({ state: "running", now_line: "Running the tests", now_at: ago(30) }), runner({ silent: true }), NOW);
    expect(w).toMatchObject({ word: "Not known", unknown: true });
    expect(w.when).toBe("last said 1:30 pm");
  });
});

describe("the sentence above the tables", () => {
  it("counts live jobs on fresh Macs and names them", () => {
    expect(headline(data([mac(), mac({ mac: "m1", name: "M1", runners: [runner({ jobs: [job({ state: "waiting" })] })] })]))).toBe("2 jobs running or waiting on M5 and M1.");
  });
  it("does not count a silent runner's jobs as running", () => {
    expect(headline(data([mac({ runners: [runner({ silent: true })] })]))).toBe("Nothing is running right now on M5.");
  });
  it("says what is true when no runner reports: not that nothing is being worked on", () => {
    const h = headline(data([mac({ runners: [] })]));
    expect(h).toBe("No job runner is reporting on M5.");
    expect(h).not.toMatch(/nothing/i);
  });
  it("never says nothing is running about a stale Mac", () => {
    const h = headline(data([mac({ runners: [] }), mac({ mac: "m1", name: "M1", stale: true })]));
    expect(h).toContain("M1 hasn’t reported recently, so Khipu can’t tell what it is running.");
    expect(headline(data([mac({ name: "M1", stale: true })]))).toBe("M1 hasn’t reported recently, so Khipu can’t tell what it is running.");
    expect(headline(data([]))).toBe("No Mac has reported yet, so Khipu can’t tell what is running.");
  });
  it("treats a Mac with no stale field as stale", () => {
    const p = parseRunning(JSON.stringify({ macs: [{ name: "M5", runners: [] }] }));
    expect(p?.macs[0].stale).toBe(true);
  });
});

describe("Running now", () => {
  it("draws one table per Mac with runner groups and the plain states", () => {
    render(<RunningNow hook={hook(data([
      mac({ runners: [runner({ name: "lanes", jobs: [job(), job({ summary: "Check #504", state: "queued", model: null, now_line: null })] })] }),
      mac({ mac: "m1", name: "M1", this_mac: false, runners: [runner({ name: "nightly", jobs: [], last_finished_at: ago(35) })] }),
    ], { note: "No other Mac has reported yet, so only this Mac is shown." }))} now={NOW} />);
    expect(screen.getByRole("heading", { level: 2, name: "On M5" })).toBeVisible();
    expect(screen.getByRole("heading", { level: 2, name: "On M1" })).toBeVisible();
    expect(screen.getAllByRole("table")).toHaveLength(2);
    expect(screen.getByText("lanes job runner")).toBeVisible();
    expect(screen.getAllByText("Reporting, last heard 1 minute ago")).toHaveLength(2);
    expect(screen.getByText("Waiting to start")).toBeVisible();
    expect(screen.getByText("Nothing running. Last job finished at 1:25 pm.")).toBeVisible();
    expect(screen.getByText("No other Mac has reported yet, so only this Mac is shown.")).toBeVisible();
    expect(screen.getByText(/Updated 1 minute ago\./)).toBeVisible();
  });

  it("shows a stopped runner, what its job last said and when, as Not known", () => {
    render(<RunningNow hook={hook(data([mac({ runners: [runner({ name: "gear-tracker", silent: true, silent_since: ago(60), jobs: [job({ summary: "Import the list", state: "unknown", now_line: "Reading the gear list", now_at: ago(60) })] })] })]))} now={NOW} />);
    expect(screen.getByText("Stopped reporting at 1:00 pm")).toBeVisible();
    expect(screen.getByText("Khipu can’t tell if these are still running.")).toBeVisible();
    expect(screen.getByText(/start it again on M5/)).toBeVisible();
    expect(screen.getByText("Not known")).toBeVisible();
    expect(screen.getByText("Last said: Reading the gear list")).toBeVisible();
    expect(screen.getByText("last said 1:00 pm")).toBeVisible();
    expect(screen.queryByText("Running")).toBeNull();
  });

  it("says a stale Mac is out of date and draws no table of jobs for it", () => {
    render(<RunningNow hook={hook(data([mac({ runners: [] }), mac({ mac: "m1", name: "M1", stale: true, as_of: ago(300), runners: [runner()] })]))} now={NOW} />);
    expect(screen.getByText("M1 last reported 5 hours ago. Khipu can’t tell what is running there now.")).toBeVisible();
    expect(screen.queryAllByRole("table", { hidden: true })).toHaveLength(0);
    expect(screen.queryByText(/Nothing running/)).toBeNull();
  });

  it("empty state says no job runner is reporting on this Mac", () => {
    render(<RunningNow hook={hook(data([mac({ runners: [] })]))} now={NOW} />);
    expect(screen.getAllByText(/No job runner is reporting on M5\./).length).toBeGreaterThan(0);
    expect(screen.queryByText(/nothing is being worked on/i)).toBeNull();
  });

  it("a failed first read says it could not read, not that nothing runs", () => {
    render(<RunningNow hook={hook(null, true)} now={NOW} />);
    expect(screen.getByRole("status")).toHaveTextContent("Khipu couldn’t read what is running yet. It will try again.");
    expect(screen.queryByText(/Nothing/)).toBeNull();
  });

  it("is read-only: it has no button at all", () => {
    render(<RunningNow hook={hook(data([mac({ runners: [runner({ silent: true })] })]))} now={NOW} />);
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(screen.queryAllByRole("link")).toHaveLength(0);
  });

  it("uses none of the words the scope removes", () => {
    const { container } = render(<RunningNow hook={hook(data([mac({ runners: [runner({ silent: true }), runner({ name: "b", jobs: [] })] }), mac({ mac: "m1", name: "M1", stale: true })]))} now={NOW} />);
    expect(container.textContent ?? "").not.toMatch(/harness|\bhub\b|capture|episode|job slot|embedding|gateway|ledger|owed|\btier\b|\bhold\b|\bcap\b|override/i);
  });
});

describe("Running here now on the account page", () => {
  const d = data([
    mac({ runners: [runner({ jobs: [job({ summary: "Mine", account_key: "acct-1", account_label: "Claude" }), job({ summary: "Other", account_label: "Codex", account_key: "codex|" }), job({ summary: "Done one", state: "done", account_label: "Claude", account_key: "claude|" })] })] }),
    mac({ mac: "m1", name: "M1", stale: true, as_of: ago(300) }),
  ]);
  it("lists this account's live jobs from the same answer, with the Mac", () => {
    render(<RunningHere accountKey="claude|" accountLabel="Claude" hook={hook(d)} now={NOW} />);
    expect(screen.getByRole("heading", { name: "Running here now" })).toBeVisible();
    expect(screen.getByText("Mine")).toBeVisible();
    expect(screen.queryByText("Other")).toBeNull();
    expect(screen.queryByText("Done one")).toBeNull();
    expect(screen.getByText(/M1 last reported 5 hours ago/)).toBeVisible();
  });
  it("says Khipu can't see, not that nothing is using the account, when no runner reports", () => {
    render(<RunningHere accountKey="claude|" accountLabel="Claude" hook={hook(data([mac({ runners: [] })]))} now={NOW} />);
    expect(screen.getByText("No job runner is reporting, so Khipu can’t see what is using it.")).toBeVisible();
  });
});

describe("the last answer and the refresh", () => {
  const answer = (over: Partial<Running> = {}) => JSON.stringify({
    updated_at: new Date().toISOString(),
    macs: [{ mac: "m5", name: "M5", this_mac: true, as_of: new Date().toISOString(), stale: false, runners: [{ name: "lanes", heartbeat_at: null, silent: false, jobs: [{ summary: "Job one", state: "running" }] }] }], ...over,
  });
  function Probe({ active = true }: { active?: boolean }) {
    const h = useRunning(active);
    return <RunningNow hook={h} />;
  }

  it("shows the stored answer at once, before any read returns, and keeps it when the read fails", async () => {
    store.set("khipu.accounts.running.v1", answer());
    let release: () => void = () => {};
    invokeMock.mockImplementation(() => new Promise((_r, rej) => { release = () => rej(new Error("slow and broken")); }));
    render(<Probe />);
    expect(screen.getByText("Job one")).toBeVisible();
    await act(async () => { release(); });
    expect(screen.getByText("Job one")).toBeVisible();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(/couldn’t read|try again/i)).toBeNull();
  });

  it("replaces the answer with a newer one without clearing the screen in between", async () => {
    store.set("khipu.accounts.running.v1", answer());
    invokeMock.mockResolvedValue(answer({ macs: JSON.parse(answer()).macs.map((m: RunMac) => ({ ...m, runners: [{ name: "lanes", jobs: [{ summary: "Job two", state: "running" }] }] })) }));
    render(<Probe />);
    expect(screen.getByText("Job one")).toBeVisible();
    await waitFor(() => expect(screen.getByText("Job two")).toBeVisible());
    expect(invokeMock).toHaveBeenCalledWith("khipu_running");
  });

  it("reads again each minute while open, and not when closed", async () => {
    vi.useFakeTimers();
    invokeMock.mockResolvedValue(answer());
    const { rerender } = render(<Probe active={false} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(130_000); });
    expect(invokeMock).not.toHaveBeenCalled();
    rerender(<Probe active />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(invokeMock).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(61_000); });
    expect(invokeMock).toHaveBeenCalledTimes(2);
  });

  it("with nothing stored and a failed read, says it could not read", async () => {
    invokeMock.mockRejectedValue(new Error("no"));
    render(<Probe />);
    expect(await screen.findByText("Khipu couldn’t read what is running yet. It will try again.")).toBeVisible();
  });

  it("only ever calls the one read", async () => {
    invokeMock.mockResolvedValue(answer());
    render(<Probe />);
    await waitFor(() => expect(invokeMock).toHaveBeenCalled());
    expect(new Set(invokeMock.mock.calls.map((c) => c[0]))).toEqual(new Set(["khipu_running"]));
  });
});
