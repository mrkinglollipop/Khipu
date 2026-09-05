// Layer 3 oracle — docs/plans/2026-09-05-setup-that-cannot-strand-you.md,
// "the gaps become oracles": the built Database step, driven by a scripted
// fake backend, must always land in exactly one of the three states ("the
// rule every screen follows") and must never show a raw code. Run via
// `npm run check:setup` (vitest), wired into the same oracle list as
// `npm run build`.
import { useState } from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup, within } from "@testing-library/react";
import { Welcome } from "../Welcome";
import App from "../App";

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

afterEach(() => {
  cleanup();
  invokeMock.mockReset();
});

// ---------------------------------------------------------------------------
// Shared fixtures: the connect pipeline's own stage shape
// (docs/plans/2026-09-05-setup-that-cannot-strand-you.md stages 1-9).
// ---------------------------------------------------------------------------

const PREFLIGHT_IDS = ["reach", "version", "privileges", "schema", "graph"];
const CONNECT_IDS = [...PREFLIGHT_IDS, "store", "upkeep", "prove", "summary"];

type Stage = { id: string; ok: boolean; status?: string; title: string; detail: string; fix?: string };

function okStage(id: string): Stage {
  return { id, ok: true, title: `${id} ok`, detail: `${id} step passed.` };
}

function passingStages(ids: string[], summaryDetail?: string): Stage[] {
  return ids.map((id) =>
    id === "summary" && summaryDetail
      ? { id, ok: true, title: "Working", detail: summaryDetail }
      : okStage(id),
  );
}

function failingAt(
  failId: string,
  failure: { title: string; detail: string; fix: string },
): Stage[] {
  const stages: Stage[] = [];
  for (const id of PREFLIGHT_IDS) {
    if (id === failId) {
      stages.push({ id, ok: false, title: failure.title, detail: failure.detail, fix: failure.fix });
      break;
    }
    stages.push(okStage(id));
  }
  const idx = PREFLIGHT_IDS.indexOf(failId);
  for (const id of PREFLIGHT_IDS.slice(idx + 1)) {
    stages.push({ id, ok: true, status: "skipped", title: "Skipped", detail: "skipped: an earlier stage failed" });
  }
  return stages;
}

// Two-plus snake_case segments, unlike SetupStages's own (deliberately
// broader) `/^[a-z_]+$/` guard on payload fields: this walks *every* text
// node in the rendered tree, including ordinary short words ("or", "a") from
// prose this test does not control, so it needs the extra segment to avoid
// flagging real English. Every actual code in this pipeline
// (vector_extension_missing, target_not_empty, dsn_required, ...) clears it.
const RAW_CODE_RE = /^[a-z]+(?:_[a-z]+)+$/;

/** Walks every visible text node under `container` and fails if any is
 *  nothing but lowercase-and-underscores — a stage id or an error code
 *  (`vector_extension_missing`, `target_not_empty`) that should have been
 *  translated to a sentence before it reached the DOM. */
function assertNoRawCode(container: HTMLElement) {
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  const offenders: string[] = [];
  let node: Node | null;
  while ((node = walker.nextNode())) {
    const text = (node.textContent ?? "").trim();
    if (text && RAW_CODE_RE.test(text)) offenders.push(text);
  }
  expect(offenders, `raw code(s) leaked into the DOM: ${JSON.stringify(offenders)}`).toEqual([]);
}

/** Mirrors the real contract: `dsnOk` is a separate, App-owned freshness
 *  signal that only becomes true once its `refreshDsn` callback has actually
 *  run (App.tsx wires this to its own periodic status check) — so a
 *  successful connect flips it via that callback, not by the pipeline result
 *  alone. A harness component, not a bare mock, so that round trip is real. */
function DatabaseStepHarness() {
  const [dsnOk, setDsnOk] = useState<boolean | null>(null);
  return (
    <Welcome
      dsnOk={dsnOk}
      refreshDsn={async () => setDsnOk(true)}
      runKhipu={async () => "{}"}
      onFinish={() => {}}
      openIntegrations={() => {}}
      initialStep="database"
      initialStepKey={1}
    />
  );
}

function renderDatabaseStep() {
  return render(<DatabaseStepHarness />);
}

function selectRemoteMode() {
  fireEvent.click(screen.getByLabelText(/Connect to a database I already run/i));
}

function typeDsn(value: string) {
  fireEvent.change(screen.getByLabelText("Connection string"), { target: { value } });
}

function clickConnect() {
  fireEvent.click(screen.getByRole("button", { name: /^Connect$/ }));
}

// ---------------------------------------------------------------------------
// The six scripted preflight failures the plan names explicitly.
// ---------------------------------------------------------------------------

const SCENARIOS: { name: string; failId: string; title: string; detail: string; fix: string }[] = [
  {
    name: "host unreachable",
    failId: "reach",
    title: "Khipu could not reach that server",
    detail: "connection failed: Connection refused",
    fix: "Check the host and port, and that the server allows connections from this Mac.",
  },
  {
    name: "wrong password",
    failId: "reach",
    title: "The username or password is wrong",
    detail: 'connection failed: password authentication failed for user "khipu"',
    fix: "Double check the username and password in the connection string, then try again.",
  },
  {
    name: "missing database",
    failId: "reach",
    title: "That database does not exist yet",
    detail: 'connection failed: database "khipu" does not exist',
    fix: "Create it on the server (`CREATE DATABASE name;`) or ask your host to create it, then try again.",
  },
  {
    name: "certificate error",
    failId: "reach",
    title: "The server's certificate is not trusted",
    detail: "connection failed: self signed certificate",
    fix: "Paste the certificate file your host gave you (often called root.crt or ca.pem).",
  },
  {
    name: "Postgres too old",
    failId: "version",
    title: "This server's Postgres is too old",
    detail: "Your server runs 16; Khipu needs Postgres 19 or newer.",
    fix: "Upgrade the server to Postgres 19+, or point Khipu at a server that already runs it.",
  },
  {
    name: "extension privilege",
    failId: "privileges",
    title: "This account cannot enable the vector extension",
    detail: 'InsufficientPrivilege: permission denied to create extension "vector"',
    fix: "Ask your host to run CREATE EXTENSION vector; on database khipu (managed providers: enable the vector extension in their console).",
  },
];

describe("Welcome › Database step (remote) — scripted failures", () => {
  beforeEach(() => {
    invokeMock.mockReset();
  });

  for (const scenario of SCENARIOS) {
    it(`${scenario.name}: shows the fix in plain words with one action, never a raw code`, async () => {
      invokeMock.mockImplementation(async (cmd: string) => {
        if (cmd === "khipu_db_preflight") {
          return JSON.stringify({ ok: false, stages: failingAt(scenario.failId, scenario) });
        }
        throw new Error(`unexpected invoke in this scenario: ${cmd}`);
      });

      const { container } = renderDatabaseStep();
      selectRemoteMode();
      typeDsn("postgresql://user:pass@example.com:5432/khipu");
      clickConnect();

      await waitFor(() => expect(screen.getByText(scenario.fix)).toBeInTheDocument());

      // Exactly one terminal state: the failed stage's fix, never also a
      // green "Working" summary.
      expect(screen.queryByText(/^Working\./)).not.toBeInTheDocument();
      const actionButtons = screen.getAllByRole("button", {
        name: /Try again|Paste the certificate file/,
      });
      expect(actionButtons.length).toBeGreaterThan(0);

      // The Next button must not be enabled off a failed database step.
      const nextButton = screen.getByRole("button", { name: /Next/ });
      expect(nextButton).toBeDisabled();

      assertNoRawCode(container);
    });
  }

  it("certificate error offers 'Paste the certificate file…', not just 'Try again'", async () => {
    const scenario = SCENARIOS.find((s) => s.name === "certificate error")!;
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "khipu_db_preflight") {
        return JSON.stringify({ ok: false, stages: failingAt(scenario.failId, scenario) });
      }
      throw new Error(`unexpected invoke: ${cmd}`);
    });
    renderDatabaseStep();
    selectRemoteMode();
    typeDsn("postgresql://user:pass@example.com:5432/khipu");
    clickConnect();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Paste the certificate file/ })).toBeInTheDocument(),
    );
  });
});

describe("Welcome › Database step (remote) — success", () => {
  beforeEach(() => {
    invokeMock.mockReset();
  });

  it("a fully successful connect shows the summary sentence with a green mark and enables Next", async () => {
    const summary = "Working. example.com · 42 sessions remembered · nightly upkeep at 02:05.";
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "khipu_db_preflight") {
        return JSON.stringify({ ok: true, stages: passingStages(PREFLIGHT_IDS) });
      }
      if (cmd === "khipu_db_connect") {
        return JSON.stringify({ ok: true, stages: passingStages(CONNECT_IDS, summary), summary: { host: "example.com" } });
      }
      throw new Error(`unexpected invoke: ${cmd}`);
    });

    const { container } = renderDatabaseStep();
    selectRemoteMode();
    typeDsn("postgresql://user:pass@example.com:5432/khipu");
    clickConnect();

    await waitFor(() => expect(screen.getByText(summary)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /Next/ })).not.toBeDisabled();
    // Not also showing a failure state.
    expect(screen.queryByRole("button", { name: /Try again/ })).not.toBeInTheDocument();
    assertNoRawCode(container);
  });
});

// ---------------------------------------------------------------------------
// Settings › Database › Move, and the Another Mac gate. These live inside the
// top-level App component (App.tsx); every section renders unconditionally
// (only CSS toggles which tab is visible), so the Database section's own
// controls and the settings subnav are already in the DOM without navigating
// tabs — only `settingsSection` (a distinct piece of state, switched via the
// subnav tabs) gates which settings pane's markup exists at all.
// ---------------------------------------------------------------------------

function defaultInvoke(cmd: string): string {
  // A generous, harmless default for the large number of calls App.tsx and
  // its always-mounted child panels (ComponentsPanel, IntegrationsPanel)
  // fire on mount — none of that machinery is what these two tests check.
  if (cmd === "khipu_db_status") {
    return JSON.stringify({ ok: true, host_kind: "remote", episodes: 10, topics: 3 });
  }
  return JSON.stringify({ ok: true });
}

describe("Settings › Database — Move dialog and Another Mac gate", () => {
  beforeEach(() => {
    invokeMock.mockReset();
  });

  it("the Move dialog's confirm button is disabled until the dry run returns", async () => {
    let dryRunResolve: ((v: string) => void) | null = null;
    invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
      if (cmd === "khipu_db_preflight") {
        return JSON.stringify({ ok: true, stages: passingStages(PREFLIGHT_IDS) });
      }
      if (cmd === "khipu_db_move" && args?.dryRun === true) {
        // Held open deliberately — the test controls when it resolves.
        return new Promise<string>((resolve) => {
          dryRunResolve = resolve;
        });
      }
      return defaultInvoke(cmd);
    });

    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: /Move this memory to another database/ }));
    fireEvent.change(screen.getByLabelText("Target connection string"), {
      target: { value: "postgresql://user:pass@target.example.com:5432/khipu" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Check the target/ }));

    // Before the dry run resolves, the confirm button does not exist at all —
    // "Reading table counts…" stands in for it.
    await waitFor(() => expect(screen.getByText(/Reading table counts/)).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /^Copy \d+ tables?$/ })).not.toBeInTheDocument();

    dryRunResolve!(
      JSON.stringify({ ok: true, dry_run: true, tables: [{ name: "episodes", source_rows: 10, target_rows: null, seconds: 0.01 }] }),
    );

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^Copy 1 table$/ })).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /^Copy 1 table$/ })).not.toBeDisabled();
  });

  it("shows the this-Mac callout and disables join-kit buttons when host_kind is local", async () => {
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "khipu_db_status") {
        return JSON.stringify({ ok: true, host_kind: "this-mac", episodes: 1, topics: 1 });
      }
      return defaultInvoke(cmd);
    });

    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    fireEvent.click(screen.getByRole("tab", { name: "Another Mac" }));

    await waitFor(() =>
      expect(
        screen.getByText(/This database lives only on this Mac, so another Mac cannot reach it\./),
      ).toBeInTheDocument(),
    );
    // Scoped to the Another Mac section card — IntegrationsPanel (always
    // mounted, on the Harnesses tab) has its own "Save join kit…" shortcut
    // that only navigates here; it is not the button under test.
    const section = within(screen.getByText("Set up another Mac").closest(".section-card") as HTMLElement);
    expect(section.getByRole("button", { name: /Save join kit/ })).toBeDisabled();
    expect(section.getByRole("button", { name: /Advertise nearby \(PIN\)/ })).toBeDisabled();
    expect(section.getByText(/Both Macs must be on the same Wi‑Fi or network/)).toBeInTheDocument();
  });

  it("does not gate the join-kit buttons when host_kind is remote", async () => {
    invokeMock.mockImplementation(async (cmd: string) => defaultInvoke(cmd));

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    fireEvent.click(screen.getByRole("tab", { name: "Another Mac" }));

    await waitFor(() =>
      expect(screen.getByText("Set up another Mac")).toBeInTheDocument(),
    );
    const section = within(screen.getByText("Set up another Mac").closest(".section-card") as HTMLElement);
    expect(section.getByRole("button", { name: /Save join kit/ })).not.toBeDisabled();
    expect(screen.queryByText(/This database lives only on this Mac/)).not.toBeInTheDocument();
  });
});
