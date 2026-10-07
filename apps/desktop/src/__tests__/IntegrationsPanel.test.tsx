// Harnesses › Claude Code card, one row per Claude home
// (docs/mockups/2026-10-07-t3-claude-homes). The tag rule and the per-home
// rows are exercised on the pure helpers; the panel test drives the real
// component with a scripted `khipu integrations` and checks which argv each
// button sends. No Tauri: the invoke the Gateway token card makes is stubbed.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import {
  IntegrationsPanel,
  cardStatus,
  homeChecks,
  homeSourceLine,
  tildePath,
} from "../IntegrationsPanel";
import type { ClaudeHomeRow, HarnessLiveness, StatusRow } from "../IntegrationsPanel";

vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(async () => "{}"),
}));

afterEach(() => {
  cleanup();
});

const DEFAULT_PATH = "/Users/me/.claude";
const T3_PATH = "/Users/me/.claude-t3-second";

function home(over: Partial<ClaudeHomeRow> = {}): ClaudeHomeRow {
  return {
    path: DEFAULT_PATH,
    label: "Default",
    source: "Claude Code, and T3's “Claude” account",
    is_default: true,
    exists: true,
    linked_to: null,
    hook_stop: true,
    hook_precompact: true,
    recall_rule: "installed",
    memory_tools_ok: true,
    hooks_ok: true,
    installed: true,
    ...over,
  };
}

/** T3's second account, its settings.json a link to ~/.claude. */
function t3Home(over: Partial<ClaudeHomeRow> = {}): ClaudeHomeRow {
  return home({
    path: T3_PATH,
    label: "T3 · Secondary",
    source: "T3's “Secondary” account",
    is_default: false,
    linked_to: { label: "Default", path: DEFAULT_PATH },
    ...over,
  });
}

/** What `t3Home()` looks like before Install: no memory tools, and (nothing
 *  linked here yet) hooks that are not there either. */
const T3_NOT_INSTALLED: Partial<ClaudeHomeRow> = {
  linked_to: null,
  hook_stop: false,
  hook_precompact: false,
  recall_rule: "missing",
  memory_tools_ok: false,
  hooks_ok: false,
  installed: false,
};

/** The pack-level fields are the AND over the homes that exist, as the CLI
 *  reports them. */
function claudeRow(homes: ClaudeHomeRow[], over: Partial<StatusRow> = {}): StatusRow {
  const live = homes.filter((h) => h.exists);
  const every = (f: (h: ClaudeHomeRow) => boolean | undefined) => live.length > 0 && live.every((h) => f(h));
  return {
    harness: "claude_code",
    detected: live.length > 0,
    mcp: every((h) => h.memory_tools_ok),
    hook_stop: every((h) => h.hook_stop),
    hook_precompact: every((h) => h.hook_precompact),
    recall_rule: every((h) => h.recall_rule === "installed") ? "installed" : "missing",
    homes,
    ...over,
  };
}

const RECORDING: HarnessLiveness = { ok: true, seen: true, captures: 4, last_captured_age_s: 240 };

// ---------------------------------------------------------------------------
// The card's tag: never green while a found home is not installed.
// ---------------------------------------------------------------------------

describe("cardStatus — Claude homes", () => {
  it("reads Recording when every home found is installed", () => {
    const row = claudeRow([home(), t3Home()]);
    expect(cardStatus(row, RECORDING, undefined)).toEqual({ tone: "ok", label: "Recording" });
  });

  it("reads '1 home not installed', warn, when one of two is missing", () => {
    const row = claudeRow([home(), t3Home(T3_NOT_INSTALLED)]);
    expect(cardStatus(row, RECORDING, undefined)).toEqual({ tone: "warn", label: "1 home not installed" });
  });

  it("counts the homes: 'N homes not installed'", () => {
    const row = claudeRow([home(), t3Home(T3_NOT_INSTALLED), home({ path: "/Users/me/.c3", label: "T3 · Third", installed: false })]);
    expect(cardStatus(row, RECORDING, undefined)).toEqual({ tone: "warn", label: "2 homes not installed" });
  });

  it("is not green while the heartbeat has not seen a session either", () => {
    const row = claudeRow([home(), t3Home(T3_NOT_INSTALLED)]);
    expect(cardStatus(row, undefined, undefined)).toEqual({ tone: "warn", label: "1 home not installed" });
  });

  it("keeps 'Not installed' when no home has Khipu at all", () => {
    const row = claudeRow([home({ installed: false }), t3Home(T3_NOT_INSTALLED)]);
    expect(cardStatus(row, RECORDING, undefined)).toEqual({ tone: "neutral", label: "Not installed" });
  });

  it("lets a red heartbeat say Not recording first", () => {
    const row = claudeRow([home(), t3Home(T3_NOT_INSTALLED)]);
    expect(cardStatus(row, { ok: false, reasons: ["hook silent"] }, undefined)).toEqual({
      tone: "err",
      label: "Not recording",
    });
  });

  it("does not count a configured folder that is not on disk", () => {
    const row = claudeRow([home(), t3Home({ ...T3_NOT_INSTALLED, exists: false })]);
    expect(cardStatus(row, RECORDING, undefined)).toEqual({ tone: "ok", label: "Recording" });
  });

  it("is unchanged for a row with no homes (every other harness)", () => {
    const row: StatusRow = {
      harness: "cursor",
      detected: true,
      mcp: true,
      hook_stop: true,
      hook_precompact: true,
      recall_rule: "project_scoped",
    };
    expect(cardStatus(row, RECORDING, undefined)).toEqual({ tone: "ok", label: "Recording" });
  });
});

// ---------------------------------------------------------------------------
// One home's row: path, source line, checks.
// ---------------------------------------------------------------------------

describe("home rows", () => {
  it("writes the home folder as ~", () => {
    expect(tildePath("/Users/me/.claude")).toBe("~/.claude");
    expect(tildePath("/Users/me")).toBe("~");
    expect(tildePath("/Volumes/Work/claude")).toBe("/Volumes/Work/claude");
  });

  it("says where Khipu found a home, and nothing more when it is installed", () => {
    expect(homeSourceLine(home())).toBe("Claude Code, and T3's “Claude” account");
  });

  it("says a linked home's hooks are the other home's", () => {
    expect(homeSourceLine(t3Home())).toBe(
      "T3's “Secondary” account · settings linked to ~/.claude, so its hooks are the Default home's",
    );
  });

  it("says an uninstalled home's sessions run without memory", () => {
    expect(homeSourceLine(t3Home(T3_NOT_INSTALLED))).toBe(
      "T3's “Secondary” account · its sessions run without memory until you install",
    );
  });

  it("says when a configured folder is not on disk, or its config cannot be read", () => {
    expect(homeSourceLine(t3Home({ exists: false }))).toMatch(/folder not found/);
    expect(homeSourceLine(t3Home({ error: "settings.json is not valid JSON" }))).toMatch(
      /couldn't read its config: settings\.json is not valid JSON/,
    );
  });

  it("marks Hooks and Memory tools from the home's own flags", () => {
    expect(homeChecks(home())).toEqual({ hooks: "ok", memoryTools: "ok" });
    expect(homeChecks(t3Home(T3_NOT_INSTALLED))).toEqual({ hooks: "err", memoryTools: "err" });
    // Capture hooks in, the rest of the six not: a warning, not a failure.
    expect(homeChecks(home({ hooks_ok: false }))).toEqual({ hooks: "warn", memoryTools: "ok" });
    expect(homeChecks(home({ memory_tools_ok: false }))).toEqual({ hooks: "ok", memoryTools: "err" });
    expect(homeChecks(home({ exists: false }))).toEqual({ hooks: "off", memoryTools: "off" });
  });
});

// ---------------------------------------------------------------------------
// The card, driven through the panel.
// ---------------------------------------------------------------------------

function renderPanel(rowsFor: () => StatusRow[], onInstall: () => void = () => {}) {
  const runKhipu = vi.fn(async (args: string[]) => {
    if (args[0] === "integrations" && args[1] === "status") return JSON.stringify(rowsFor());
    if (args[0] === "integrations" && args[1] === "install") {
      onInstall();
      return '{"harness":"claude_code","detected":true,"changes":[]}\n{\n  "verify": []\n}';
    }
    return "{}";
  });
  render(
    <IntegrationsPanel
      runKhipu={runKhipu}
      onToast={() => {}}
      active={true}
      liveness={{ ok: true, harnesses: { claude_code: RECORDING } }}
      recallProbe={null}
      refreshHealth={() => {}}
      onAnotherMac={() => {}}
    />,
  );
  return runKhipu;
}

async function claudeCard(): Promise<HTMLElement> {
  await waitFor(() => expect(screen.getByText("Claude Code")).toBeInTheDocument());
  return screen.getByText("Claude Code").closest(".hcard") as HTMLElement;
}

describe("IntegrationsPanel — Claude homes", () => {
  it("lists every home with its path, checks and tag, under the shared evidence", async () => {
    renderPanel(() => [claudeRow([home(), t3Home()])]);
    const card = await claudeCard();

    expect(within(card).getByText("Recording")).toBeInTheDocument();
    expect(within(card).getByText(/Capture hook · last capture 4 min ago/)).toBeInTheDocument();
    const list = within(card).getByRole("list", { name: "Claude homes" });
    expect(within(list).getByText("Claude homes")).toBeInTheDocument();
    expect(within(list).getByText("2 found")).toBeInTheDocument();

    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText("Default")).toBeInTheDocument();
    expect(within(rows[0]).getByText("~/.claude")).toBeInTheDocument();
    expect(within(rows[0]).getByText("Hooks")).toBeInTheDocument();
    expect(within(rows[0]).getByText("Memory tools")).toBeInTheDocument();
    expect(within(rows[0]).getByText("Installed")).toBeInTheDocument();
    expect(within(rows[0]).getByRole("button", { name: "Remove" })).toBeInTheDocument();
    expect(within(rows[1]).getByText("T3 · Secondary")).toBeInTheDocument();
    expect(within(rows[1]).getByText("~/.claude-t3-second")).toBeInTheDocument();
    expect(within(rows[1]).getByText(/settings linked to ~\/\.claude, so its hooks are the Default home's/)).toBeInTheDocument();
    expect(within(rows[1]).getByRole("button", { name: "Remove" })).toBeInTheDocument();

    expect(within(card).getByText(/Khipu finds Claude homes in ~\/\.claude, CLAUDE_CONFIG_DIR and T3's settings/)).toBeInTheDocument();
    // Remove is per home on this card; Reinstall and Verify stay on the card.
    expect(within(card).getAllByRole("button", { name: "Remove" })).toHaveLength(2);
    expect(within(card).getByRole("button", { name: "Reinstall" })).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Verify" })).toBeInTheDocument();
  });

  it("reads '1 home not installed' and offers Install on the missing home", async () => {
    renderPanel(() => [claudeRow([home(), t3Home(T3_NOT_INSTALLED)])]);
    const card = await claudeCard();

    expect(within(card).getByText("1 home not installed")).toBeInTheDocument();
    expect(within(card).queryByText("Recording")).not.toBeInTheDocument();
    // The shared evidence stays about the harness, not the missing home.
    expect(within(card).getByText(/Capture hook · last capture 4 min ago/)).toBeInTheDocument();
    expect(within(card).getByText("Recall at session start")).toBeInTheDocument();

    const rows = within(within(card).getByRole("list", { name: "Claude homes" })).getAllByRole("listitem");
    expect(within(rows[1]).getByText("Not installed")).toBeInTheDocument();
    expect(within(rows[1]).getByText(/its sessions run without memory until you install/)).toBeInTheDocument();
    // The marks are icons; the verdict is also in text for a screen reader.
    expect(within(rows[0]).getAllByText(": installed")).toHaveLength(2);
    expect(within(rows[1]).getAllByText(": not installed")).toHaveLength(2);
    expect(within(rows[1]).getByRole("button", { name: "Install" })).toBeInTheDocument();
    expect(within(rows[1]).queryByRole("button", { name: "Remove" })).not.toBeInTheDocument();
  });

  it("installs and removes one home by its path", async () => {
    const runKhipu = renderPanel(() => [claudeRow([home(), t3Home(T3_NOT_INSTALLED)])]);
    const card = await claudeCard();
    const rows = within(within(card).getByRole("list", { name: "Claude homes" })).getAllByRole("listitem");

    fireEvent.click(within(rows[1]).getByRole("button", { name: "Install" }));
    await waitFor(() =>
      expect(runKhipu).toHaveBeenCalledWith(["integrations", "install", "claude_code", "--home", T3_PATH]),
    );

    fireEvent.click(within(rows[0]).getByRole("button", { name: "Remove" }));
    await waitFor(() =>
      expect(runKhipu).toHaveBeenCalledWith(["integrations", "uninstall", "claude_code", "--home", DEFAULT_PATH]),
    );
  });

  it("sends the card's Reinstall for every home, with no --home", async () => {
    const runKhipu = renderPanel(() => [claudeRow([home(), t3Home()])]);
    const card = await claudeCard();

    fireEvent.click(within(card).getByRole("button", { name: "Reinstall" }));
    await waitFor(() => expect(runKhipu).toHaveBeenCalledWith(["integrations", "install", "claude_code"]));
  });

  it("stays off green after a capture lands while a home is still not installed", async () => {
    // Same shape as the auto-verify flip, but one home is still missing: the
    // newer beat must not turn the tag to Verified.
    let installedAtIso = "";
    const runKhipu = renderPanel(
      () => [
        claudeRow([home(), t3Home(T3_NOT_INSTALLED)], {
          last_beat_at: installedAtIso ? new Date(Date.parse(installedAtIso) + 5_000).toISOString() : null,
        }),
      ],
      () => {
        installedAtIso = new Date().toISOString();
      },
    );
    const card = await claudeCard();

    fireEvent.click(within(card).getByRole("button", { name: "Reinstall" }));
    await waitFor(() => expect(within(card).getByText(/this card turns green by itself/)).toBeInTheDocument());
    window.dispatchEvent(new Event("focus"));
    await waitFor(() => expect(runKhipu.mock.calls.filter((c) => c[0][1] === "status").length).toBeGreaterThan(2));

    expect(within(card).getByText("1 home not installed")).toBeInTheDocument();
    expect(within(card).queryByText("Verified")).not.toBeInTheDocument();
  });

  it("falls back to the plain card when the CLI reports no homes", async () => {
    renderPanel(() => [claudeRow([], { detected: true, mcp: true, hook_stop: true, hook_precompact: true, recall_rule: "installed" })]);
    const card = await claudeCard();

    expect(within(card).getByText("Recording")).toBeInTheDocument();
    expect(within(card).queryByRole("list", { name: "Claude homes" })).not.toBeInTheDocument();
    expect(card).not.toHaveClass("homes-card");
    expect(within(card).getByRole("button", { name: "Remove" })).toBeInTheDocument();
  });
});
