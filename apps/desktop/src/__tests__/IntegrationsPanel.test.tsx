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
  hasKhipuEntries,
  homeChecks,
  homeSourceLine,
  parseActResults,
  tildePath,
  userHomeDir,
} from "../IntegrationsPanel";
import type { ClaudeHomeRow, HarnessLiveness, StatusRow } from "../IntegrationsPanel";

vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(async () => "{}"),
}));

afterEach(() => {
  cleanup();
});

const HOME_DIR = "/Users/me";
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
  it("writes the user's own home folder as ~, and no other", () => {
    expect(tildePath("/Users/me/.claude", HOME_DIR)).toBe("~/.claude");
    expect(tildePath("/Users/me", HOME_DIR)).toBe("~");
    expect(tildePath("/Volumes/Work/claude", HOME_DIR)).toBe("/Volumes/Work/claude");
    // Another user's folder, and a sibling that only shares the prefix.
    expect(tildePath("/Users/shared/.claude", HOME_DIR)).toBe("/Users/shared/.claude");
    expect(tildePath("/Users/meagan/.claude", HOME_DIR)).toBe("/Users/meagan/.claude");
    // No known home folder: leave the path alone.
    expect(tildePath("/Users/me/.claude")).toBe("/Users/me/.claude");
  });

  it("takes the user's home folder from the default Claude home", () => {
    expect(userHomeDir([t3Home(), home()])).toBe(HOME_DIR);
    expect(userHomeDir([t3Home()])).toBeUndefined();
    expect(userHomeDir(undefined)).toBeUndefined();
  });

  it("says where Khipu found a home, and nothing more when it is installed", () => {
    expect(homeSourceLine(home())).toBe("Claude Code, and T3's “Claude” account");
  });

  it("says a linked home's hooks are the other home's", () => {
    expect(homeSourceLine(t3Home(), HOME_DIR)).toBe(
      "T3's “Secondary” account · settings linked to ~/.claude, so its hooks are the Default home's",
    );
  });

  it("says an uninstalled home's sessions run without memory", () => {
    expect(homeSourceLine(t3Home(T3_NOT_INSTALLED), HOME_DIR)).toBe(
      "T3's “Secondary” account · its sessions run without memory until you install",
    );
  });

  it("does not claim a partly installed home runs without memory", () => {
    const hooksOnly = t3Home({ ...T3_NOT_INSTALLED, hook_stop: true, hook_precompact: true });
    expect(homeSourceLine(hooksOnly, HOME_DIR)).toMatch(/its hooks are in but the memory tools are not/);
    expect(homeSourceLine(hooksOnly, HOME_DIR)).not.toMatch(/without memory/);
    expect(homeSourceLine(t3Home({ ...T3_NOT_INSTALLED, memory_tools_ok: true }), HOME_DIR)).toMatch(
      /memory tools are in but the hooks are not/,
    );
    expect(homeSourceLine(home({ installed: false, launcher_ok: false }), HOME_DIR)).toMatch(
      /launcher link is broken/,
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

  it("adds a failing Launcher check only when the launcher link is broken", () => {
    expect(homeChecks(home({ launcher_ok: true })).launcher).toBeUndefined();
    expect(homeChecks(home()).launcher).toBeUndefined();
    expect(homeChecks(home({ launcher_ok: false, installed: false }))).toEqual({
      hooks: "ok",
      memoryTools: "ok",
      launcher: "err",
    });
  });

  it("tells a half-installed home from one with nothing of Khipu's", () => {
    expect(hasKhipuEntries(t3Home(T3_NOT_INSTALLED))).toBe(false);
    expect(hasKhipuEntries(t3Home({ ...T3_NOT_INSTALLED, hook_stop: true }))).toBe(true);
    expect(hasKhipuEntries(t3Home({ ...T3_NOT_INSTALLED, memory_tools_ok: true }))).toBe(true);
    expect(hasKhipuEntries(t3Home({ ...T3_NOT_INSTALLED, has_khipu: true }))).toBe(true);
    expect(hasKhipuEntries(t3Home({ memory_tools_ok: true, has_khipu: false }))).toBe(false);
    // A linked home's hooks are the owner's, not its own entries.
    expect(hasKhipuEntries(t3Home({ installed: false, memory_tools_ok: false }))).toBe(false);
  });

  it("counts unreadable homes apart from uninstalled ones", () => {
    const row = claudeRow([home(), t3Home({ ...T3_NOT_INSTALLED, error: "settings.json is not valid JSON" })]);
    expect(cardStatus(row, RECORDING, undefined)).toEqual({ tone: "warn", label: "1 home can't be read" });
    const both = claudeRow([
      home(),
      t3Home(T3_NOT_INSTALLED),
      home({ path: "/Users/me/.c3", label: "T3 · Third", installed: false, error: "bad json" }),
    ]);
    expect(cardStatus(both, RECORDING, undefined)).toEqual({
      tone: "warn",
      label: "1 home not installed, 1 can't be read",
    });
    const none = claudeRow([home({ installed: false, error: "bad json" })]);
    expect(cardStatus(none, RECORDING, undefined)).toEqual({ tone: "warn", label: "1 home can't be read" });
  });

  it("reads the CLI's install and uninstall results", () => {
    expect(parseActResults('[{"harness":"claude_code","ok":false,"aborted":true,"error":"bad"}]')[0].error).toBe("bad");
    expect(parseActResults('[{"harness":"claude_code"}]\n{\n  "verify": []\n}')).toHaveLength(1);
    expect(parseActResults('{"harness":"claude_code","ok":false,"error":"unknown home"}')[0].ok).toBe(false);
    expect(parseActResults("not json")).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// The card, driven through the panel.
// ---------------------------------------------------------------------------

function renderPanel(
  rowsFor: () => StatusRow[],
  onInstall: () => void = () => {},
  opts: {
    actResult?: string;
    onToast?: (m: string) => void;
    verifyResult?: string;
    verifyThrows?: string;
    liveness?: { ok: boolean; red?: string[]; harnesses: Record<string, HarnessLiveness> };
    refreshHealth?: () => Promise<void> | void;
  } = {},
) {
  const runKhipu = vi.fn(async (args: string[]) => {
    if (args[0] === "integrations" && args[1] === "status") return JSON.stringify(rowsFor());
    if (args[0] === "integrations" && args[1] === "install") {
      onInstall();
      return opts.actResult ?? '[{"harness":"claude_code","detected":true,"changes":[]}]';
    }
    if (args[0] === "integrations" && args[1] === "verify") {
      if (opts.verifyThrows) throw new Error(opts.verifyThrows);
      return opts.verifyResult ?? "[]";
    }
    if (args[0] === "integrations" && args[1] === "uninstall") {
      return opts.actResult ?? '[{"harness":"claude_code","changes":[]}]';
    }
    return "{}";
  });
  render(
    <IntegrationsPanel
      runKhipu={runKhipu}
      onToast={opts.onToast ?? (() => {})}
      active={true}
      liveness={opts.liveness ?? { ok: true, harnesses: { claude_code: RECORDING } }}
      recallProbe={null}
      refreshHealth={opts.refreshHealth ?? (() => {})}
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
    // The header is the list's label, not one of its items.
    expect(within(list).queryByText("Claude homes")).not.toBeInTheDocument();
    expect(within(card).getByText("Claude homes")).toBeInTheDocument();
    expect(within(card).getByText("2 found")).toBeInTheDocument();

    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText("Default")).toBeInTheDocument();
    expect(within(rows[0]).getByText("~/.claude")).toBeInTheDocument();
    expect(within(rows[0]).getByText("Hooks")).toBeInTheDocument();
    expect(within(rows[0]).getByText("Memory tools")).toBeInTheDocument();
    expect(within(rows[0]).getByText("Installed")).toBeInTheDocument();
    expect(within(rows[0]).getByRole("button", { name: "Remove Khipu from Default" })).toBeInTheDocument();
    expect(within(rows[1]).getByText("T3 · Secondary")).toBeInTheDocument();
    expect(within(rows[1]).getByText("~/.claude-t3-second")).toBeInTheDocument();
    expect(within(rows[1]).getByText(/settings linked to ~\/\.claude, so its hooks are the Default home's/)).toBeInTheDocument();
    expect(within(rows[1]).getByRole("button", { name: "Remove Khipu from T3 · Secondary" })).toBeInTheDocument();

    expect(within(card).getByText(/Khipu finds Claude homes in ~\/\.claude, CLAUDE_CONFIG_DIR and T3's settings/)).toBeInTheDocument();
    // Remove is per home on this card; Reinstall and Verify stay on the card.
    expect(within(card).getAllByRole("button", { name: /^Remove Khipu from / })).toHaveLength(2);
    expect(within(card).getByRole("button", { name: "Reinstall" })).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Verify" })).toBeInTheDocument();
  });

  it("offers Remove for a linked stale Khipu entry reported by the CLI", async () => {
    renderPanel(() => [claudeRow([home(), t3Home({
      ...T3_NOT_INSTALLED,
      has_khipu: true,
      linked_to: { label: "Default", path: DEFAULT_PATH },
    })])]);
    const card = await claudeCard();
    expect(within(card).getByRole("button", { name: "Remove Khipu from T3 · Secondary" })).toBeInTheDocument();
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
    expect(within(rows[1]).getByRole("button", { name: "Install Khipu in T3 · Secondary" })).toBeInTheDocument();
    expect(within(rows[1]).queryByRole("button", { name: /^Remove/ })).not.toBeInTheDocument();
  });

  it("installs and removes one home by its path", async () => {
    const runKhipu = renderPanel(() => [claudeRow([home(), t3Home(T3_NOT_INSTALLED)])]);
    const card = await claudeCard();
    const rows = within(within(card).getByRole("list", { name: "Claude homes" })).getAllByRole("listitem");

    // A per-home install leaves verification to the card's Verify button.
    fireEvent.click(within(rows[1]).getByRole("button", { name: "Install Khipu in T3 · Secondary" }));
    await waitFor(() =>
      expect(runKhipu).toHaveBeenCalledWith([
        "integrations",
        "install",
        "claude_code",
        "--home",
        T3_PATH,
        "--no-verify",
      ]),
    );

    fireEvent.click(within(rows[0]).getByRole("button", { name: "Remove Khipu from Default" }));
    await waitFor(() =>
      expect(runKhipu).toHaveBeenCalledWith(["integrations", "uninstall", "claude_code", "--home", DEFAULT_PATH]),
    );
  });

  it("sends the card's Reinstall for every home, with no --home", async () => {
    const runKhipu = renderPanel(() => [claudeRow([home(), t3Home()])]);
    const card = await claudeCard();

    fireEvent.click(within(card).getByRole("button", { name: "Reinstall" }));
    await waitFor(() =>
      expect(runKhipu).toHaveBeenCalledWith(["integrations", "install", "claude_code", "--no-verify"]),
    );
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

  it("renders the old card when the status has no homes field at all (an older CLI)", async () => {
    const { homes: _homes, ...older } = claudeRow([], {
      detected: true,
      mcp: true,
      hook_stop: true,
      hook_precompact: true,
      recall_rule: "installed",
    });
    renderPanel(() => [older as StatusRow]);
    const card = await claudeCard();

    expect(within(card).getByText("Recording")).toBeInTheDocument();
    expect(within(card).queryByRole("list", { name: "Claude homes" })).not.toBeInTheDocument();
    expect(card).not.toHaveClass("homes-card");
    expect(within(card).getByText("~/.claude.json · ~/.claude/settings.json")).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Reinstall" })).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Remove" })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Rows in the states the CLI really reports.
// ---------------------------------------------------------------------------

/** A linked T3 home with no Khipu of its own, shaped like one row of
 *  `khipu integrations status claude_code` → `homes[]`: its settings.json is
 *  a link to ~/.claude, so the hooks read present, but its .claude.json has no
 *  memory tools, so it is not installed. */
const T3_LINKED_NOT_INSTALLED: ClaudeHomeRow = {
  path: T3_PATH,
  label: "T3 · Secondary",
  source: "T3's “Secondary” account",
  is_default: false,
  exists: true,
  settings_path: `${DEFAULT_PATH}/settings.json`,
  mcp_paths: [`${T3_PATH}/.claude.json`],
  linked_to: { label: "Default", path: DEFAULT_PATH },
  hook_stop: true,
  hook_precompact: true,
  hook_sessionend: true,
  hook_subagentstop: true,
  recall_rule: "installed",
  prompt_recall: "installed",
  memory_tools_ok: false,
  launcher_ok: true,
  hooks_ok: true,
  installed: false,
};

describe("IntegrationsPanel — home row states", () => {
  async function rowsOf(homes: ClaudeHomeRow[], opts?: Parameters<typeof renderPanel>[2]) {
    const runKhipu = renderPanel(() => [claudeRow(homes)], undefined, opts);
    const card = await claudeCard();
    const rows = within(within(card).getByRole("list", { name: "Claude homes" })).getAllByRole("listitem");
    return { runKhipu, card, rows };
  }

  it("shows a linked home with hooks but no memory tools as Not installed, with Install", async () => {
    const { card, rows } = await rowsOf([home(), T3_LINKED_NOT_INSTALLED]);
    const row = rows[1];

    expect(within(row).getByText(/settings linked to ~\/\.claude, so its hooks are the Default home's/)).toBeInTheDocument();
    expect(within(row).getByText(/^Hooks/).textContent).toMatch(/: installed/);
    expect(within(row).getByText(/^Memory tools/).textContent).toMatch(/: not installed/);
    expect(within(row).getByText("Not installed")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Install Khipu in T3 · Secondary" })).toBeInTheDocument();
    // The hooks it shows are the Default home's: nothing of its own to remove.
    expect(within(row).queryByRole("button", { name: /^Remove/ })).not.toBeInTheDocument();
    expect(within(card).getByText("1 home not installed")).toBeInTheDocument();
  });

  it("shows a Launcher mark on a row whose launcher link is broken, and offers Install and Remove", async () => {
    const { rows } = await rowsOf([
      home(),
      t3Home({ ...T3_NOT_INSTALLED, memory_tools_ok: true, hook_stop: true, hook_precompact: true, hooks_ok: true, launcher_ok: false }),
    ]);
    const row = rows[1];

    expect(within(row).getByText(/^Launcher/).textContent).toMatch(/: broken/);
    expect(within(row).getByText(/a Khipu launcher link is broken, Install repairs it/)).toBeInTheDocument();
    expect(within(row).getByText("Not installed")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Install Khipu in T3 · Secondary" })).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Remove Khipu from T3 · Secondary" })).toBeInTheDocument();
    // An installed home shows no Launcher line at all.
    expect(within(rows[0]).queryByText(/^Launcher/)).not.toBeInTheDocument();
  });

  it("offers Install and Remove on a home with hooks in and no memory tools", async () => {
    const { rows } = await rowsOf([
      home(),
      t3Home({ ...T3_NOT_INSTALLED, hook_stop: true, hook_precompact: true, hooks_ok: true }),
    ]);
    expect(within(rows[1]).getByRole("button", { name: /^Install Khipu in/ })).toBeInTheDocument();
    expect(within(rows[1]).getByRole("button", { name: /^Remove Khipu from/ })).toBeInTheDocument();
    expect(within(rows[1]).queryByText(/without memory/)).not.toBeInTheDocument();
  });

  it("renders a Not found row with no actions", async () => {
    const { card, rows } = await rowsOf([home(), t3Home({ ...T3_NOT_INSTALLED, exists: false })]);
    const row = rows[1];

    expect(within(row).getByText("Not found")).toBeInTheDocument();
    expect(within(row).getByText(/folder not found, nothing to install yet/)).toBeInTheDocument();
    expect(within(row).getAllByText(": not checked")).toHaveLength(2);
    expect(within(row).queryByRole("button")).not.toBeInTheDocument();
    expect(within(card).getByText("1 found")).toBeInTheDocument();
    expect(within(card).getByText("Recording")).toBeInTheDocument();
  });

  it("renders a Can't read row with no actions, and counts it as unreadable on the card", async () => {
    const { card, rows } = await rowsOf([
      home(),
      t3Home({ ...T3_NOT_INSTALLED, error: "settings.json is not valid JSON" }),
    ]);
    const row = rows[1];

    expect(within(row).getByText("Can't read")).toBeInTheDocument();
    expect(within(row).getByText(/couldn't read its config: settings\.json is not valid JSON/)).toBeInTheDocument();
    expect(within(row).queryByRole("button")).not.toBeInTheDocument();
    expect(within(card).getByText("1 home can't be read")).toBeInTheDocument();
    expect(within(card).queryByText("1 home not installed")).not.toBeInTheDocument();
  });

  it("does not abbreviate another user's folder as ~", async () => {
    const { rows } = await rowsOf([home(), t3Home({ ...T3_NOT_INSTALLED, path: "/Users/shared/.claude-team" })]);
    expect(within(rows[1]).getByText("/Users/shared/.claude-team")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// What a click does: toasts, reloads, focus, auto-verify.
// ---------------------------------------------------------------------------

describe("IntegrationsPanel — per-home actions", () => {
  const statusCalls = (runKhipu: ReturnType<typeof renderPanel>) =>
    runKhipu.mock.calls.filter((c) => c[0][1] === "status").length;

  it("toasts the CLI's error, not a success, when an install aborts, and reloads the rows", async () => {
    const toasts: string[] = [];
    const runKhipu = renderPanel(() => [claudeRow([home(), t3Home(T3_NOT_INSTALLED)])], undefined, {
      actResult:
        '[{"harness":"claude_code","ok":false,"aborted":true,"error":"/Users/me/.claude.json is not valid JSON","homes":[]}]',
      onToast: (m) => toasts.push(m),
    });
    const card = await claudeCard();
    await waitFor(() => expect(statusCalls(runKhipu)).toBeGreaterThan(0));
    const before = statusCalls(runKhipu);

    fireEvent.click(within(card).getByRole("button", { name: "Install Khipu in T3 · Secondary" }));
    await waitFor(() => expect(toasts).toHaveLength(1));

    expect(toasts[0]).toBe("install failed: /Users/me/.claude.json is not valid JSON");
    expect(toasts[0]).not.toMatch(/Installed/);
    await waitFor(() => expect(statusCalls(runKhipu)).toBeGreaterThan(before));
  });

  it("reloads the rows after a failed command too", async () => {
    const toasts: string[] = [];
    const runKhipu = vi.fn(async (args: string[]) => {
      if (args[1] === "status") return JSON.stringify([claudeRow([home(), t3Home(T3_NOT_INSTALLED)])]);
      throw new Error("khipu exited 2");
    });
    render(
      <IntegrationsPanel
        runKhipu={runKhipu}
        onToast={(m) => toasts.push(m)}
        active={true}
        liveness={{ ok: true, harnesses: { claude_code: RECORDING } }}
        recallProbe={null}
        refreshHealth={() => {}}
        onAnotherMac={() => {}}
      />,
    );
    const card = await claudeCard();
    const before = statusCalls(runKhipu as unknown as ReturnType<typeof renderPanel>);
    fireEvent.click(within(card).getByRole("button", { name: "Install Khipu in T3 · Secondary" }));
    await waitFor(() => expect(toasts[0]).toMatch(/^install failed: Error: khipu exited 2/));
    await waitFor(() =>
      expect(statusCalls(runKhipu as unknown as ReturnType<typeof renderPanel>)).toBeGreaterThan(before),
    );
  });

  it("toasts that the hooks were kept when a remove leaves them to another home", async () => {
    const toasts: string[] = [];
    renderPanel(() => [claudeRow([home(), t3Home()])], undefined, {
      actResult:
        '[{"harness":"claude_code","changes":[],"homes":[{"home":"/Users/me/.claude","label":"Default","hooks_kept":"still used by T3 · Secondary"}]}]',
      onToast: (m) => toasts.push(m),
    });
    const card = await claudeCard();
    fireEvent.click(within(card).getByRole("button", { name: "Remove Khipu from Default" }));
    await waitFor(() => expect(toasts).toHaveLength(1));

    expect(toasts[0]).toMatch(/Removed the memory tools/);
    expect(toasts[0]).toMatch(/hooks were kept \(still used by T3 · Secondary\)/);
  });

  it("says plainly that a plain remove took everything out", async () => {
    const toasts: string[] = [];
    renderPanel(() => [claudeRow([home(), t3Home()])], undefined, { onToast: (m) => toasts.push(m) });
    const card = await claudeCard();
    fireEvent.click(within(card).getByRole("button", { name: "Remove Khipu from Default" }));
    await waitFor(() => expect(toasts).toEqual(["Removed Khipu entries. Backups kept next to each file."]));
  });

  it("does not let one home's install arm auto-verify from another home's capture", async () => {
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
    fireEvent.click(within(card).getByRole("button", { name: "Install Khipu in T3 · Secondary" }));
    await waitFor(() => expect(installedAtIso).not.toBe(""));
    window.dispatchEvent(new Event("focus"));
    await waitFor(() => expect(statusCalls(runKhipu)).toBeGreaterThan(2));

    expect(within(card).queryByText(/Verified — a session ran the hook after Install/)).not.toBeInTheDocument();
    expect(within(card).queryByText(/this card turns green by itself/)).not.toBeInTheDocument();
  });

  it("keeps keyboard focus on the row's action when Install and Remove swap", async () => {
    let installed = false;
    renderPanel(() => [claudeRow([home(), t3Home(installed ? {} : T3_NOT_INSTALLED)])], () => {
      installed = true;
    });
    const card = await claudeCard();
    const install = within(card).getByRole("button", { name: "Install Khipu in T3 · Secondary" });
    install.focus();
    fireEvent.click(install);

    const remove = await within(card).findByRole("button", { name: "Remove Khipu from T3 · Secondary" });
    await waitFor(() => expect(remove).toHaveFocus());
  });
});

// ---------------------------------------------------------------------------
// Install runs without the CLI's own verify and verifies as a second call: a
// failed verify exits 2 with two JSON documents, which the Tauri runner reports
// as an error although the files were written.
// ---------------------------------------------------------------------------

const CURSOR_MISSING: StatusRow = {
  harness: "cursor",
  detected: true,
  mcp: false,
  hook_stop: false,
  hook_precompact: false,
  recall_rule: "missing",
};
const VERIFY_OK = '[{"harness":"claude_code","detected":true,"ok":true,"components":{"mcp":{"ok":true}}}]';
const VERIFY_BAD =
  '[{"harness":"claude_code","detected":true,"ok":false,"components":{"mcp":{"ok":true},"hook":{"ok":false,"error":"Stop hook not found"}}}]';
const INSTALL_OK_COPY = "Installed. Restart each harness, then start any session — its card turns green by itself.";

type EntryPoint = {
  name: string;
  harness: string;
  rows: () => StatusRow[];
  liveness?: { ok: boolean; red?: string[]; harnesses: Record<string, HarnessLiveness> };
  click: () => Promise<void>;
};

const ENTRY_POINTS: EntryPoint[] = [
  {
    name: "the card's Install",
    harness: "cursor",
    rows: () => [CURSOR_MISSING],
    click: async () => void fireEvent.click(await screen.findByRole("button", { name: "Install" })),
  },
  {
    name: "Install all",
    harness: "all",
    rows: () => [claudeRow([home(), t3Home()]), CURSOR_MISSING],
    click: async () => void fireEvent.click(await screen.findByRole("button", { name: "Install all" })),
  },
  {
    name: "Reinstall hook",
    harness: "claude_code",
    rows: () => [claudeRow([home(), t3Home()])],
    liveness: { ok: false, red: ["claude_code"], harnesses: { claude_code: { ok: false, seen: true, captures: 4 } } },
    click: async () => void fireEvent.click(await screen.findByRole("button", { name: "Reinstall hook" })),
  },
];

describe.each(ENTRY_POINTS)("IntegrationsPanel — $name", (ep) => {
  const setup = (opts: Parameters<typeof renderPanel>[2]) => {
    const toasts: string[] = [];
    const runKhipu = renderPanel(ep.rows, undefined, { ...opts, liveness: ep.liveness, onToast: (m) => toasts.push(m) });
    return { runKhipu, toasts };
  };
  const calls = (runKhipu: ReturnType<typeof renderPanel>) => runKhipu.mock.calls.map((c) => c[0]);

  it("installs without verify, verifies separately, and toasts success when both pass", async () => {
    const { runKhipu, toasts } = setup({ verifyResult: VERIFY_OK });
    await ep.click();
    await waitFor(() => expect(toasts).toHaveLength(1));

    expect(toasts[0]).toBe(INSTALL_OK_COPY);
    const argv = calls(runKhipu).filter((a) => a[1] === "install" || a[1] === "verify");
    expect(argv).toEqual([
      ["integrations", "install", ep.harness, "--no-verify"],
      ["integrations", "verify", ep.harness],
    ]);
  });

  it("warns 'Installed; verify failed' and never 'install failed' when only verify fails", async () => {
    const { toasts } = setup({ verifyResult: VERIFY_BAD });
    await ep.click();
    await waitFor(() => expect(toasts).toHaveLength(1));

    expect(toasts[0]).toBe("Installed; verify failed: claude_code hook: Stop hook not found");
    expect(toasts[0]).not.toMatch(/install failed/);
  });

  it("warns the same way when the verify call itself errors", async () => {
    const { toasts } = setup({ verifyThrows: "khipu exited 2" });
    await ep.click();
    await waitFor(() => expect(toasts).toHaveLength(1));

    expect(toasts[0]).toMatch(/^Installed; verify failed: Error: khipu exited 2/);
  });

  it("shows the CLI's error, and does not verify, when the install did not finish", async () => {
    const { runKhipu, toasts } = setup({
      actResult: '[{"harness":"claude_code","ok":false,"aborted":true,"error":"/Users/me/.claude.json is not valid JSON"}]',
    });
    await ep.click();
    await waitFor(() => expect(toasts).toHaveLength(1));

    expect(toasts[0]).toBe("install failed: /Users/me/.claude.json is not valid JSON");
    expect(calls(runKhipu).some((a) => a[1] === "verify")).toBe(false);
  });
});

describe("IntegrationsPanel — after an action", () => {
  it("feeds the separate verify result into the card's verify state", async () => {
    const toasts: string[] = [];
    renderPanel(() => [claudeRow([home(), t3Home()])], undefined, {
      verifyResult:
        '[{"harness":"claude_code","detected":true,"ok":false,"components":{"mcp":{"ok":false,"error":"handshake failed"}}}]',
      onToast: (m) => toasts.push(m),
    });
    const card = await claudeCard();
    fireEvent.click(within(card).getByRole("button", { name: "Reinstall" }));
    await waitFor(() => expect(toasts).toHaveLength(1));

    expect(within(card).getByText("Memory tools (MCP) · handshake failed")).toBeInTheDocument();
  });

  it("keeps the buttons busy until the forced doctor refresh has answered", async () => {
    let release: () => void = () => {};
    const refresh = vi.fn(() => new Promise<void>((resolve) => (release = resolve)));
    renderPanel(() => [claudeRow([home(), t3Home()])], undefined, { refreshHealth: refresh });
    const card = await claudeCard();
    fireEvent.click(within(card).getByRole("button", { name: "Reinstall" }));

    await waitFor(() => expect(refresh).toHaveBeenCalled());
    expect(within(card).getByRole("button", { name: "Verify" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Install all" })).toBeDisabled();

    release();
    await waitFor(() => expect(within(card).getByRole("button", { name: "Verify" })).not.toBeDisabled());
  });
});
