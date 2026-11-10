// Settings -> Optional features. The webview names a fixed Tauri command and
// passes a switch name; the CLI (faked here) owns the state, so every write is
// followed by a re-read and the screen shows what the re-read says.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { OptionalFeatures } from "../OptionalFeatures";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

const NAMES = ["routing_push"];

type Backend = {
  features: Record<string, { enabled: boolean; source: "env" | "file" | "default" }>;
  floor: { value: number; source: string; default: number };
  recallInstalled: boolean;
  recallRunning: boolean;
  refuse?: string;
};

let backend: Backend;
const calls: Array<[string, Record<string, unknown> | undefined]> = [];

function fresh(): Backend {
  return {
    features: Object.fromEntries(
      NAMES.map((n) => [n, { enabled: false, source: "default" }]),
    ) as Backend["features"],
    floor: { value: 0.65, source: "default", default: 0.65 },
    recallInstalled: false,
    recallRunning: false,
  };
}

beforeEach(() => {
  backend = fresh();
  calls.length = 0;
  invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
    calls.push([cmd, args]);
    switch (cmd) {
      case "khipu_features_show":
        return JSON.stringify({ features: backend.features, unknown: [] });
      case "khipu_feature_set": {
        if (backend.refuse) return JSON.stringify({ ok: false, error: backend.refuse });
        const name = String(args?.name);
        backend.features[name] = { enabled: Boolean(args?.enabled), source: "file" };
        return JSON.stringify({ features: backend.features, unknown: [] });
      }
      case "khipu_config_show":
        return JSON.stringify({ relevance_cosine_floor: backend.floor, config: {} });
      case "khipu_config_set":
        if (args?.key === "relevance.cosine_floor") {
          backend.floor = { value: Number(args.value), source: "file", default: 0.65 };
        }
        return JSON.stringify({ ok: true });
      case "khipu_config_unset":
        backend.floor = { value: 0.65, source: "default", default: 0.65 };
        return JSON.stringify({ ok: true });
      case "khipu_jobs_status":
        return JSON.stringify({ recall_daemon: { plist_loaded: backend.recallInstalled } });
      case "khipu_job_install":
        backend.recallInstalled = true;
        backend.recallRunning = true;
        return JSON.stringify({ ok: true, results: [{ ok: true }] });
      case "khipu_job_uninstall":
        backend.recallInstalled = false;
        backend.recallRunning = false;
        return JSON.stringify({ ok: true, results: [{ ok: true }] });
      case "khipu_recall_status":
        return JSON.stringify(
          backend.recallRunning ? { ok: true, served: 7, pid: 1 } : { ok: false, running: false },
        );
      default:
        throw new Error(`unexpected command ${cmd}`);
    }
  });
});

afterEach(() => {
  cleanup();
  invokeMock.mockReset();
});

function switchFor(label: RegExp) {
  return screen.getByRole("switch", { name: label });
}

describe("OptionalFeatures", () => {
  it("shows the recall service and the one routing switch", async () => {
    render(<OptionalFeatures active />);
    await waitFor(() => expect(screen.getAllByRole("switch")).toHaveLength(2));
    const order = screen.getAllByRole("switch").map((el) => el.id);
    expect(order).toEqual(["feat-recall-service", "feat-routing_push"]);
    expect(screen.queryByText("Measured, not recommended")).toBeNull();
    const routing = screen.getByText("Routing", { selector: ".section-head" }).closest(".section-card") as HTMLElement;
    expect(within(routing).getByRole("switch", { name: /Tell conversations which accounts are open/ })).not.toBeChecked();
    expect(within(routing).getByText(/never chooses where work goes/)).toBeInTheDocument();
    // R3-2: the start budget is a setting (up to 8 s), not a fixed second.
    expect(within(routing).getByText(/up to the routing start budget/)).toBeInTheDocument();
    expect(within(routing).queryByText(/at most one second/)).toBeNull();
    // Routing truth, phase 3b: the changes before each prompt, and their own budget.
    expect(within(routing).getByText(/only what changed since that conversation last heard/)).toBeInTheDocument();
    expect(within(routing).getByText(/up to the routing prompt budget before a prompt/)).toBeInTheDocument();
  });

  const ROUTING = /Tell conversations which accounts are open/;

  it("reflects what the CLI reports as on and off", async () => {
    backend.features.routing_push = { enabled: true, source: "file" };
    render(<OptionalFeatures active />);
    await waitFor(() => expect(switchFor(ROUTING)).toBeChecked());
  });

  it("toggling calls khipu_feature_set with the name and the new value, then re-reads", async () => {
    render(<OptionalFeatures active />);
    await waitFor(() => expect(switchFor(ROUTING)).not.toBeDisabled());
    const before = calls.filter(([c]) => c === "khipu_features_show").length;
    fireEvent.click(switchFor(ROUTING));
    await waitFor(() => expect(switchFor(ROUTING)).toBeChecked());
    expect(calls).toContainEqual(["khipu_feature_set", { name: "routing_push", enabled: true }]);
    expect(calls.filter(([c]) => c === "khipu_features_show").length).toBe(before + 1);
  });

  it("locks an environment-sourced switch and names the variable", async () => {
    backend.features.routing_push = { enabled: true, source: "env" };
    render(<OptionalFeatures active />);
    await waitFor(() => {
      expect(switchFor(ROUTING)).toBeDisabled();
      expect(switchFor(ROUTING)).toBeChecked();
    });
    expect(screen.getByText("KHIPU_FEATURE_ROUTING_PUSH")).toBeInTheDocument();
    fireEvent.click(switchFor(ROUTING));
    expect(calls.some(([c]) => c === "khipu_feature_set")).toBe(false);
  });

  it("shows the CLI's error text and leaves the switch where the CLI says it is", async () => {
    backend.refuse = "unknown feature 'routing_push'; known: [...]";
    render(<OptionalFeatures active />);
    await waitFor(() => expect(switchFor(ROUTING)).not.toBeDisabled());
    fireEvent.click(switchFor(ROUTING));
    expect(await screen.findByRole("alert")).toHaveTextContent("unknown feature 'routing_push'");
    expect(switchFor(ROUTING)).not.toBeChecked();
  });

  it("shows an error when the Tauri command itself fails", async () => {
    render(<OptionalFeatures active />);
    await waitFor(() => expect(switchFor(ROUTING)).not.toBeDisabled());
    const impl = invokeMock.getMockImplementation()!;
    invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
      if (cmd === "khipu_feature_set") throw "khipu exited 2: boom";
      return impl(cmd, args);
    });
    fireEvent.click(switchFor(ROUTING));
    expect(await screen.findByRole("alert")).toHaveTextContent("khipu exited 2: boom");
  });

  it("installs and removes the recall service through the job commands", async () => {
    render(<OptionalFeatures active />);
    await waitFor(() => expect(switchFor(/Keep recall warm/)).not.toBeDisabled());
    expect(switchFor(/Keep recall warm/)).not.toBeChecked();

    fireEvent.click(switchFor(/Keep recall warm/));
    await waitFor(() => expect(switchFor(/Keep recall warm/)).toBeChecked());
    expect(calls).toContainEqual(["khipu_job_install", { name: "recall_daemon" }]);
    expect(await screen.findByText("Running")).toBeInTheDocument();
    expect(screen.getByText(/answered 7 lookups/)).toBeInTheDocument();

    fireEvent.click(switchFor(/Keep recall warm/));
    await waitFor(() => expect(switchFor(/Keep recall warm/)).not.toBeChecked());
    expect(calls).toContainEqual(["khipu_job_uninstall", { name: "recall_daemon" }]);
  });

  it("says Not running when the job is installed but the service does not answer", async () => {
    backend.recallInstalled = true;
    render(<OptionalFeatures active />);
    expect(await screen.findByText("Not running")).toBeInTheDocument();
  });

  it("makes no call while inactive", () => {
    render(<OptionalFeatures active={false} />);
    expect(calls).toHaveLength(0);
  });
});
