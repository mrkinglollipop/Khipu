// Slice G: the Cursor card's per-function rows, the Antigravity card, and the
// T3 provider rows. Pure helpers first, then the panel driven with a scripted
// `khipu integrations` and a fake of the Tauri-backed project actions.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { IntegrationsPanel, parseStatusRows } from "../IntegrationsPanel";
import type { AntigravityApi, StatusRow } from "../IntegrationsPanel";
import {
  antigravityT3Rows,
  cursorFunctionRows,
  parseProof,
  projectLine,
  plainNote,
  providerSummary,
  shortDate,
} from "../harnessFunctions";
import { antigravityStatus } from "../AntigravityCard";
import {
  AG_ABSENT,
  AG_BARE,
  AG_ON_MAC,
  CURSOR_LIVENESS,
  CURSOR_PROBE,
  CURSOR_ROW,
  KHIPU_PROJECT,
  T3_PROVIDERS,
  T3_REAL,
} from "./harnessCardsFixtures";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn(async () => "{}") }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ open: vi.fn(async () => null) }));

afterEach(() => cleanup());

describe("proof words", () => {
  it("reads the CLI's proof table and short dates", () => {
    expect(shortDate("2026-10-27")).toBe("Oct 27");
    expect(shortDate("2026-10-27T15:20:00+00:00")).toBe("Oct 27");
    expect(shortDate("last week")).toBeNull();
    expect(parseProof("real-run 2026-10-27")).toEqual({ kind: "proven", date: "2026-10-27" });
    expect(parseProof("not-seen 2026-10-27").kind).toBe("not-seen");
    expect(parseProof("hermetic").kind).toBe("hermetic");
    expect(parseProof(undefined).kind).toBe("unproven");
  });
});

describe("Cursor's function rows", () => {
  it("shows proven, not seen and waiting for proof, never red", () => {
    const rows = cursorFunctionRows(CURSOR_ROW, true)!;
    expect(rows.map((r) => [r.key, r.mark, r.tag?.label])).toEqual([
      ["session_recall", "ok", "proven Oct 27"],
      ["prompt_recall", "off", "not seen"],
      ["delegation", "wait", undefined],
    ]);
    expect(rows[0].text).toBe("Recall at session start · once per session");
    expect(rows[2].text).toBe("Routing after a delegation · installed, waiting for proof");
  });

  it("is amber when a function is missing from an installed pack, and null from an older CLI", () => {
    const rows = cursorFunctionRows({ ...CURSOR_ROW, prompt_recall: "missing" }, true)!;
    expect(rows[1]).toMatchObject({ mark: "warn", text: "Recall on each prompt · Reinstall adds it" });
    expect(cursorFunctionRows({ ...CURSOR_ROW, proof: undefined }, true)).toBeNull();
  });
});

describe("T3 provider rows", () => {
  it("counts features and calls anything short of complete Partial, in amber", () => {
    const [cursor, antigravity] = T3_PROVIDERS.providers!.map(providerSummary);
    expect(cursor).toMatchObject({ line: "Cursor · 4 of 5 features, Reinstall adds the rest", tag: { tone: "warn", label: "Partial" } });
    expect(antigravity.line).toBe("Antigravity · 2 of 5 features, capture and tools in 1 project; recall not possible in T3");
    expect(antigravity.tag).toEqual({ tone: "warn", label: "Partial" });
  });

  it("says All 5 features (green) when every feature is in, and Not added to any project (neutral) when none is", () => {
    const all = { driver: "cursor", name: "Cursor", hooks: "installed", functions: { capture: "installed", start_block: "installed", prompt_recall: "installed", delegation: "installed", mcp: "installed" } };
    expect(providerSummary(all).tag).toEqual({ tone: "ok", label: "All 5 features" });
    const none = { driver: "antigravity", name: "Antigravity", hooks: "missing", functions: { capture: "missing", mcp: "missing", start_block: "unsupported", prompt_recall: "unsupported", delegation: "unsupported" } };
    expect(providerSummary(none).tag).toEqual({ tone: "neutral", label: "Not added to any project" });
  });

  it("uses the display name, not T3's lowercase instance name, and never red", () => {
    const [cursor, antigravity] = T3_REAL.providers!.map(providerSummary);
    expect(cursor.name).toBe("Cursor");
    expect(cursor.line).toBe("Cursor");
    expect(cursor.tag).toEqual({ tone: "ok", label: "All 5 features" });
    expect(antigravity.name).toBe("Antigravity");
    expect(antigravity.line).toBe("Antigravity · add a project to start");
    expect(antigravity.tag.tone).toBe("neutral");
  });

  it("drops the 64-character runtime hash and capitalises provider names in notes", () => {
    const text = T3_REAL.notes!.map(plainNote).join(" ");
    expect(text).not.toMatch(/[0-9a-f]{16,}/);
    expect(text).toContain("T3 runs Antigravity without");
    expect(text).toContain("T3's Antigravity runtime");
  });

  it("reads recall as a harness limit, not a gap", () => {
    const rows = antigravityT3Rows(T3_PROVIDERS.providers![1].functions, [KHIPU_PROJECT]);
    expect(rows.map((r) => r.mark)).toEqual(["ok", "ok", "na"]);
    expect(antigravityT3Rows(undefined, []).map((r) => r.mark)).toEqual(["off", "off", "na"]);
  });

  it("names a project and when it was added", () => {
    expect(projectLine(KHIPU_PROJECT)).toEqual({ name: "Khipu", note: "added Oct 27", warn: false });
    expect(projectLine({ ...KHIPU_PROJECT, installed: false }).warn).toBe(true);
  });
});

describe("Antigravity's verdict", () => {
  const base = { detected: false, macInstalled: false, projectsInstalled: 0, t3Present: true, lvOk: undefined, recording: false };
  it("reads In T3 only for project files with no Mac install, amber not red", () => {
    expect(antigravityStatus({ ...base, projectsInstalled: 1 })).toEqual({ tone: "warn", label: "In T3 only" });
    expect(antigravityStatus(base)).toEqual({ tone: "neutral", label: "Not set up" });
    expect(antigravityStatus({ ...base, t3Present: false })).toEqual({ tone: "neutral", label: "Not on this Mac" });
    // The ~/.gemini folder is there but Khipu is not: grey, never red.
    expect(antigravityStatus({ ...base, detected: true })).toEqual({ tone: "neutral", label: "Not set up" });
    expect(antigravityStatus({ ...base, macInstalled: true, detected: true, recording: true })).toEqual({ tone: "ok", label: "Recording" });
  });
});

describe("status parsing", () => {
  it("accepts an Antigravity row, which has no PreCompact hook", () => {
    const { hook_precompact: _drop, ...row } = AG_ON_MAC;
    const parsed = parseStatusRows(JSON.stringify([row]));
    expect(parsed?.[0].hook_precompact).toBe(false);
  });
});

// ---------------------------------------------------------------------------

type Opts = { rows?: StatusRow[]; t3?: typeof T3_PROVIDERS | null; api?: Partial<AntigravityApi> };

function fakeApi(over: Partial<AntigravityApi> = {}): AntigravityApi {
  return {
    list: vi.fn(async () => ({ ok: true as const, projects: [KHIPU_PROJECT] })),
    run: vi.fn(async () => ({ ok: true as const, notes: ["If Antigravity asks whether to trust this workspace's hooks, choose Trust."] })),
    pickFolder: vi.fn(async () => "/Users/me/code/Other"),
    readTrust: vi.fn(async () => false),
    setTrust: vi.fn(async () => ({ ok: true as const })),
    ...over,
  };
}

function renderCards(opts: Opts = {}) {
  const toasts: string[] = [];
  const api = fakeApi(opts.api);
  const runKhipu = vi.fn(async (args: string[]) => {
    if (args[0] === "integrations" && args[1] === "status") return JSON.stringify(opts.rows ?? [CURSOR_ROW, AG_ABSENT]);
    return "[]";
  });
  render(
    <IntegrationsPanel
      runKhipu={runKhipu}
      onToast={(m) => toasts.push(m)}
      active
      liveness={{ ok: true, harnesses: { cursor: CURSOR_LIVENESS } }}
      recallProbe={CURSOR_PROBE}
      t3={opts.t3 === undefined ? T3_PROVIDERS : opts.t3}
      refreshHealth={() => {}}
      onAnotherMac={() => {}}
      antigravityApi={api}
    />,
  );
  return { runKhipu, toasts, api };
}

const agCard = async () => (await screen.findByText("Antigravity", { selector: ".name" })).closest(".hcard") as HTMLElement;

describe("the Cursor card", () => {
  it("shows one row per function with proof, its files and Reinstall, Verify, Remove", async () => {
    renderCards();
    const card = (await screen.findByText("Cursor", { selector: ".name" })).closest(".hcard") as HTMLElement;
    expect(within(card).getByText(/Capture hook · last capture 29 s ago|Capture hook · last capture 29s ago/)).toBeInTheDocument();
    expect(within(card).getByText("proven Oct 27")).toBeInTheDocument();
    expect(within(card).getByText("not seen")).toBeInTheDocument();
    expect(within(card).getByText(/Routing after a delegation · installed, waiting for proof/)).toBeInTheDocument();
    expect(card.querySelector(".ck.wait")).not.toBeNull();
    expect(card.querySelector(".ck.err")).toBeNull();
    expect(within(card).getByText(/~\/\.cursor\/hooks\.json/)).toBeInTheDocument();
    for (const name of ["Reinstall", "Verify", "Remove"]) expect(within(card).getByRole("button", { name })).toBeEnabled();
  });
});

describe("the Antigravity card", () => {
  it("with no app: says so, offers no install, and lists the T3 projects with Remove", async () => {
    renderCards();
    const card = await agCard();
    expect(within(card).getByText("On this Mac")).toBeInTheDocument();
    expect(within(card).getByText(/The Antigravity app isn't installed/)).toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: /Install on this Mac|Reinstall/ })).toBeNull();
    expect(within(card).getByText("In T3 only")).toBeInTheDocument();
    expect(within(card).getByText("Inside T3, per project")).toBeInTheDocument();
    const list = await within(card).findByRole("list", { name: "Projects Khipu was added to" });
    expect(within(list).getByText("Khipu")).toBeInTheDocument();
    expect(within(list).getByText(/added Oct 27/)).toBeInTheDocument();
    expect(within(card).getByText(/writes three files in its \.agents folder/)).toBeInTheDocument();
    expect(card.textContent).not.toMatch(/terminal|command line|khipu t3/i);
  });

  it("with the Antigravity folder but nothing of Khipu in it: one grey line, a grey badge, no red", async () => {
    renderCards({ rows: [CURSOR_ROW, AG_BARE], t3: T3_REAL, api: { list: vi.fn(async () => ({ ok: true as const, projects: [] })) } });
    const card = await agCard();
    expect(within(card).getByText(/Khipu isn't installed in Antigravity on this Mac/)).toBeInTheDocument();
    expect(within(card).getByText("Not set up")).toBeInTheDocument();
    expect(card.querySelector(".ck.err")).toBeNull();
    expect(within(card).queryByText(/Install adds it/)).toBeNull();
    expect(within(card).getByRole("button", { name: "Install on this Mac" })).toBeEnabled();
  });

  it("the T3 card shows provider names and tags as words, with no hash", async () => {
    renderCards({ rows: [CURSOR_ROW, AG_BARE], t3: T3_REAL });
    const providers = await screen.findByRole("list", { name: "T3 providers, feature by feature" });
    expect(providers.textContent).toContain("Cursor");
    expect(providers.textContent).toContain("All 5 features");
    expect(providers.textContent).toContain("Not added to any project");
    expect(providers.textContent).not.toMatch(/cursor ·|antigravity ·/);
    expect(document.body.textContent).not.toMatch(/[0-9a-f]{40,}/);
  });

  it("offers a one-click install when the app is on this Mac", async () => {
    const { runKhipu } = renderCards({ rows: [CURSOR_ROW, { ...AG_ABSENT, detected: true }] });
    const card = await agCard();
    fireEvent.click(within(card).getByRole("button", { name: "Install on this Mac" }));
    await waitFor(() => expect(runKhipu.mock.calls.some((c) => c[0][1] === "install" && c[0][2] === "antigravity")).toBe(true));
  });

  it("Add to a project… picks a folder, runs add on it and says what Antigravity will ask", async () => {
    const { api, toasts } = renderCards();
    const card = await agCard();
    fireEvent.click(within(card).getByRole("button", { name: "Add to a project…" }));
    await waitFor(() => expect(toasts).toHaveLength(1));
    expect(api.run).toHaveBeenCalledWith("add", "/Users/me/code/Other");
    expect(toasts[0]).toBe("Added Khipu to Other. If Antigravity asks whether to trust this workspace's hooks, choose Trust.");
  });

  it("does nothing when the folder picker is cancelled", async () => {
    const { api, toasts } = renderCards({ api: { pickFolder: vi.fn(async () => null) } });
    fireEvent.click(within(await agCard()).getByRole("button", { name: "Add to a project…" }));
    await new Promise((r) => setTimeout(r, 30));
    expect(api.run).not.toHaveBeenCalled();
    expect(toasts).toEqual([]);
  });

  it("reports a refusal in the CLI's words", async () => {
    const { toasts } = renderCards({ api: { run: vi.fn(async () => ({ ok: false as const, error: "/x is T3's private state" })) } });
    fireEvent.click(within(await agCard()).getByRole("button", { name: "Add to a project…" }));
    await waitFor(() => expect(toasts).toHaveLength(1));
    expect(toasts[0]).toBe("Couldn't add Khipu to Other: /x is T3's private state");
  });

  it("Remove takes Khipu out of that project only", async () => {
    const { api, toasts } = renderCards();
    const card = await agCard();
    fireEvent.click(await within(card).findByRole("button", { name: "Remove Khipu from Khipu" }));
    await waitFor(() => expect(toasts).toHaveLength(1));
    expect(api.run).toHaveBeenCalledWith("remove", "/Users/me/code/Khipu");
    expect(toasts[0]).toMatch(/^Removed Khipu from Khipu\./);
  });

  it("Verify with no Mac install reads every project's files", async () => {
    const { toasts } = renderCards();
    fireEvent.click(within(await agCard()).getByRole("button", { name: "Verify" }));
    await waitFor(() => expect(toasts).toHaveLength(1));
    expect(toasts[0]).toMatch(/^Khipu's three files are in the project\./);
  });

  it("keeps the direction switch off and disabled until a real run proves it", async () => {
    const { api } = renderCards();
    const sw = await within(await agCard()).findByRole("switch", { name: "Count prompts typed here as your directions" });
    expect(sw).not.toBeChecked();
    expect(sw).toBeDisabled();
    expect(screen.getByText("Off until a real run proves it")).toBeInTheDocument();
    expect(api.setTrust).not.toHaveBeenCalled();
  });

  it("once proven, the switch goes through the owner-direction path", async () => {
    const proven = { ...AG_ABSENT, proof: { ...AG_ABSENT.proof!, workspace_files: "real-run 2026-11-02" } };
    const { api } = renderCards({ rows: [CURSOR_ROW, proven] });
    const sw = await within(await agCard()).findByRole("switch", { name: "Count prompts typed here as your directions" });
    await waitFor(() => expect(sw).toBeEnabled());
    fireEvent.click(sw);
    await waitFor(() => expect(api.setTrust).toHaveBeenCalledWith(true));
  });

  it("an already-trusted front end can always be turned off", async () => {
    const { api } = renderCards({ api: { readTrust: vi.fn(async () => true) } });
    const sw = await within(await agCard()).findByRole("switch", { name: "Count prompts typed here as your directions" });
    await waitFor(() => expect(sw).toBeChecked());
    expect(sw).toBeEnabled();
    fireEvent.click(sw);
    await waitFor(() => expect(api.setTrust).toHaveBeenCalledWith(false));
  });

  it("shows no T3 section on a Mac with neither T3 nor Antigravity", async () => {
    renderCards({ t3: null, api: { list: vi.fn(async () => ({ ok: true as const, projects: [] })) } });
    const card = await agCard();
    expect(within(card).getByText("Not on this Mac")).toBeInTheDocument();
    expect(within(card).queryByText("Inside T3, per project")).toBeNull();
    expect(within(card).queryByRole("button", { name: "Add to a project…" })).toBeNull();
  });
});

describe("the T3 providers rows", () => {
  it("show Partial in amber with N of M features and the plain notes", async () => {
    renderCards();
    const t3 = (await screen.findByText("T3 Code")).closest(".hcard") as HTMLElement;
    const providers = within(t3).getByRole("list", { name: "T3 providers, feature by feature" });
    const items = within(providers).getAllByRole("listitem");
    expect(items[0].textContent).toContain("Cursor · 4 of 5 features");
    expect(items[1].textContent).toContain("recall not possible in T3");
    for (const item of items) {
      const tag = within(item).getByText("Partial");
      expect(tag.className).toMatch(/\bwarn\b/);
      expect(tag.className).not.toMatch(/\berr\b/);
    }
    expect(within(t3).getByText(/routing reaches it through khipu_route and the brief/)).toBeInTheDocument();
    expect(within(t3).getByText(/Partial is amber, never red/)).toBeInTheDocument();
  });
});
