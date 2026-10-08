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
});
