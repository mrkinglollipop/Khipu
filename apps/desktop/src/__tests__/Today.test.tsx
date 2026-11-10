// Today (slice A2): the status sentence and the five checks panel.
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Today } from "../today/Today";
import { parseHealth, sentenceFor, type Health } from "../today/health";

const invokeMock = vi.hoisted(() => vi.fn());
vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));

const area = (key: string, name: string, state = "ok", extra: Record<string, string> = {}) => ({
  key, name, state, line: `${name} line.`, khipu_doing: "", you_do: "Nothing.", link: "accounts", ...extra,
});
const make = (over: Partial<Health> = {}): Health => ({
  checked_at: new Date(Date.now() - 120000).toISOString(), mac: "M1", headline: "Everything is working.",
  areas: [area("saving", "Saving memory"), area("finding", "Finding things"), area("routing", "Routing"), area("database", "Database and disk"), area("jobs", "Jobs and other checks", "ok", { link: "running-now" })] as Health["areas"],
  jobs_running: 2, silent_runners: [], free_gb: 80, last_backup_at: null, agent_note: "NOTE FOR AGENT", ...over,
});

// Nothing waits: Needs you has its own tests (NeedsCards.test.tsx).
const NEEDS = { data: { total: 0, items: [], more: 0, this_mac: "M1", last_answer: null, unread: [] }, failed: false, loading: false, reload: async () => null };

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
afterEach(cleanup);

describe("sentenceFor", () => {
  it("says everything works only when all five areas are ok", () => {
    expect(sentenceFor(make()).text).toBe("Everything is working.");
  });
  it("never calls an unknown area fine or a problem", () => {
    const h = make();
    h.areas[1].state = "unknown";
    const s = sentenceFor(h);
    expect(s.text).not.toBe("Everything is working.");
    expect(s.tone).toBe("unknown");
    expect(s.attention).toBe(0);
    expect(s.sub).toContain("Couldn't check: Finding things");
  });
  it("says it could not check when every area is unknown", () => {
    const h = make();
    h.areas.forEach((a) => { a.state = "unknown"; });
    expect(sentenceFor(h).text).toBe("Couldn't check how Khipu is doing.");
  });
  it("leads with the worst area and says what Khipu does", () => {
    const h = make();
    h.areas[0] = { ...h.areas[0], state: "warn", line: "M1 hasn't saved.", khipu_doing: "Khipu keeps trying." };
    h.areas[3] = { ...h.areas[3], state: "err", line: "The database does not answer.", khipu_doing: "Khipu retries." };
    const s = sentenceFor(h);
    expect(s.text).toBe("The database does not answer.");
    expect(s.tone).toBe("err");
    expect(s.sub).toBe("Khipu retries. Nothing for you to do.");
    expect(s.attention).toBe(2);
  });
  it("treats a state it does not know as unknown", () => {
    const h = parseHealth(JSON.stringify({ ...make(), areas: [{ ...area("saving", "Saving memory"), state: "great" }] }));
    expect(h?.areas[0].state).toBe("unknown");
  });
});

describe("Today", () => {
  it("shows the stored answer at once, with its time, before the check returns", async () => {
    localStorage.setItem("khipu.today.health.v1", JSON.stringify(make()));
    invokeMock.mockReturnValue(new Promise(() => {})); // a very slow check
    render(<Today active onOpen={() => {}} needs={NEEDS} />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Everything is working.");
    expect(screen.getByText(/Checked 2 minutes ago on M1/)).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });
  it("keeps the last answer and shows no error when the check fails", async () => {
    localStorage.setItem("khipu.today.health.v1", JSON.stringify(make()));
    invokeMock.mockRejectedValue(new Error("boom"));
    render(<Today active onOpen={() => {}} needs={NEEDS} />);
    await waitFor(() => expect(screen.getByText(/Couldn’t refresh just now/)).toBeTruthy());
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Everything is working.");
    expect(screen.queryByText(/boom/)).toBeNull();
  });
  it("opens the five checks, shows unknown as couldn't check, and copies the note", async () => {
    const h = make();
    h.areas[2].state = "unknown";
    invokeMock.mockResolvedValue(JSON.stringify(h));
    const write = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText: write } });
    render(<Today active onOpen={() => {}} needs={NEEDS} />);
    await screen.findByText(/Everything we could check is working/);
    fireEvent.click(screen.getByRole("button", { name: "See the five checks" }));
    expect(screen.getByRole("complementary", { name: "How Khipu is doing" })).toBeTruthy();
    expect(screen.getAllByText(/Couldn’t check/).length).toBeGreaterThan(0);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Copy a note for an agent" })); });
    expect(write).toHaveBeenCalledWith("NOTE FOR AGENT");
  });
  it("links Running now from the jobs area", async () => {
    invokeMock.mockResolvedValue(JSON.stringify(make()));
    const onOpen = vi.fn();
    render(<Today active onOpen={onOpen} needs={NEEDS} />);
    await screen.findByText("Checked 2 minutes ago on M1.");
    fireEvent.click(screen.getByRole("button", { name: "See the five checks" }));
    fireEvent.click(screen.getByRole("button", { name: "Open Running now" }));
    expect(onOpen).toHaveBeenCalledWith("running-now");
  });
});
