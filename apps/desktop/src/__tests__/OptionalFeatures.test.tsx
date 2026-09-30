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

const NAMES = [
  "validity_ranking",
  "time_interpretation",
  "relevance_floor",
  "rerank",
  "reflect",
  "briefs",
  "graph_candidates",
  "decision_details",
  "auto_supersede",
];

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
      NAMES.map((n) => [n, { enabled: n === "rerank", source: n === "rerank" ? "file" : "default" }]),
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
  it("shows the nine switches, the six recommended ones first, then the rejected group apart", async () => {
    render(<OptionalFeatures active />);
    await waitFor(() => expect(screen.getAllByRole("switch")).toHaveLength(10)); // nine plus the recall service
    const order = screen.getAllByRole("switch").map((el) => el.id);
    expect(order).toEqual([
      "feat-validity_ranking",
      "feat-time_interpretation",
      "feat-relevance_floor",
      "feat-rerank",
      "feat-reflect",
      "feat-briefs",
      "feat-recall-service",
      "feat-graph_candidates",
      "feat-decision_details",
      "feat-auto_supersede",
    ]);
    const rejected = screen.getByText("Measured, not recommended").closest(".section-card") as HTMLElement;
    expect(within(rejected).getAllByRole("switch")).toHaveLength(3);
    expect(within(rejected).getByText(/59 to 4/)).toBeInTheDocument();
    expect(within(rejected).getAllByText(/1 transcript in 10 lost all its decisions/)).toHaveLength(2);
    expect(screen.getByText(/about 0\.8 s to a search/)).toBeInTheDocument();
    expect(screen.getByText(/up to 40 topics a night/)).toBeInTheDocument();
    expect(screen.getByText(/only when you ask/)).toBeInTheDocument();
  });

  it("reflects what the CLI reports as on and off", async () => {
    render(<OptionalFeatures active />);
    await waitFor(() => expect(switchFor(/Reorder top results/)).toBeChecked());
    expect(switchFor(/Rank current decisions/)).not.toBeChecked();
  });

  it("toggling calls khipu_feature_set with the name and the new value, then re-reads", async () => {
    render(<OptionalFeatures active />);
    await waitFor(() => expect(switchFor(/Understand time phrases/)).not.toBeDisabled());
    const before = calls.filter(([c]) => c === "khipu_features_show").length;
    fireEvent.click(switchFor(/Understand time phrases/));
    await waitFor(() => expect(switchFor(/Understand time phrases/)).toBeChecked());
    expect(calls).toContainEqual(["khipu_feature_set", { name: "time_interpretation", enabled: true }]);
    expect(calls.filter(([c]) => c === "khipu_features_show").length).toBe(before + 1);

    fireEvent.click(switchFor(/Reorder top results/));
    await waitFor(() => expect(switchFor(/Reorder top results/)).not.toBeChecked());
    expect(calls).toContainEqual(["khipu_feature_set", { name: "rerank", enabled: false }]);
  });

  it("locks an environment-sourced switch and names the variable", async () => {
    backend.features.rerank = { enabled: true, source: "env" };
    render(<OptionalFeatures active />);
    await waitFor(() => expect(switchFor(/Reorder top results/)).toBeDisabled());
    expect(switchFor(/Reorder top results/)).toBeChecked();
    expect(screen.getByText("KHIPU_FEATURE_RERANK")).toBeInTheDocument();
    fireEvent.click(switchFor(/Reorder top results/));
    expect(calls.some(([c]) => c === "khipu_feature_set")).toBe(false);
  });

  it("shows the CLI's error text and leaves the switch where the CLI says it is", async () => {
    backend.refuse = "unknown feature 'briefs'; known: [...]";
    render(<OptionalFeatures active />);
    await waitFor(() => expect(switchFor(/sourced summary page/)).not.toBeDisabled());
    fireEvent.click(switchFor(/sourced summary page/));
    expect(await screen.findByRole("alert")).toHaveTextContent("unknown feature 'briefs'");
    expect(switchFor(/sourced summary page/)).not.toBeChecked();
  });

  it("shows an error when the Tauri command itself fails", async () => {
    render(<OptionalFeatures active />);
    await waitFor(() => expect(switchFor(/Answer questions from memory/)).not.toBeDisabled());
    const impl = invokeMock.getMockImplementation()!;
    invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
      if (cmd === "khipu_feature_set") throw "khipu exited 2: boom";
      return impl(cmd, args);
    });
    fireEvent.click(switchFor(/Answer questions from memory/));
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

  it("saves the cosine floor through khipu_config_set and refuses out-of-range input before calling", async () => {
    render(<OptionalFeatures active />);
    const field = (await screen.findByLabelText("Lowest match score that counts")) as HTMLInputElement;
    await waitFor(() => expect(field.value).toBe("0.65"));

    fireEvent.change(field, { target: { value: "1.5" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/above 0 and up to 1/);
    expect(calls.some(([c]) => c === "khipu_config_set")).toBe(false);

    fireEvent.change(field, { target: { value: "0.7" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(calls).toContainEqual(["khipu_config_set", { key: "relevance.cosine_floor", value: "0.7" }]),
    );
    const useDefault = await screen.findByRole("button", { name: "Use default" });
    fireEvent.click(useDefault);
    await waitFor(() =>
      expect(calls).toContainEqual(["khipu_config_unset", { key: "relevance.cosine_floor" }]),
    );
    await waitFor(() => expect(field.value).toBe("0.65"));
  });

  it("makes no call while inactive", () => {
    render(<OptionalFeatures active={false} />);
    expect(calls).toHaveLength(0);
  });
});
