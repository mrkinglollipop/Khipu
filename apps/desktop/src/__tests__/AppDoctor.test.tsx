// The rail's health line and the doctor read behind it, driven through App
// with a scripted `run_khipu`: a doctor answer that arrives late must not
// overwrite a newer one, a failed Claude homes check must not read green, and
// Home's "Reinstall hook" installs without the CLI's verify.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import App from "../App";
import { WELCOME_DONE_KEY } from "../Welcome";

const invokeMock = vi.fn();

vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));
vi.mock("@tauri-apps/plugin-dialog", () => ({
  open: vi.fn(async () => null),
  save: vi.fn(async () => null),
}));
vi.mock("@tauri-apps/api/path", () => ({
  resourceDir: vi.fn(async () => "/Applications/Khipu.app/Contents/Resources"),
}));
vi.mock("@tauri-apps/api/app", () => ({
  getVersion: vi.fn(async () => "0.4.0"),
}));
vi.mock("@tauri-apps/plugin-updater", () => ({
  check: vi.fn(async () => null),
}));
vi.mock("@tauri-apps/plugin-process", () => ({
  relaunch: vi.fn(async () => {}),
}));

beforeEach(() => {
  invokeMock.mockReset();
  const store = new Map<string, string>([[WELCOME_DONE_KEY, "1"]]);
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const LIVE = { ok: true, red: [], harnesses: { claude_code: { ok: true, seen: true, captures: 3 } } };
const GOOD_HOMES = { homes: [{ exists: true, installed: true }] };

function doctorDoc(over: Record<string, unknown> = {}): string {
  return JSON.stringify({ ok: true, capture_liveness: LIVE, claude_homes: GOOD_HOMES, ...over });
}

/** Routes `run_khipu` by its argv; anything else gets the harmless default. */
function script(khipu: (args: string[]) => Promise<string> | string) {
  invokeMock.mockImplementation(async (cmd: string, payload?: { args?: string[] }) => {
    if (cmd === "run_khipu") return khipu(payload?.args ?? []);
    if (cmd === "dsn_configured") return true;
    return JSON.stringify({ ok: true });
  });
}

const railText = () => document.querySelector(".rail-health")?.textContent ?? "";

describe("App — doctor reads", () => {
  it("routes a failed gateway through its diagnostics instead of reinstalling a hook", async () => {
    const gateway = {
      ok: false,
      red: ["gateway"],
      harnesses: { gateway: { ok: false, seen: true, reasons: ["token rejected"] } },
    };
    script((args) => args[0] === "doctor" ? doctorDoc({ capture_liveness: gateway }) : "{}");
    render(<App />);
    expect(await screen.findByText("Gateway is not reachable")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reinstall hook" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open Harnesses" }));
    expect(screen.getByRole("heading", { name: "Harnesses" })).toBeInTheDocument();
  });

  it("keeps configured connection presence separate from a failed database probe", async () => {
    invokeMock.mockImplementation(async (cmd: string, payload?: { args?: string[] }) => {
      if (cmd === "dsn_configured") return true;
      if (cmd === "khipu_db_status") return JSON.stringify({ ok: false, error: "connection refused" });
      if (cmd === "run_khipu" && payload?.args?.[0] === "doctor") return doctorDoc();
      return JSON.stringify({ ok: true });
    });
    render(<App />);
    expect((await screen.findAllByText("Not reachable")).length).toBeGreaterThan(0);
    expect(screen.queryByText("Connected")).not.toBeInTheDocument();
  });

  it("rechecks the database connection when asked", async () => {
    let statusReads = 0;
    invokeMock.mockImplementation(async (cmd: string, payload?: { args?: string[] }) => {
      if (cmd === "dsn_configured") return true;
      if (cmd === "khipu_db_status") {
        statusReads += 1;
        return JSON.stringify({ ok: true });
      }
      if (cmd === "liveness_now") return JSON.stringify({ ok: true, red: [], harnesses: {} });
      if (cmd === "run_khipu" && payload?.args?.[0] === "doctor") return doctorDoc();
      return JSON.stringify({ ok: true });
    });
    render(<App />);
    await waitFor(() => expect(statusReads).toBeGreaterThan(0));
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    const before = statusReads;
    fireEvent.click(await screen.findByRole("button", { name: "Recheck connection" }));
    await waitFor(() => expect(statusReads).toBeGreaterThan(before));
  });

  it("does not turn a failed live-session response into an empty successful card", async () => {
    invokeMock.mockImplementation(async (cmd: string, payload?: { args?: string[] }) => {
      if (cmd === "dsn_configured") return true;
      if (cmd === "khipu_db_status") return JSON.stringify({ ok: true });
      if (cmd === "liveness_now") return JSON.stringify({ ok: false, error: "hub unavailable", red: [], harnesses: {} });
      if (cmd === "run_khipu" && payload?.args?.[0] === "doctor") return doctorDoc();
      return JSON.stringify({ ok: true });
    });
    render(<App />);
    expect(await screen.findByText("Could not check live sessions")).toBeInTheDocument();
    expect(screen.queryByText("No live sessions yet")).not.toBeInTheDocument();
  });

  it("does not count unseen harnesses as recording or call an all-unseen report clear", async () => {
    script((args) => args[0] === "doctor"
      ? doctorDoc({ capture_liveness: { ok: true, red: [], harnesses: { claude_code: { ok: true, seen: false } } } })
      : "{}");
    render(<App />);
    expect(await screen.findByText("0 of 1")).toBeInTheDocument();
    expect(screen.queryByText("Everything is recording and in sync")).not.toBeInTheDocument();
  });

  it("counts only proven harnesses in a mixed liveness report", async () => {
    script((args) => args[0] === "doctor"
      ? doctorDoc({ capture_liveness: { ok: true, red: [], harnesses: {
        claude_code: { ok: true, seen: true },
        codex: { ok: true, seen: false },
        cursor: { ok: true, seen: false },
      } } })
      : "{}");
    render(<App />);
    expect(await screen.findByText("1 of 3")).toBeInTheDocument();
    expect(screen.queryByText("Everything is recording and in sync")).not.toBeInTheDocument();
  });

  it("marks retained doctor data stale when its refresh fails", async () => {
    let fail = false;
    script((args) => {
      if (args[0] !== "doctor") return "{}";
      if (fail) throw new Error("hub unavailable");
      return doctorDoc();
    });
    render(<App />);
    await screen.findByText("Everything is recording and in sync");
    fail = true;
    fireEvent.click(screen.getAllByRole("button", { name: "Refresh" })[0]);
    expect(await screen.findByText("Health check failed")).toBeInTheDocument();
    expect(screen.queryByText("Everything is recording and in sync")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss error" }));
    expect(screen.getByText("Health check failed")).toBeInTheDocument();
  });

  it("shows a loading live-session read instead of an empty result", async () => {
    let resolveLive: ((value: string) => void) | undefined;
    invokeMock.mockImplementation(async (cmd: string, payload?: { args?: string[] }) => {
      if (cmd === "dsn_configured") return true;
      if (cmd === "khipu_db_status") return JSON.stringify({ ok: true });
      if (cmd === "liveness_now") return new Promise<string>((resolve) => { resolveLive = resolve; });
      if (cmd === "run_khipu" && payload?.args?.[0] === "doctor") return doctorDoc();
      return JSON.stringify({ ok: true });
    });
    render(<App />);
    expect(await screen.findByText("Checking live sessions")).toBeInTheDocument();
    expect(screen.queryByText("No live sessions yet")).not.toBeInTheDocument();
    resolveLive?.(JSON.stringify({ ok: true, red: [], harnesses: {} }));
  });
  it("ignores a doctor answer that arrives after a newer one", async () => {
    const doctorReleases: Array<(v: string) => void> = [];
    script((args) => {
      if (args[0] === "doctor") return new Promise<string>((resolve) => doctorReleases.push(resolve));
      return "{}";
    });
    render(<App />);
    await waitFor(() => expect(doctorReleases).toHaveLength(1));

    fireEvent.click(screen.getAllByRole("button", { name: "Refresh" })[0]);
    await waitFor(() => expect(doctorReleases.length).toBeGreaterThan(1));
    await new Promise((r) => setTimeout(r, 30));

    const newest = doctorReleases.length - 1;
    doctorReleases[newest](doctorDoc());
    await waitFor(() => expect(railText()).toBe("All harnesses recording"));

    for (const release of doctorReleases.slice(0, newest)) {
      release(doctorDoc({ claude_homes: { homes: [], error: "ImportError: nope" } }));
    }
    await new Promise((r) => setTimeout(r, 30));
    expect(railText()).toBe("All harnesses recording");
  });

  it("warns on the rail when the Claude homes check failed", async () => {
    script((args) => (args[0] === "doctor" ? doctorDoc({ claude_homes: { homes: [], error: "ImportError: nope" } }) : "{}"));
    render(<App />);
    await waitFor(() => expect(railText()).toBe("Couldn't check Claude homes"));
    expect(document.querySelector(".rail-health .hdot")).toHaveClass("warn");
  });

  it("keeps the old rail when doctor has no Claude homes block", async () => {
    script((args) => {
      if (args[0] !== "doctor") return "{}";
      return JSON.stringify({ ok: true, capture_liveness: LIVE });
    });
    render(<App />);
    await waitFor(() => expect(railText()).toBe("All harnesses recording"));
  });

  it("opens Recall with the exact via:t3 filter from the T3 card", async () => {
    const calls: string[][] = [];
    script((args) => {
      calls.push(args);
      if (args[0] === "doctor") {
        return doctorDoc({
          t3: {
            detected: true,
            lookup: { ok: true },
            thread_linked_captures_today: 1,
            warnings: [],
          },
        });
      }
      if (args[0] === "integrations" && args[1] === "status") {
        return JSON.stringify([{ harness: "claude_code", detected: true, mcp: true, hook_stop: true, hook_precompact: true, recall_rule: "installed" }]);
      }
      if (args[0] === "search") return JSON.stringify({ results: [] });
      return "{}";
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Harnesses" }));
    fireEvent.click(await screen.findByRole("button", { name: "Search T3 captures" }));

    expect(screen.getByRole("button", { name: "Recall" })).toHaveAttribute("aria-current", "page");
    const query = await screen.findByRole("textbox", { name: "Search" });
    expect(query).toHaveFocus();
    fireEvent.change(query, { target: { value: "open commitments" } });
    fireEvent.keyDown(query, { key: "Enter" });
    await waitFor(() => expect(calls).toContainEqual(["search", "--mode", "hybrid", "--limit", "20", "--via=t3", "--", "open commitments"]));
  });
});

describe("App — Home's Reinstall hook", () => {
  const RED = {
    ok: false,
    red: ["claude_code"],
    harnesses: { claude_code: { ok: false, seen: true, captures: 3, reasons: ["hook silent"] } },
  };

  it("installs without verify, verifies separately, and reports a failed verify as one", async () => {
    const seen: string[][] = [];
    script((args) => {
      if (args[0] === "doctor") return doctorDoc({ capture_liveness: RED });
      if (args[0] === "integrations") seen.push(args);
      if (args[1] === "install") return '[{"harness":"claude_code","detected":true,"changes":[]}]';
      if (args[1] === "verify") {
        return '[{"harness":"claude_code","detected":true,"ok":false,"components":{"hook":{"ok":false,"error":"Stop hook not found"}}}]';
      }
      return "{}";
    });
    render(<App />);
    const buttons = await screen.findAllByRole("button", { name: "Reinstall hook" });
    fireEvent.click(buttons[0]);

    expect(await screen.findByText(/Installed; verify failed: claude_code hook: Stop hook not found/)).toBeInTheDocument();
    expect(seen).toEqual([
      ["integrations", "install", "claude_code", "--no-verify"],
      ["integrations", "verify", "claude_code"],
    ]);
    expect(screen.queryByText(/install failed/)).not.toBeInTheDocument();
  });

  it("shows the CLI's error when the install did not finish", async () => {
    script((args) => {
      if (args[0] === "doctor") return doctorDoc({ capture_liveness: RED });
      if (args[1] === "install") return '[{"harness":"claude_code","ok":false,"aborted":true,"error":"bad json"}]';
      return "{}";
    });
    render(<App />);
    fireEvent.click((await screen.findAllByRole("button", { name: "Reinstall hook" }))[0]);
    expect(await screen.findByText("install failed: bad json")).toBeInTheDocument();
  });

  it("keeps a health-check failure beside the reinstall result", async () => {
    let failDoctor = false;
    script((args) => {
      if (args[0] === "doctor") {
        if (failDoctor) throw new Error("hub unreachable");
        return doctorDoc({ capture_liveness: RED });
      }
      if (args[1] === "install") return '[{"harness":"claude_code","detected":true,"changes":[]}]';
      if (args[1] === "verify") {
        return '[{"harness":"claude_code","detected":true,"ok":false,"components":{"hook":{"ok":false,"error":"Stop hook not found"}}}]';
      }
      return "{}";
    });
    render(<App />);
    const button = (await screen.findAllByRole("button", { name: "Reinstall hook" }))[0];
    failDoctor = true;
    fireEvent.click(button);
    const toast = await screen.findByText(/The health check then failed:/);
    expect(toast).toHaveTextContent("Installed; verify failed: claude_code hook: Stop hook not found");
    expect(toast).toHaveTextContent("hub unreachable");
  });
});
