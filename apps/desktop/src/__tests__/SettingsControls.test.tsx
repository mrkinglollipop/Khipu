// The other settings the CLI owns: capture mode and thresholds, gateway
// address, file locations, background jobs, the gateway token (write-only).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import {
  CaptureTuningCard,
  GatewayTokenCard,
  GatewayUrlCard,
  GraphBackupRemoteCard,
  JobsCard,
  HubAwayCard,
  OfflineWaitsCard,
  PathOverridesCard,
  ReplicaCopyCard,
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

beforeEach(() => {
  calls.length = 0;
  refuse = null;
  tokenExists = false;
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
    replica_refresh: { plist_loaded: false },
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
    const select = (await screen.findByLabelText("Where captures are written")) as HTMLSelectElement;
    await waitFor(() => expect(select).not.toBeDisabled());
    fireEvent.change(select, { target: { value: "hub" } });
    await waitFor(() => expect(calls).toContainEqual(["khipu_config_set", { key: "capture_mode", value: "hub" }]));
  });

  it("locks a threshold the environment decides and names the variable", async () => {
    render(<CaptureTuningCard active />);
    const close = (await screen.findByLabelText("Close a commitment when a later capture matches it at")) as HTMLInputElement;
    await waitFor(() => expect(close.value).toBe("0.9"));
    expect(close).toBeDisabled();
    expect(screen.getAllByText("Locked").length).toBeGreaterThan(0);
    expect(screen.getByText("KHIPU_COMMITMENT_CLOSE_SIMILARITY")).toBeInTheDocument();
  });

  it("describes the independent matching rules beside both thresholds", async () => {
    render(<CaptureTuningCard active />);
    await screen.findByLabelText("Merge near-duplicate captures at");
    expect(
      screen.getByText("From 0 to 1; higher merges only closer matches. A word matching rule can also merge close captures. Default 0.92."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("From 0 to 1. A word and completion rule can also close a commitment. Default 0.85."),
    ).toBeInTheDocument();
  });

  it("keeps both newest threshold values when reversed reloads finish", async () => {
    config = {
      ...config,
      float_settings: {
        dedup_similarity: { value: 0.92, source: "default", default: 0.92 },
        commitment_close_similarity: { value: 0.85, source: "default", default: 0.85 },
      },
    };
    const pending: Array<(raw: string) => void> = [];
    let showReads = 0;
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "khipu_config_set") return JSON.stringify({ ok: true });
      if (cmd !== "khipu_config_show") throw new Error(`unexpected command ${cmd}`);
      showReads += 1;
      if (showReads === 1) return JSON.stringify(config);
      return new Promise<string>((resolve) => pending.push(resolve));
    });

    render(<CaptureTuningCard active />);
    const dedup = (await screen.findByLabelText("Merge near-duplicate captures at")) as HTMLInputElement;
    const close = screen.getByLabelText("Close a commitment when a later capture matches it at") as HTMLInputElement;
    await waitFor(() => expect(close.value).toBe("0.85"));

    fireEvent.change(dedup, { target: { value: "0.8" } });
    fireEvent.submit(dedup.closest("form")!);
    fireEvent.change(close, { target: { value: "0.7" } });
    fireEvent.submit(close.closest("form")!);
    await waitFor(() => expect(pending).toHaveLength(2));

    const newest = {
      ...config,
      float_settings: {
        dedup_similarity: { value: 0.8, source: "file", default: 0.92 },
        commitment_close_similarity: { value: 0.7, source: "file", default: 0.85 },
      },
    };
    const stale = {
      ...config,
      float_settings: {
        dedup_similarity: { value: 0.8, source: "file", default: 0.92 },
        commitment_close_similarity: { value: 0.85, source: "default", default: 0.85 },
      },
    };
    await act(async () => pending[1](JSON.stringify(newest)));
    await waitFor(() => expect(close.value).toBe("0.7"));
    await act(async () => pending[0](JSON.stringify(stale)));

    expect(dedup.value).toBe("0.8");
    expect(close.value).toBe("0.7");
  });

  it("is two cards: when a conversation is saved, and commitments", async () => {
    const { container } = render(<CaptureTuningCard active turns={5} minutes={20} />);
    await screen.findByLabelText("Your names");
    const heads = Array.from(container.querySelectorAll(".section-head")).map((h) => h.textContent);
    expect(heads).toEqual(["When Khipu saves a conversation", "Commitments"]);
    expect(
      screen.getByText(
        "After 5 turns or 20 minutes, before a conversation is compacted, and when it ends. The same on every harness.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Keys, tokens and passwords are masked before anything is summarised or stored.")).toBeInTheDocument();
    const [first, second] = Array.from(container.querySelectorAll(".section-card"));
    expect(first.querySelector("select")).not.toBeNull();
    expect(first.textContent).toContain("Merge near-duplicate captures at");
    expect(second.textContent).toContain("A commitment is something a conversation said is still owed");
    expect(second.textContent).toContain("Your names");
    expect(second.textContent).toContain("Close a commitment when a later capture matches it at");
    expect(second.textContent).toContain("Default: none.");
  });

  it("offers every Owed limit with No limit, and the GitHub check as a switch", async () => {
    config.count_settings = {
      owed_orchestrator_slice_max: { value: 3, source: "default", default: 3, no_limit: 0 },
      owed_page_max: { value: 200, source: "default", default: 200, no_limit: 0 },
      owed_bulk_max: { value: 900, source: "file", default: 500, no_limit: 0 },
      owed_tidy_calls: { value: 25, source: "default", default: 25, no_limit: 0 },
    };
    config.config = { ...(config.config as Record<string, unknown>), owed_pr_check_github: "on" };
    render(<CaptureTuningCard active />);
    const labels = [
      "Items for the orchestrator at the start of a conversation",
      "Most items on one page of Owed",
      "Most items changed at once",
      "Most model calls for one clean-up plan",
    ];
    for (const label of labels) expect(await screen.findByLabelText(label)).toBeInTheDocument();
    const page = screen.getByLabelText("Most items on one page of Owed") as HTMLInputElement;
    await waitFor(() => expect(page.value).toBe("200"));
    fireEvent.change(page, { target: { value: "0" } });
    fireEvent.submit(page.closest("form") as HTMLFormElement);
    await waitFor(() => expect(calls).toContainEqual(["khipu_config_set", { key: "owed_page_max", value: "0" }]));
    const bulk = screen.getByLabelText("Most items changed at once").closest("form") as HTMLElement;
    fireEvent.click(within(bulk).getByRole("button", { name: "Reset" }));
    await waitFor(() => expect(calls).toContainEqual(["khipu_config_unset", { key: "owed_bulk_max" }]));
    const gh = screen.getByLabelText("Ask GitHub whether a pull request merged") as HTMLSelectElement;
    expect(gh.value).toBe("on");
    fireEvent.change(gh, { target: { value: "off" } });
    await waitFor(() => expect(calls).toContainEqual(["khipu_config_set", { key: "owed_pr_check_github", value: "off" }]));
  });

  it("reads the settings once for both cards", async () => {
    render(<CaptureTuningCard active />);
    await screen.findByLabelText("Your names");
    expect(calls.filter(([c]) => c === "khipu_config_show")).toHaveLength(1);
  });

  it("shows Reading… instead of a false mode until the settings are read", async () => {
    let release: (raw: string) => void = () => {};
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd !== "khipu_config_show") throw new Error(`unexpected command ${cmd}`);
      return new Promise<string>((resolve) => (release = resolve));
    });
    render(<CaptureTuningCard active />);
    const select = screen.getByLabelText("Where captures are written") as HTMLSelectElement;
    expect(select.selectedOptions[0].textContent).toBe("Reading…");
    expect(select).toBeDisabled();
    expect(screen.queryByText("Files and Khipu")).toBeNull();
    await act(async () => release(JSON.stringify(config)));
    await waitFor(() => expect(select.selectedOptions[0].textContent).toBe("Files and Khipu"));
  });

  it("says when the settings cannot be read", async () => {
    invokeMock.mockImplementation(async () => {
      throw new Error("config unreadable");
    });
    render(<CaptureTuningCard active />);
    expect(await screen.findByText(/Could not read the settings: .*config unreadable/)).toBeInTheDocument();
    expect(screen.queryByText("Files and Khipu")).toBeNull();
  });

  it("names the variable that locks where captures are written", async () => {
    config = { ...config, capture_mode: "hub", capture_mode_source: "env" };
    render(<CaptureTuningCard active />);
    const select = (await screen.findByLabelText("Where captures are written")) as HTMLSelectElement;
    await waitFor(() => expect(select.value).toBe("hub"));
    expect(select).toBeDisabled();
    expect(screen.getByText("KHIPU_CAPTURE_MODE")).toBeInTheDocument();
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

  it("saves the commitment closing threshold as commitment_close_similarity", async () => {
    config = {
      ...config,
      float_settings: {
        dedup_similarity: { value: 0.92, source: "default", default: 0.92 },
        commitment_close_similarity: { value: 0.85, source: "default", default: 0.85 },
      },
    };
    render(<CaptureTuningCard active />);
    const close = (await screen.findByLabelText("Close a commitment when a later capture matches it at")) as HTMLInputElement;
    await waitFor(() => expect(close.value).toBe("0.85"));
    fireEvent.change(close, { target: { value: "0.8" } });
    fireEvent.submit(close.closest("form")!);
    await waitFor(() =>
      expect(calls).toContainEqual(["khipu_config_set", { key: "commitment_close_similarity", value: "0.8" }]),
    );
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

describe("GraphBackupRemoteCard", () => {
  const def = "r2:khipu-backups/khipu-graph";

  it("shows the default and where it came from, and saves a valid destination", async () => {
    config.graph_backup_remote = { value: def, source: "default", default: def };
    render(<GraphBackupRemoteCard active />);
    const field = (await screen.findByLabelText("Off-site copy goes to")) as HTMLInputElement;
    await waitFor(() => expect(field.value).toBe(def));
    expect(screen.getByText("Comes from the default.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Use the default" })).not.toBeInTheDocument();
    fireEvent.change(field, { target: { value: " r2:my-bucket/graph " } });
    fireEvent.submit(field.closest("form")!);
    await waitFor(() =>
      expect(calls).toContainEqual(["khipu_config_set", { key: "graph_backup_remote", value: "r2:my-bucket/graph" }]),
    );
  });

  it("refuses a value without a remote name before calling the CLI", async () => {
    config.graph_backup_remote = { value: def, source: "default", default: def };
    render(<GraphBackupRemoteCard active />);
    const field = (await screen.findByLabelText("Off-site copy goes to")) as HTMLInputElement;
    await waitFor(() => expect(field.value).toBe(def));
    fireEvent.change(field, { target: { value: "just-a-bucket" } });
    fireEvent.submit(field.closest("form")!);
    expect(await screen.findByRole("alert")).toHaveTextContent(/rclone remote and folder/);
    expect(calls.some(([cmd]) => cmd === "khipu_config_set")).toBe(false);
  });

  it("offers the default again for a saved value and locks an environment one", async () => {
    config.graph_backup_remote = { value: "r2:mine/g", source: "config", default: def };
    render(<GraphBackupRemoteCard active />);
    expect(await screen.findByText("Comes from this Mac's settings.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Use the default" }));
    await waitFor(() => expect(calls).toContainEqual(["khipu_config_unset", { key: "graph_backup_remote" }]));
    cleanup();
    config.graph_backup_remote = { value: "r2:env/g", source: "env", default: def };
    render(<GraphBackupRemoteCard active />);
    const field = (await screen.findByLabelText("Off-site copy goes to")) as HTMLInputElement;
    await waitFor(() => expect(field.value).toBe("r2:env/g"));
    expect(field).toBeDisabled();
    expect(screen.getByText("Comes from the environment variable KHIPU_GRAPH_BACKUP_REMOTE.")).toBeInTheDocument();
  });
});

describe("ReplicaCopyCard", () => {
  beforeEach(() => {
    config.replica = {
      replica_refresh_after: { value: 900, default: 900, source: "default" },
      replica_check_every: { value: 300, default: 300, source: "default" },
      hub_down_memo_for: { value: 60, default: 60, source: "default" },
    };
  });

  it("shows the skip window in seconds, saves whole seconds (0 allowed) and resets from file", async () => {
    (config.replica as Record<string, unknown>).hub_down_memo_for = { value: 90, default: 60, source: "file" };
    render(<ReplicaCopyCard active />);
    const memo = (await screen.findByLabelText(
      "Skip the database after a failed connection for (seconds)",
    )) as HTMLInputElement;
    await waitFor(() => expect(memo.value).toBe("90"));
    fireEvent.change(memo, { target: { value: "0" } });
    fireEvent.submit(memo.closest("form")!);
    await waitFor(() => expect(calls).toContainEqual(["khipu_config_set", { key: "hub_down_memo_for", value: "0" }]));
    fireEvent.change(memo, { target: { value: "1.5" } });
    fireEvent.submit(memo.closest("form")!);
    expect(await screen.findByRole("alert")).toHaveTextContent(/whole number of seconds/);
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    await waitFor(() => expect(calls).toContainEqual(["khipu_config_unset", { key: "hub_down_memo_for" }]));
  });

  it("shows both times in minutes and saves minutes as whole seconds", async () => {
    render(<ReplicaCopyCard active />);
    const after = (await screen.findByLabelText("Bring the copy up to date after (minutes)")) as HTMLInputElement;
    const every = screen.getByLabelText("Check every (minutes)") as HTMLInputElement;
    await waitFor(() => expect(after.value).toBe("15"));
    expect(every.value).toBe("5");
    fireEvent.change(after, { target: { value: "20" } });
    fireEvent.submit(after.closest("form")!);
    await waitFor(() =>
      expect(calls).toContainEqual(["khipu_config_set", { key: "replica_refresh_after", value: "1200" }]),
    );
  });

  it("refuses a check interval under a minute before calling the CLI", async () => {
    render(<ReplicaCopyCard active />);
    const every = (await screen.findByLabelText("Check every (minutes)")) as HTMLInputElement;
    await waitFor(() => expect(every.value).toBe("5"));
    fireEvent.change(every, { target: { value: "0" } });
    fireEvent.submit(every.closest("form")!);
    expect(await screen.findByRole("alert")).toHaveTextContent(/whole number of minutes, 1 or more/);
    expect(calls.some(([cmd]) => cmd === "khipu_config_set")).toBe(false);
  });
});

describe("OfflineWaitsCard", () => {
  const offline = (value: number, source = "default") => ({
    value, default: 60, kind: "duration", source, words: "Longest the app waits for one Khipu answer",
  });

  it("saves a wait through khipu_config_set and resets through khipu_config_unset", async () => {
    config = { ...config, offline: { app_cli_timeout: offline(45, "file") } };
    render(<OfflineWaitsCard active />);
    const field = (await screen.findByLabelText("Wait for an answer in this app (seconds)")) as HTMLInputElement;
    await waitFor(() => expect(field.value).toBe("45"));
    expect(screen.getByText("Longest the app waits for one Khipu answer. Default 60.")).toBeInTheDocument();
    fireEvent.change(field, { target: { value: "90" } });
    fireEvent.click(field.closest("form")!.querySelector("button[type=submit]")!);
    await waitFor(() => expect(calls).toContainEqual(["khipu_config_set", { key: "app_cli_timeout", value: "90" }]));
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    await waitFor(() => expect(calls).toContainEqual(["khipu_config_unset", { key: "app_cli_timeout" }]));
  });

  it("refuses a value that is not whole seconds without calling the CLI", async () => {
    config = { ...config, offline: { app_cli_timeout: offline(60) } };
    render(<OfflineWaitsCard active />);
    const field = (await screen.findByLabelText("Wait for an answer in this app (seconds)")) as HTMLInputElement;
    await waitFor(() => expect(field.value).toBe("60"));
    fireEvent.change(field, { target: { value: "1.5" } });
    fireEvent.click(field.closest("form")!.querySelector("button[type=submit]")!);
    expect(await screen.findByRole("alert")).toHaveTextContent("whole number of seconds");
    expect(calls.some(([c]) => c === "khipu_config_set")).toBe(false);
  });
});

describe("HubAwayCard", () => {
  it("puts the copy, the skip window and the waits under one heading, each saved through khipu_config_set", async () => {
    config = {
      ...config,
      replica: {
        replica_refresh_after: { value: 900, default: 900, source: "default" },
        replica_check_every: { value: 300, default: 300, source: "default" },
        hub_down_memo_for: { value: 60, default: 60, source: "default" },
      },
      offline: {
        hub_statement_timeout: { value: 30, default: 30, kind: "duration", source: "default", words: "a" },
        hub_dead_connection_after: { value: 30, default: 30, kind: "duration", source: "default", words: "b" },
        app_cli_timeout: { value: 60, default: 60, kind: "duration", source: "default", words: "c" },
      },
    };
    render(<HubAwayCard active />);
    const card = screen.getByRole("region", { name: "When the hub is away" });
    expect(screen.getAllByText("When the hub is away")).toHaveLength(1);
    const fields: Array<[string, string, string]> = [
      ["Bring the copy up to date after (minutes)", "20", "1200"],
      ["Skip the database after a failed connection for (seconds)", "45", "45"],
      ["Wait on the hub for a read or write (seconds)", "10", "10"],
      ["Wait for an answer in this app (seconds)", "90", "90"],
    ];
    const keys = ["replica_refresh_after", "hub_down_memo_for", "hub_statement_timeout", "app_cli_timeout"];
    for (const [i, [label, typed, stored]] of fields.entries()) {
      const field = (await screen.findByLabelText(label)) as HTMLInputElement;
      expect(card.contains(field)).toBe(true);
      await waitFor(() => expect(field.value).not.toBe(""));
      fireEvent.change(field, { target: { value: typed } });
      fireEvent.submit(field.closest("form")!);
      await waitFor(() => expect(calls).toContainEqual(["khipu_config_set", { key: keys[i], value: stored }]));
    }
  });
});
