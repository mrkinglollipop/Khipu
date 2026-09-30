// The other settings the CLI owns: capture mode and thresholds, gateway
// address, file locations, background jobs, the gateway token (write-only) and
// the active search-index profile.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import {
  CaptureTuningCard,
  EmbedProfilesCard,
  GatewayTokenCard,
  GatewayUrlCard,
  JobsCard,
  PathOverridesCard,
} from "../SettingsControls";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

const calls: Array<[string, Record<string, unknown> | undefined]> = [];
let config: Record<string, unknown>;
let jobs: Record<string, unknown>;
let tokenExists: boolean;
let refuse: string | null;
let profileMissing: Record<string, number>;
let activeProfile: string;

beforeEach(() => {
  calls.length = 0;
  refuse = null;
  tokenExists = false;
  activeProfile = "gemini-embedding-2@768";
  profileMissing = { "gemini-embedding-001@768": 0 };
  config = {
    capture_mode: "dual",
    capture_mode_source: "default",
    gateway_url: "https://khipu.example.org",
    paths: {
      memory_root: { value: "/Volumes/M/conversations", source: "file", exists: true },
      memory_repo: { value: null, source: "unset", exists: false },
      capture_v2: { value: "/x/capture_v2.py", source: "env:KHIPU_CAPTURE_V2", exists: true },
      graph_sqlite: { value: "/gone/graph.sqlite", source: "file", exists: false },
      gemini_key_file: { value: null, source: "unset", exists: false },
    },
    user_aliases: ["matt"],
    float_settings: {
      dedup_similarity: { value: 0.92, source: "default", default: 0.92 },
      commitment_close_similarity: { value: 0.9, source: "env", default: 0.85 },
    },
    config: { gateway_url: "https://khipu.example.org", user_aliases: ["matt"] },
  };
  jobs = {
    nightly: { plist_loaded: true, next_schedule: "daily 02:05", last_run_iso: null },
    monthly: { plist_loaded: false },
    graph_build: { plist_loaded: true, next_schedule: "daily 02:17" },
    notes_watch: { plist_loaded: false },
    queue_drain: { plist_loaded: false },
  };
  invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
    calls.push([cmd, args]);
    const bad = () => JSON.stringify({ ok: false, error: refuse });
    switch (cmd) {
      case "khipu_config_show":
        return JSON.stringify(config);
      case "khipu_config_set":
        return refuse ? bad() : JSON.stringify({ ok: true });
      case "khipu_config_unset":
        return JSON.stringify({ ok: true });
      case "khipu_jobs_status":
        return JSON.stringify(jobs);
      case "khipu_job_install":
      case "khipu_job_uninstall": {
        if (refuse) return bad();
        (jobs[String(args?.name)] as Record<string, unknown>).plist_loaded = cmd === "khipu_job_install";
        return JSON.stringify({ ok: true, results: [{ ok: true }] });
      }
      case "khipu_gateway_token_status":
        return JSON.stringify({ path: "/Users/x/.config/khipu/gateway-token", exists: tokenExists, bytes: tokenExists ? 48 : 0 });
      case "khipu_gateway_token_set":
        if (refuse) return bad();
        tokenExists = true;
        return JSON.stringify({ ok: true, exists: true, bytes: 48 });
      case "khipu_embed_status": {
        const profile = args?.profile as string | undefined;
        if (profile) {
          return JSON.stringify({ profile, episodes: { missing: profileMissing[profile] ?? 0 }, topics: { missing: 0 } });
        }
        return JSON.stringify({
          active_profile: activeProfile,
          profiles: [
            { id: "gemini-embedding-001@768", model: "gemini-embedding-001", dim: 768, active: activeProfile === "gemini-embedding-001@768" },
            { id: "gemini-embedding-2@768", model: "gemini-embedding-2", dim: 768, active: activeProfile === "gemini-embedding-2@768" },
          ],
        });
      }
      case "khipu_embed_activate":
        if (refuse) return bad();
        activeProfile = String(args?.profile);
        return JSON.stringify({ ok: true, active_profile: activeProfile });
      default:
        throw new Error(`unexpected command ${cmd}`);
    }
  });
});

afterEach(() => {
  cleanup();
  invokeMock.mockReset();
});

describe("CaptureTuningCard", () => {
  it("changes capture mode through khipu_config_set with the fixed key", async () => {
    render(<CaptureTuningCard active />);
    const select = (await screen.findByLabelText("Where a capture is written")) as HTMLSelectElement;
    await waitFor(() => expect(select).not.toBeDisabled());
    fireEvent.change(select, { target: { value: "hub" } });
    await waitFor(() => expect(calls).toContainEqual(["khipu_config_set", { key: "capture_mode", value: "hub" }]));
  });

  it("locks a threshold the environment decides and names the variable", async () => {
    render(<CaptureTuningCard active />);
    const close = (await screen.findByLabelText("Close an open commitment at")) as HTMLInputElement;
    await waitFor(() => expect(close.value).toBe("0.9"));
    expect(close).toBeDisabled();
    expect(screen.getAllByText("Locked").length).toBeGreaterThan(0);
    expect(screen.getByText("KHIPU_COMMITMENT_CLOSE_SIMILARITY")).toBeInTheDocument();
  });

  it("saves the dedup threshold and your names, and shows the CLI's refusal", async () => {
    render(<CaptureTuningCard active />);
    const dedup = (await screen.findByLabelText("Merge near-duplicate captures at")) as HTMLInputElement;
    await waitFor(() => expect(dedup.value).toBe("0.92"));
    fireEvent.change(dedup, { target: { value: "0.8" } });
    fireEvent.click(dedup.closest("form")!.querySelector("button")!);
    await waitFor(() => expect(calls).toContainEqual(["khipu_config_set", { key: "dedup_similarity", value: "0.8" }]));

    const names = screen.getByLabelText("Your names") as HTMLInputElement;
    fireEvent.change(names, { target: { value: "matt, matthew" } });
    refuse = "user_aliases is not writable";
    fireEvent.submit(names.closest("form")!);
    expect(await screen.findByText("user_aliases is not writable")).toBeInTheDocument();
    expect(calls).toContainEqual(["khipu_config_set", { key: "user_aliases", value: "matt, matthew" }]);
  });
});

describe("Save button", () => {
  it("stays disabled until the value differs from the saved one", async () => {
    render(<PathOverridesCard active />);
    const root = (await screen.findByLabelText("Session file folder")) as HTMLInputElement;
    await waitFor(() => expect(root.value).toBe("/Volumes/M/conversations"));
    const save = root.closest("form")!.querySelector("button[type=submit]") as HTMLButtonElement;
    expect(save).toBeDisabled();
    fireEvent.change(root, { target: { value: "/Volumes/M/other" } });
    expect(save).not.toBeDisabled();
    fireEvent.change(root, { target: { value: "/Volumes/M/conversations" } });
    expect(save).toBeDisabled();
  });
});

describe("JobsCard descriptions", () => {
  it("says in plain words what each job does", async () => {
    render(<JobsCard active />);
    expect(await screen.findByText(/Consolidates the day's captures/)).toBeInTheDocument();
    expect(screen.getByText(/saves captures that another harness queued/)).toBeInTheDocument();
    expect(screen.getByText(/Keeps a small process running/)).toBeInTheDocument();
    expect(screen.queryByText("Queue drain")).not.toBeInTheDocument();
  });
});

describe("GatewayUrlCard", () => {
  it("saves an address and clears it with an empty value", async () => {
    render(<GatewayUrlCard active />);
    const field = (await screen.findByLabelText(/Public https address/)) as HTMLInputElement;
    await waitFor(() => expect(field.value).toBe("https://khipu.example.org"));
    fireEvent.change(field, { target: { value: "" } });
    fireEvent.submit(field.closest("form")!);
    await waitFor(() => expect(calls).toContainEqual(["khipu_config_set", { key: "gateway_url", value: "" }]));
  });

  it("says when an environment variable overrides the saved address", async () => {
    config.gateway_url = "https://env.example.org";
    render(<GatewayUrlCard active />);
    expect(await screen.findByText("KHIPU_GATEWAY_URL")).toBeInTheDocument();
  });
});

describe("PathOverridesCard", () => {
  it("resets a file-sourced path, locks an env-sourced one and flags a missing file", async () => {
    render(<PathOverridesCard active />);
    const root = (await screen.findByLabelText("Session file folder")) as HTMLInputElement;
    await waitFor(() => expect(root.value).toBe("/Volumes/M/conversations"));
    expect(screen.getByLabelText("Capture script")).toBeDisabled();
    expect(screen.getByText("KHIPU_CAPTURE_V2")).toBeInTheDocument();
    expect(screen.getByText("Not found on this Mac")).toBeInTheDocument();
    // Reset only where the file holds a value.
    expect(screen.getAllByRole("button", { name: "Reset" })).toHaveLength(2);
    fireEvent.click(root.closest("form")!.querySelectorAll("button")[1]);
    await waitFor(() => expect(calls).toContainEqual(["khipu_config_unset", { key: "memory_root" }]));
  });

  it("saves a path through khipu_config_set", async () => {
    render(<PathOverridesCard active />);
    const repo = (await screen.findByLabelText("Memory git repository")) as HTMLInputElement;
    fireEvent.change(repo, { target: { value: "/Volumes/M" } });
    fireEvent.submit(repo.closest("form")!);
    await waitFor(() => expect(calls).toContainEqual(["khipu_config_set", { key: "memory_repo", value: "/Volumes/M" }]));
  });
});

describe("JobsCard", () => {
  it("installs a missing job and removes an installed one after a confirm", async () => {
    render(<JobsCard active />);
    fireEvent.click(await screen.findByRole("button", { name: "Install monthly topic pass" }));
    await waitFor(() => expect(calls).toContainEqual(["khipu_job_install", { name: "monthly" }]));
    expect(await screen.findByRole("button", { name: "Remove monthly topic pass" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Remove nightly upkeep" }));
    expect(calls.some(([c]) => c === "khipu_job_uninstall")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Remove nightly upkeep" }));
    await waitFor(() => expect(calls).toContainEqual(["khipu_job_uninstall", { name: "nightly" }]));
  });

  it("shows the CLI's error beside the job that failed", async () => {
    render(<JobsCard active />);
    refuse = "launchctl refused";
    fireEvent.click(await screen.findByRole("button", { name: "Install queued captures" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("launchctl refused");
  });
});

describe("GatewayTokenCard", () => {
  it("sends the value once, empties the field, and never displays it", async () => {
    const secret = "tok_ABCDEF0123456789";
    const { container } = render(<GatewayTokenCard active />);
    const field = (await screen.findByLabelText("Bearer token")) as HTMLInputElement;
    expect(field.type).toBe("password");
    fireEvent.change(field, { target: { value: secret } });
    fireEvent.submit(field.closest("form")!);
    await waitFor(() => expect(calls).toContainEqual(["khipu_gateway_token_set", { value: secret }]));
    await waitFor(() => expect(field.value).toBe(""));
    expect(container.textContent).not.toContain(secret);
    expect(await screen.findByText("Stored")).toBeInTheDocument();
  });

  it("keeps the field content and shows the error when the CLI refuses", async () => {
    render(<GatewayTokenCard active />);
    const field = (await screen.findByLabelText("Bearer token")) as HTMLInputElement;
    refuse = "token is empty";
    fireEvent.change(field, { target: { value: "abc" } });
    fireEvent.submit(field.closest("form")!);
    expect(await screen.findByRole("alert")).toHaveTextContent("token is empty");
    expect(field.value).toBe("abc");
  });
});

describe("EmbedProfilesCard", () => {
  it("marks the active profile and makes another one active when it has every vector", async () => {
    render(<EmbedProfilesCard active />);
    const make = await screen.findByRole("button", { name: "Make active" });
    await waitFor(() => expect(make).not.toBeDisabled());
    expect(screen.getByText("Active")).toBeInTheDocument();
    fireEvent.click(make);
    await waitFor(() =>
      expect(calls).toContainEqual(["khipu_embed_activate", { profile: "gemini-embedding-001@768" }]),
    );
    await waitFor(() => expect(screen.queryByRole("button", { name: "Make active" })).toBeInTheDocument());
  });

  it("disables Make active while the profile is missing vectors, and never sends force", async () => {
    profileMissing["gemini-embedding-001@768"] = 12;
    render(<EmbedProfilesCard active />);
    const make = await screen.findByRole("button", { name: "Make active" });
    await waitFor(() => expect(screen.getByText(/12 items still missing vectors/)).toBeInTheDocument());
    expect(make).toBeDisabled();
    fireEvent.click(make);
    expect(calls.some(([c]) => c === "khipu_embed_activate")).toBe(false);
  });
});
