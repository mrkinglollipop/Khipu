// Settings clarity phase 9, slice 9C (docs/plans/2026-10-15-settings-clarity.md,
// "Phase 9"; mocks docs/mockups/2026-10-15-settings-clarity/add-account*.html):
// the Add an account dialog, Found rows, Home's cards, managing an account and
// setup's step. Props-driven, one test per mock state, plus the command-level
// checks the slice names (no check before Check, Close turns nothing down,
// automatic tracking off saves nothing, Sign in now saves first).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { AddAccountDialog } from "../AddAccountDialog";
import type { AddAccountStart } from "../AddAccountDialog";
import {
  AccountExtras,
  AccountsGroupBar,
  AutoTrackSwitch,
  CliSettingsRows,
  FinderTop,
  FoundRows,
  HomeAccountCards,
  HomeAutoCard,
  HomeFoundCard,
  NoAccountsTracked,
  NotOfferedAgain,
  RenameDialog,
  StopTrackingDialog,
  addQuietLogins,
  homeOffersOf,
  movedPairs,
  quietLogins,
  useAccountsFinder,
} from "../FoundAccounts";
import type { AccountRefs } from "../FoundAccounts";
import { COMING, LIST_MOVED, discoverAccounts, lastRunLine, normalDiscover, saveNewAccount, signInNow, useDiscovery, versionRefused } from "../accountsApi";
import type { DiscoverResult, Support } from "../accountsApi";
import { SetupAccountsStep } from "../SetupAccounts";
import { POST_UPDATE_NOTICES as NOTICES } from "../postUpdateNotices";
import {
  CLIENT,
  CLIS_ALL,
  CLIS_CODEX_CANT,
  CLIS_CODEX_NOT,
  CLIS_NONE,
  CODEX_LAB,
  CODEX_WORK,
  GROK_SECOND,
  NOW,
  OFFICE,
  OLD,
  TRACKED,
  discovery,
  liveRow,
  status9B,
} from "./accountsFixtures";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));
const pickerMock = vi.fn(async (_opts?: unknown): Promise<string | null> => null);
vi.mock("@tauri-apps/plugin-dialog", () => ({
  open: (opts: unknown) => pickerMock(opts),
  save: vi.fn(async () => null),
}));

const READING: Support = { state: "reading" };
const HERE: Support = { state: "here", status: status9B() };

const calls = (name: string) => invokeMock.mock.calls.filter((c) => c[0] === name);
const names = () => invokeMock.mock.calls.map((c) => c[0] as string);

let store: Map<string, string>;
beforeEach(() => {
  store = new Map();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  invokeMock.mockReset();
  pickerMock.mockReset();
  pickerMock.mockImplementation(async () => null);
});

/** A fake Khipu: discovery, `khipu accounts status` (failing when `nineB`
 *  is false), the account list and the writes, each answered as the CLI
 *  prints it. */
function fakeKhipu(o: {
  disc?: DiscoverResult;
  nineB?: boolean;
  status?: ReturnType<typeof status9B>;
  setRefusal?: Record<string, unknown>;
  trackRefusal?: Record<string, unknown>;
  check?: DiscoverResult;
} = {}) {
  invokeMock.mockImplementation(async (cmd: string) => {
    switch (cmd) {
      case "khipu_accounts_discover":
        return JSON.stringify(o.disc ?? discovery());
      case "khipu_accounts_check":
        return JSON.stringify(o.check ?? o.disc ?? discovery());
      case "khipu_accounts_status":
        if (o.nineB === false) throw new Error("khipu accounts status failed");
        return JSON.stringify(o.status ?? status9B());
      case "khipu_config_show":
        return JSON.stringify({
          usage_accounts: { source: "default", accounts: TRACKED, user_home: "/home/someone" },
          cli_settings: {},
        });
      case "khipu_config_set":
        return JSON.stringify(o.setRefusal ?? { ok: true });
      case "khipu_accounts_track":
        return JSON.stringify(o.trackRefusal ?? { ok: true, version: "v13", accounts: [{ key: "claude|~/.claude-old", label: "Claude old" }], rows: [] });
      case "khipu_account_sign_in":
        return JSON.stringify({ ok: true, row: liveRow({ account: "Claude old" }) });
      case "khipu_accounts_try":
        return JSON.stringify({ ok: true, default: false, row: liveRow() });
      default:
        return JSON.stringify({ ok: true });
    }
  });
}

function Dialog(props: { start?: AddAccountStart; disc?: DiscoverResult | null; support?: Support; tracked?: typeof TRACKED;
  onRediscover?: () => Promise<DiscoverResult | null>; canSignInNew?: boolean }) {
  return (
    <AddAccountDialog open onClose={() => {}} discovery={props.disc === undefined ? discovery() : props.disc}
      onRediscover={props.onRediscover ?? (async () => null)} onCheck={async () => ({ ok: false, error: "folder_not_found" })}
      tracked={props.tracked ?? TRACKED} shownDefaults={TRACKED} support={props.support ?? HERE} start={props.start}
      onSaved={() => {}} canSignInNew={props.canSignInNew} />
  );
}

// ---------------------------------------------------------------------------
// The dialog, one test per mock state
// ---------------------------------------------------------------------------

describe("Add an account: step 1, the service", () => {
  it("lists every service with its words, Muse already tracked and off", () => {
    render(<Dialog />);
    expect(screen.getByRole("heading", { name: /Step 1 of 3\s*Add an account/ })).toBeInTheDocument();
    const list = screen.getByRole("radiogroup", { name: "Service" });
    expect(within(list).getByText("Claude Code 2.1 is installed. Can hold several logins.")).toBeInTheDocument();
    expect(within(list).getByText("Codex 0.162 is installed. Can hold several logins.")).toBeInTheDocument();
    expect(within(list).getByText("Grok Build 1.0 is installed. Khipu can use a login you made in Grok Build.")).toBeInTheDocument();
    expect(within(list).getByText("Muse has one login per Mac, and Khipu already tracks it.")).toBeInTheDocument();
    expect(within(list).getByText("Already tracked")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Muse/ })).toBeDisabled();
    // As in the mock: the first installed service is chosen, so Next is ready.
    expect(screen.getByRole("radio", { name: /Claude/ })).toBeChecked();
    expect(screen.getByRole("button", { name: "Next" })).toBeEnabled();
    fireEvent.click(screen.getByRole("radio", { name: /Codex/ }));
    expect(screen.getByRole("radio", { name: /Codex/ })).toBeChecked();
    expect(screen.getByRole("button", { name: "Next" })).toBeEnabled();
    expect(screen.getByText("Accounts you add are tracked on this Mac only.")).toBeInTheDocument();
  });

  it("service not installed: says so with the download page and Check again, Next off", () => {
    render(<Dialog disc={discovery({ clis: CLIS_CODEX_NOT })} start={{ cli: "codex", step: 1 }} />);
    expect(screen.getByText("Codex is not installed on this Mac")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the Codex download page" })).toHaveAttribute("href", expect.stringMatching(/^https:\/\//));
    expect(screen.getByRole("button", { name: "Check again" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("nothing installed: all four not installed, download pages and Choose the CLI…, Next off", () => {
    render(<Dialog disc={discovery({ clis: CLIS_NONE })} tracked={[]} />);
    expect(screen.getAllByText("Not installed")).toHaveLength(4);
    expect(screen.getByRole("link", { name: "Claude Code download page" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Codex download page" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Choose the CLI…" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("installed but can't run: where it was found and the in-app fixes", () => {
    render(<Dialog disc={discovery({ clis: CLIS_CODEX_CANT })} start={{ cli: "codex", step: 1 }} />);
    expect(screen.getByText("Found at /home/someone/.nvm/versions/node/v20.11.0/bin/codex, but Khipu can't start it.")).toBeInTheDocument();
    expect(screen.getByText("Can't run it")).toBeInTheDocument();
    expect(screen.getByText("Khipu found Codex but can't start it")).toBeInTheDocument();
    expect(screen.getByText(/Codex needs Node, and Khipu couldn't find it next to Codex/)).toBeInTheDocument();
    for (const b of ["Set the Node folder…", "Choose the CLI…", "Check again"]) expect(screen.getByRole("button", { name: b })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("Set the Node folder… and Choose the CLI… save their settings; Check again runs discovery again", async () => {
    fakeKhipu({ disc: discovery({ clis: CLIS_ALL }) });
    render(<Dialog disc={discovery({ clis: CLIS_CODEX_CANT })} start={{ cli: "codex", step: 1 }}
      onRediscover={async () => {
        const r = await discoverAccounts(false);
        return r.ok ? normalDiscover(r.data) : null;
      }} />);
    pickerMock.mockImplementationOnce(async () => "/home/someone/.nvm/versions/node/v20.11.0/bin");
    fireEvent.click(screen.getByRole("button", { name: "Set the Node folder…" }));
    await waitFor(() => expect(calls("khipu_config_set")).toHaveLength(1));
    expect(calls("khipu_config_set")[0][1]).toEqual({ key: "node_folder", value: "/home/someone/.nvm/versions/node/v20.11.0/bin" });
    expect(await screen.findByText(/Saved the Node folder/)).toBeInTheDocument();
    pickerMock.mockImplementationOnce(async () => "/opt/fake/bin/codex");
    fireEvent.click(screen.getByRole("button", { name: "Choose the CLI…" }));
    await waitFor(() => expect(calls("khipu_config_set")).toHaveLength(2));
    expect(calls("khipu_config_set")[1][1]).toEqual({ key: "cli_path.codex", value: "/opt/fake/bin/codex" });
    expect(calls("khipu_accounts_discover")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    await waitFor(() => expect(calls("khipu_accounts_discover")).toHaveLength(1));
    expect(await screen.findByText(/Codex starts now: version 0.162/)).toBeInTheDocument();
  });
});

describe("Add an account: step 2, which login", () => {
  it("lists what Khipu found, tracked ones off with their names, and Choose another folder…", () => {
    render(<Dialog start={{ cli: "claude", step: 2 }} />);
    expect(screen.getByRole("heading", { name: /Claude: which login\?/ })).toBeInTheDocument();
    const logins = screen.getByRole("radiogroup", { name: "Claude logins on this Mac" });
    expect(within(logins).getByText("Tracked as Claude main")).toBeInTheDocument();
    expect(within(logins).getByText("Tracked as Claude work")).toBeInTheDocument();
    expect(within(logins).getByText("Signed in · Pro · used by 4 conversations this week")).toBeInTheDocument();
    expect(within(logins).getByText("Not signed in · no conversation used it this week")).toBeInTheDocument();
    expect(screen.getByText(/It never opened what is inside them/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Choose another folder…" })).toBeInTheDocument();
    // The default login's path shows next to its name, as in the mock.
    expect(within(logins).getByText("~/.claude")).toBeInTheDocument();
    // As in the mock: the first signed-in login is chosen, so Next is ready.
    expect(screen.getByRole("radio", { name: /~\/\.claude-client/ })).toBeChecked();
    expect(screen.getByRole("button", { name: "Next" })).toBeEnabled();
  });

  it("Sign in to another account shows disabled with words where it is not offered", () => {
    render(<Dialog start={{ cli: "claude", step: 2 }} />);
    expect(screen.getByRole("radio", { name: /Sign in to another account/ })).toBeDisabled();
    expect(screen.getByText(COMING.signInNew)).toBeInTheDocument();
  });

  it("Sign in to another account is offered where the CLI says so", async () => {
    invokeMock.mockImplementation(async (cmd: string) =>
      cmd === "khipu_account_sign_in_offers"
        ? JSON.stringify({ claude: { offered: true }, codex: { offered: false } })
        : JSON.stringify({ ok: true }),
    );
    render(<Dialog start={{ cli: "claude", step: 2 }} canSignInNew={undefined} />);
    await waitFor(() => expect(screen.getByRole("radio", { name: /Sign in to another account/ })).toBeEnabled());
    expect(screen.queryByText(COMING.signInNew)).not.toBeInTheDocument();
    cleanup();
    render(<Dialog start={{ cli: "codex", step: 2 }} canSignInNew={undefined} />);
    await waitFor(() => expect(calls("khipu_account_sign_in_offers").length).toBeGreaterThan(1));
    expect(screen.getByRole("radio", { name: /Sign in to another account/ })).toBeDisabled();
  });

  it("a new sign-in that finishes goes on to step 3 with its folder and name, saving nothing", async () => {
    invokeMock.mockImplementation(async (cmd: string) => {
      switch (cmd) {
        case "khipu_account_new_folder":
          return JSON.stringify({ ok: true, folder: "~/.claude-lab", reused: false });
        case "khipu_account_sign_in_new":
          return JSON.stringify({ ok: true, state: "waiting",
            attempt: { id: "0123456789abcdef", folder: "~/.claude-lab", started: 1_800_000_000, deadline: 1_800_000_900 } });
        case "khipu_account_sign_in_status":
          return JSON.stringify({ ok: true, state: "signed_in", attempt: { id: "0123456789abcdef", folder: "~/.claude-lab" } });
        case "khipu_accounts_try":
          return JSON.stringify({ ok: true, default: false, row: liveRow({ account: "Claude lab" }) });
        default:
          return JSON.stringify({ ok: true });
      }
    });
    render(<Dialog start={{ cli: "claude", step: 2 }} canSignInNew />);
    fireEvent.click(screen.getByRole("radio", { name: /Sign in to another account/ }));
    const open = screen.getByRole("button", { name: "Open the sign-in page" });
    expect(open).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Name for this account"), { target: { value: "Claude lab" } });
    expect(open).toBeEnabled();
    fireEvent.click(open);
    await waitFor(() => expect(screen.getByRole("heading", { name: /Check and save/ })).toBeInTheDocument(), { timeout: 5000 });
    expect(calls("khipu_account_new_folder")[0][1]).toEqual({ harness: "claude", name: "Claude lab" });
    expect(screen.getByLabelText("Name")).toHaveValue("Claude lab");
    await waitFor(() => expect(calls("khipu_accounts_try").length).toBe(1));
    expect(calls("khipu_accounts_try")[0][1]).toMatchObject({ harness: "claude", folder: "~/.claude-lab" });
    expect(names()).not.toContain("khipu_accounts_track");
    expect(names()).not.toContain("khipu_config_set");
  });

  it("nothing found besides what is tracked", () => {
    render(<Dialog start={{ cli: "claude", step: 2 }} disc={discovery({ candidates: [] })} />);
    expect(screen.getByText("No other Claude logins found")).toBeInTheDocument();
    expect(screen.getByText(/already tracks it as Claude main\. To add a second account, sign in to it here\./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("a folder found only by name needs Check first, with its help line", () => {
    render(<Dialog start={{ cli: "codex", step: 2 }} />);
    expect(screen.getByRole("radio", { name: /~\/\.codex-work/ })).toBeDisabled();
    expect(screen.getByText("Check asks Codex whether this login is signed in. Codex may create a settings file in that folder when it is asked.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Check the Codex login at ~/.codex-work" })).toBeInTheDocument();
  });

  it("Grok Build: which login?", () => {
    render(<Dialog start={{ cli: "grok", step: 2 }} disc={discovery({ candidates: [GROK_SECOND] })}
      tracked={[...TRACKED, { label: "Grok Build", harness: "grok" }]} />);
    expect(screen.getByRole("heading", { name: /Grok Build: which login\?/ })).toBeInTheDocument();
    expect(screen.getByText("Signed in, plan not read yet · used by 2 conversations this week")).toBeInTheDocument();
    expect(screen.getByText(/shows you a code to approve in your browser/)).toBeInTheDocument();
  });
});

describe("Add an account: step 3, check and save", () => {
  it("Live: read just now, with the plan row", () => {
    render(<Dialog start={{ candidate: CLIENT, read: { ok: true, row: liveRow() } }} />);
    expect(screen.getByRole("heading", { name: /Step 3 of 3\s*Check and save/ })).toBeInTheDocument();
    expect(screen.getByLabelText("Name")).toHaveValue("Claude client");
    expect(screen.getByText("Live: read just now")).toBeInTheDocument();
    expect(screen.getByRole("meter")).toHaveAttribute("aria-valuetext", "18% used");
    expect(screen.getByText(/Reading a plan uses no turn and costs nothing/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save account" })).toBeEnabled();
  });

  it("signed in, but the read fails: the plain reason and Read again now", () => {
    render(<Dialog start={{ candidate: GROK_SECOND, read: { ok: false, row: { harness: "grok", ok: false, error: "timeout", windows: [] } } }} />);
    expect(screen.getByText("Signed in, but its plan can't be read")).toBeInTheDocument();
    expect(screen.getByText(/Grok Build didn't answer in time\. Khipu will try again on its next read/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Read again now" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save account" })).toBeEnabled();
  });

  it("not signed in: Save without signing in and Sign in now, said before it starts", () => {
    render(<Dialog start={{ candidate: OLD, read: { ok: false, row: { harness: "claude", ok: false, error: "not_signed_in", windows: [] } } }} />);
    expect(screen.getByText("Not signed in")).toBeInTheDocument();
    expect(screen.getByText(/Sign in now tracks Claude old first, then opens the sign-in page/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save without signing in" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in now" })).toBeInTheDocument();
  });

  it("duplicate login: Khipu already tracks it, Show the other account", () => {
    render(<Dialog start={{ candidate: OFFICE, name: "Claude team", read: { ok: true, row: liveRow() },
      problem: { code: "login_taken", field: "home", other: "Claude work", message: "Khipu already tracks this login as Claude work." } }} />);
    expect(screen.getByText("Khipu already tracks this login as Claude work.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show Claude work" })).toBeInTheDocument();
  });

  it("name already used: says so with a suggestion, Save off", () => {
    render(<Dialog start={{ candidate: { ...CODEX_LAB, state: "signed_in" }, name: "Codex", read: { ok: true, row: liveRow({ harness: "codex" }) } }} />);
    expect(screen.getByText("Another account is already named Codex. Choose a different name, such as Codex work.")).toBeInTheDocument();
    expect(screen.getByLabelText("Name")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("button", { name: "Save account" })).toBeDisabled();
  });

  it("the login folder is not there: says so, Save off", () => {
    render(<Dialog start={{ candidate: CLIENT, read: { ok: false, error: "folder_not_found" } }} />);
    expect(screen.getByText("Khipu can't find the login folder ~/.claude-client")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save account" })).toBeDisabled();
  });
});

describe("Sign in now on a login Khipu doesn't track yet", () => {
  it("saves through khipu_accounts_track first, then signs in; the app never writes the list itself", async () => {
    fakeKhipu({ status: status9B({ value: null }) });
    const out = await signInNow({ harness: "claude", home: "~/.claude-old", label: "Claude old" }, READING, TRACKED);
    expect(out.saved).toBe(true);
    const order = names().filter((n) => ["khipu_config_set", "khipu_accounts_track", "khipu_account_sign_in"].includes(n));
    expect(order).toEqual(["khipu_accounts_track", "khipu_account_sign_in"]);
    // The defaults as shown go along for the CLI to write out first.
    expect(calls("khipu_accounts_track")[0][1]).toEqual({
      entries: [{ harness: "claude", home: "~/.claude-old", label: "Claude old" }],
      expect: "v12",
      shownDefaults: TRACKED.map((t) => ({ harness: t.harness, home: t.home ?? null, label: t.label })),
    });
    expect(calls("khipu_account_sign_in")[0][1]).toEqual({ harness: "claude", home: "~/.claude-old" });
  });

  it("Add keeps an existing invalid entry: only the new account is sent, and no list is written", async () => {
    const invalid = { label: "Old custom", harness: "claude", home: "relative/login", invalid: "home_not_absolute" };
    fakeKhipu({ status: status9B({ accounts: [...status9B().accounts, invalid] }) });
    const out = await saveNewAccount({ harness: "claude", home: "~/.claude-new", label: "Claude new" }, READING, null);
    expect(out.ok).toBe(true);
    expect(calls("khipu_config_set")).toHaveLength(0);
    expect(calls("khipu_config_unset")).toHaveLength(0);
    expect(calls("khipu_accounts_track")[0][1]).toEqual({ entries: [{ harness: "claude", home: "~/.claude-new", label: "Claude new" }], expect: "v12" });
  });

  it("an unread account list saves nothing", async () => {
    fakeKhipu({ nineB: false });
    const out = await signInNow({ harness: "claude", home: "~/.claude-old", label: "Claude old" }, READING, TRACKED);
    expect(out.saved).toBe(false);
    expect(calls("khipu_accounts_track")).toHaveLength(0);
    expect(calls("khipu_config_set")).toHaveLength(0);
    expect(calls("khipu_account_sign_in")).toHaveLength(0);
  });

  it("with 9B here: khipu_accounts_track first, then the sign-in; a refused track signs nothing in", async () => {
    fakeKhipu();
    await signInNow({ harness: "claude", home: "~/.claude-old", label: "Claude old" }, HERE, null);
    expect(names().filter((n) => n === "khipu_accounts_track" || n === "khipu_account_sign_in")).toEqual(["khipu_accounts_track", "khipu_account_sign_in"]);
    expect(calls("khipu_accounts_track")[0][1]).toMatchObject({ entries: [{ harness: "claude", home: "~/.claude-old", label: "Claude old" }], expect: "v12" });
    invokeMock.mockReset();
    fakeKhipu({ trackRefusal: { ok: false, error: "taken", code: "name_taken" } });
    const out = await signInNow({ harness: "claude", home: "~/.claude-old", label: "Claude old" }, HERE, null);
    expect(out.saved).toBe(false);
    expect(calls("khipu_account_sign_in")).toHaveLength(0);
  });

  it("from the dialog: Sign in now saves, then signs in, in that order", async () => {
    fakeKhipu();
    render(<Dialog start={{ candidate: OLD, read: { ok: false, row: { harness: "claude", ok: false, error: "not_signed_in", windows: [] } } }} />);
    fireEvent.click(screen.getByRole("button", { name: "Sign in now" }));
    await waitFor(() => expect(calls("khipu_account_sign_in")).toHaveLength(1));
    const order = names().filter((n) => n === "khipu_config_set" || n === "khipu_accounts_track" || n === "khipu_account_sign_in");
    expect(order).toEqual(["khipu_accounts_track", "khipu_account_sign_in"]);
    expect(await screen.findByText(/Claude old is tracked\./)).toBeInTheDocument();
  });

  it("from the dialog: a refused save shows why and starts no sign-in", async () => {
    fakeKhipu({ trackRefusal: { ok: false, error: "taken", code: "name_taken", index: 4, field: "label", other: 0 } });
    render(<Dialog start={{ candidate: OLD, read: { ok: false, row: { harness: "claude", ok: false, error: "not_signed_in", windows: [] } } }} />);
    fireEvent.click(screen.getByRole("button", { name: "Sign in now" }));
    await waitFor(() => expect(calls("khipu_accounts_track")).toHaveLength(1));
    await waitFor(() => expect(screen.getByLabelText("Name")).toHaveAttribute("aria-invalid", "true"));
    expect(calls("khipu_account_sign_in")).toHaveLength(0);
  });
});

describe("the dialog keeps focus", () => {
  function Opener() {
    const [open, setOpen] = useState(false);
    return (
      <>
        <button type="button" onClick={() => setOpen(true)}>
          Add an account…
        </button>
        {open ? (
          <AddAccountDialog open onClose={() => setOpen(false)} discovery={discovery()} onRediscover={async () => null}
            onCheck={async () => ({ ok: false, error: "folder_not_found" })} tracked={TRACKED} shownDefaults={TRACKED} support={HERE}
            start={{ cli: "claude", step: 2 }} onSaved={() => {}} />
        ) : null}
      </>
    );
  }

  it("traps Tab inside, returns focus after the folder picker, and to the opener on close", async () => {
    render(<Opener />);
    const opener = screen.getByRole("button", { name: "Add an account…" });
    opener.focus();
    fireEvent.click(opener);
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
    const enabled = Array.from(dialog.querySelectorAll<HTMLElement>("button:not([disabled]), input:not([disabled]), a[href]"));
    const first = enabled[0];
    const last = enabled[enabled.length - 1];
    last.focus();
    fireEvent.keyDown(last, { key: "Tab" });
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(first, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);
    // The native folder picker returns (nothing picked): focus is back on the button.
    const choose = screen.getByRole("button", { name: "Choose another folder…" });
    choose.focus();
    fireEvent.click(choose);
    await waitFor(() => expect(pickerMock).toHaveBeenCalled());
    await waitFor(() => expect(document.activeElement).toBe(choose));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(opener));
  });

  describe("with a showModal that moves focus inside, as a browser's does", () => {
    let real: typeof HTMLDialogElement.prototype.showModal;
    beforeEach(() => {
      real = HTMLDialogElement.prototype.showModal;
      HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
        this.setAttribute("open", "");
        this.querySelector<HTMLElement>("button:not([disabled]), input:not([disabled])")?.focus();
      };
    });
    afterEach(() => {
      HTMLDialogElement.prototype.showModal = real;
    });

    async function openIt() {
      render(<Opener />);
      const opener = screen.getByRole("button", { name: "Add an account…" });
      opener.focus();
      fireEvent.click(opener);
      const dialog = await screen.findByRole("dialog");
      await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
      return { opener, dialog };
    }

    it("Escape gives focus back to the button that opened it", async () => {
      const { opener, dialog } = await openIt();
      fireEvent.keyDown(dialog.querySelector(".kit-dialog-body")!, { key: "Escape" });
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      await waitFor(() => expect(document.activeElement).toBe(opener));
    });

    it("Cancel gives focus back to the button that opened it", async () => {
      const { opener } = await openIt();
      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      await waitFor(() => expect(document.activeElement).toBe(opener));
    });
  });

  it("a picked folder puts focus on the login it found", async () => {
    pickerMock.mockImplementationOnce(async () => "/home/someone/.codex-team");
    render(
      <AddAccountDialog open onClose={() => {}} discovery={discovery()} onRediscover={async () => null}
        onCheck={async () => ({ ok: true, candidate: { ...CODEX_WORK, id: "codex:team", path: "~/.codex-team", state: "signed_in", plan: "plus" } })}
        tracked={TRACKED} shownDefaults={TRACKED} support={HERE} start={{ cli: "codex", step: 2 }} onSaved={() => {}} />,
    );
    const choose = screen.getByRole("button", { name: "Choose another folder…" });
    choose.focus();
    fireEvent.click(choose);
    const radio = await screen.findByRole("radio", { name: /~\/\.codex-team/ });
    await waitFor(() => expect(document.activeElement).toBe(radio));
    expect(radio).toBeChecked();
  });

  it("Next moves focus to the new step's heading", async () => {
    render(<Dialog />);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    const heading = await screen.findByRole("heading", { name: /Step 2 of 3/ });
    await waitFor(() => expect(document.activeElement).toBe(heading));
  });

  it("Track… from a Found row still has Back on step 3, to the logins", async () => {
    render(<Dialog start={{ candidate: CLIENT, read: { ok: false, error: "folder_not_found" } }} />);
    expect(screen.getByText("It may have been moved or removed. Go Back to choose another login, or another folder.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(await screen.findByRole("heading", { name: /Claude: which login\?/ })).toBeInTheDocument();
  });

  it("Escape closes it like Cancel", async () => {
    render(<Opener />);
    fireEvent.click(screen.getByRole("button", { name: "Add an account…" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.keyDown(dialog.querySelector(".kit-dialog-body")!, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
});

// ---------------------------------------------------------------------------
// Found rows, the group bar and the rest of Accounts
// ---------------------------------------------------------------------------

describe("Found rows", () => {
  it("Found, Found not checked with its help line, Couldn't check with Check again", () => {
    render(<FoundRows candidates={[CLIENT, CODEX_WORK, CODEX_LAB]} support={HERE} now={NOW} onTrack={() => {}} onCheck={() => {}} onDecline={() => {}} />);
    expect(screen.getByText("Found")).toBeInTheDocument();
    expect(screen.getByText("Signed in (Pro). 4 of your conversations this week ran on it. Found 20 min ago.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Track the Claude account at ~/.claude-client" })).toBeInTheDocument();
    expect(screen.getByText("Found, not checked")).toBeInTheDocument();
    expect(screen.getByText(/no conversation has used it, so Khipu has not asked Codex about it yet/)).toBeInTheDocument();
    const check = screen.getByRole("button", { name: "Check the Codex login at ~/.codex-work" });
    expect(check).toHaveAccessibleDescription("Check asks Codex whether this login is signed in. Codex may create a settings file in that folder when it is asked.");
    expect(screen.getByText("Couldn't check")).toBeInTheDocument();
    expect(screen.getByText("Couldn't check ~/.codex-lab: Codex didn't answer in time. Khipu tries again tomorrow, or now.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Check ~/.codex-lab again" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /^Not this one/ })).toHaveLength(2);
  });

  it("moved?: a tracked account's folder is gone and a new login is signed in", () => {
    const rows = [{ harness: "claude", account: "Claude work", home: "~/.claude-work", key: "claude|~/.claude-work", ok: false, error: "login_folder_missing", windows: [] }];
    const moved = movedPairs(rows, [OFFICE]);
    render(<FoundRows candidates={[OFFICE]} moved={moved} support={HERE} onTrack={() => {}} onCheck={() => {}} onDecline={() => {}} />);
    expect(screen.getByText(/Is this Claude work, moved\?/)).toBeInTheDocument();
    expect(screen.getByText("Claude work's folder ~/.claude-work is gone, and this new Claude login is signed in (Max 20x).")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Yes, ~/.claude-office is Claude work, moved" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "No, track ~/.claude-office as a new account" })).toBeEnabled();
  });

  it("a not-checked row runs no check until Check is pressed", async () => {
    fakeKhipu({ disc: discovery({ candidates: [CODEX_WORK] }) });
    function Host() {
      const finder = useAccountsFinder(true);
      return <FinderTop finder={finder} />;
    }
    render(<Host />);
    const check = await screen.findByRole("button", { name: "Check the Codex login at ~/.codex-work" });
    await waitFor(() => expect(calls("khipu_accounts_discover").length).toBeGreaterThan(0));
    expect(calls("khipu_accounts_check")).toHaveLength(0);
    fireEvent.click(check);
    await waitFor(() => expect(calls("khipu_accounts_check")).toHaveLength(1));
    expect(calls("khipu_accounts_check")[0][1]).toEqual({ folder: "~/.codex-work" });
  });

  it("Not this one waits only for the account list to be read, with no later-update words", () => {
    render(<FoundRows candidates={[CLIENT]} support={READING} onTrack={() => {}} onCheck={() => {}} onDecline={() => {}} />);
    const no = screen.getByRole("button", { name: "Not this one: never offer ~/.claude-client again" });
    expect(no).toBeDisabled();
    expect(screen.queryByText(/later update/)).toBeNull();
    cleanup();
    render(<FoundRows candidates={[CLIENT]} support={HERE} onTrack={() => {}} onCheck={() => {}} onDecline={() => {}} />);
    expect(screen.getByRole("button", { name: "Not this one: never offer ~/.claude-client again" })).toBeEnabled();
  });

  it("a one-shot Check keeps where each program is, so going back never reads forever", async () => {
    const checked = { ...CODEX_WORK, state: "signed_in" as const, needs_owner_check: false };
    const reply = discovery({ candidates: [checked], check: { ok: true, candidate: checked } });
    delete reply.clis;
    invokeMock.mockImplementation(async (cmd: string) => JSON.stringify(cmd === "khipu_accounts_discover" ? discovery() : reply));
    const { result } = renderHook(() => useDiscovery(true));
    await waitFor(() => expect(result.current.data?.clis?.codex?.state).toBe("installed"));
    await act(async () => {
      await result.current.check(CODEX_WORK.path!);
    });
    expect(result.current.data?.candidates).toEqual([checked]);
    expect(result.current.data?.clis?.codex?.state).toBe("installed");
  });
});

describe("the Accounts group bar", () => {
  it("the last run's line, with plurals", () => {
    expect(lastRunLine({ at: "2026-10-15T19:40:00Z", couldnt_check: 1 }, NOW)).toMatch(/^Last looked .+; 1 folder could not be checked$/);
    expect(lastRunLine({ at: "2026-10-15T19:40:00Z", couldnt_check: 2, left_for_later: 1 }, NOW)).toMatch(
      /; 2 folders could not be checked; 1 folder left for the next look$/,
    );
    expect(lastRunLine({ at: "2026-10-15T19:40:00Z" }, NOW)).toMatch(/^Last looked [^;]+$/);
    render(<AccountsGroupBar looking={false} lastRun={{ at: "2026-10-15T19:40:00Z", couldnt_check: 1 }} onLookNow={() => {}} onAdd={() => {}}
      onForget={async () => "done"} now={NOW} />);
    expect(screen.getByText(/1 folder could not be checked\./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Forget which logins conversations used" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Look for accounts now" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Add an account…" })).toBeInTheDocument();
  });

  it("Looking…, never 0 found, with Look for accounts now off", () => {
    render(<AccountsGroupBar looking lastRun={null} onLookNow={() => {}} onAdd={() => {}} />);
    expect(screen.getByRole("status")).toHaveTextContent("Looking for accounts…");
    expect(screen.getByRole("button", { name: "Look for accounts now" })).toBeDisabled();
    expect(screen.queryByText(/0 found/)).toBeNull();
  });

  it("Forget asks first, then calls the command", async () => {
    fakeKhipu();
    function Host() {
      const finder = useAccountsFinder(true);
      return <FinderTop finder={finder} />;
    }
    render(<Host />);
    fireEvent.click(await screen.findByRole("button", { name: "Forget which logins conversations used" }));
    expect(calls("khipu_accounts_forget_login_paths")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Forget them" }));
    await waitFor(() => expect(calls("khipu_accounts_forget_login_paths")).toHaveLength(1));
  });
});

describe("Not offered again, the switch, programs and no accounts", () => {
  it("Not offered again lists each login with Offer again (9B), with plurals", () => {
    const declined = [
      { cli: "claude" as const, path: "~/.claude-old", why: "you chose Not this one", at: "2026-10-09T15:00:00Z" },
      { cli: "grok" as const, path: "~/.grok-test", why: "you stopped tracking it", at: "2026-10-02T15:00:00Z" },
    ];
    render(<NotOfferedAgain support={{ state: "here", status: status9B({ declined }) }} onOfferAgain={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /Not offered again/ }));
    expect(screen.getByText(/2 logins you said no to/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Offer ~/.claude-old again" })).toBeInTheDocument();
    expect(screen.getByText(/Grok Build · you stopped tracking it on Oct 2/)).toBeInTheDocument();
    cleanup();
    render(<NotOfferedAgain support={{ state: "here", status: status9B({ declined: [declined[0]] }) }} onOfferAgain={() => {}} />);
    expect(screen.getByText(/1 login you said no to/)).toBeInTheDocument();
  });

  it("Not offered again and the switch wait only for the account list to be read", () => {
    render(
      <>
        <NotOfferedAgain support={READING} onOfferAgain={() => {}} />
        <AutoTrackSwitch support={READING} onChange={() => {}} />
      </>,
    );
    fireEvent.click(screen.getByRole("button", { name: /Not offered again/ }));
    expect(screen.getByText("Reading…")).toBeInTheDocument();
    const sw = screen.getByRole("switch", { name: "Track new accounts automatically" });
    expect(sw).toBeDisabled();
    expect(sw).not.toBeChecked();
    expect(screen.queryByText(/later update/)).toBeNull();
  });

  it("the switch with 9B: its value and Off in words", () => {
    const on = vi.fn();
    render(<AutoTrackSwitch support={HERE} onChange={on} />);
    const sw = screen.getByRole("switch", { name: "Track new accounts automatically" });
    expect(sw).toBeEnabled();
    expect(screen.getByText("Off")).toBeInTheDocument();
    fireEvent.click(sw);
    expect(on).toHaveBeenCalledWith(true);
  });

  it("where Khipu finds each program, with Change…, Clear and Check again", () => {
    const set = vi.fn();
    render(<CliSettingsRows clis={{ ...CLIS_ALL, grok: { ...CLIS_ALL!.grok!, state: "cant_run", reason: "start_failed" } }}
      settings={{ "cli_path.codex": { value: null, usable: false }, node_folder: { value: "/home/someone/.nvm/versions/node/v20.11.0/bin", usable: true } }}
      onSet={set} onCheckAgain={() => {}} />);
    expect(screen.getByText("Node folder: /home/someone/.nvm/versions/node/v20.11.0/bin (set by you)")).toBeInTheDocument();
    expect(screen.getByText("Install looks incomplete: its program file is missing. Reinstall it from its maker.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear the Node folder" }));
    expect(set).toHaveBeenCalledWith("node_folder", null, expect.anything());
    expect(screen.getByRole("button", { name: "Check Grok Build again" })).toBeInTheDocument();
  });

  it("Read no accounts: No accounts tracked with Add an account…", () => {
    const add = vi.fn();
    render(<NoAccountsTracked onAdd={add} />);
    expect(screen.getByText("No accounts tracked")).toBeInTheDocument();
    expect(screen.getByText("You stopped tracking every account on this Mac. Add one to see how much of its plan is left.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add an account…" }));
    expect(add).toHaveBeenCalled();
  });
});

describe("one tracked account", () => {
  it("Rename… and Stop tracking… wait only for the account list to be read", () => {
    render(<AccountExtras label="Claude work" harness="claude" home="~/.claude-work" support={READING} onRename={() => {}} onStop={() => {}} />);
    expect(screen.getByRole("button", { name: "Rename Claude work" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Stop tracking Claude work" })).toBeDisabled();
    expect(screen.queryByText(/later update/)).toBeNull();
    cleanup();
    render(<AccountExtras label="Claude work" harness="claude" home="~/.claude-work" support={HERE} onRename={() => {}} onStop={() => {}} />);
    expect(screen.getByRole("button", { name: "Rename Claude work" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Stop tracking Claude work" })).toBeEnabled();
  });

  it("login folder not found: says so, never Not signed in, with Choose the folder… and Stop tracking…", () => {
    render(<AccountExtras label="Claude work" harness="claude" home="~/.claude-work" support={HERE} onRename={() => {}} onStop={() => {}}
      row={{ harness: "claude", ok: false, error: "login_folder_missing", windows: [] }} />);
    expect(screen.getByText("Khipu can't find the login folder ~/.claude-work")).toBeInTheDocument();
    expect(screen.queryByText(/Not signed in/)).toBeNull();
    expect(screen.getByRole("button", { name: "Choose the folder for Claude work" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Sign in to Claude work again in a new ~/.claude-work" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Stop tracking Claude work" })).toBeEnabled();
    expect(screen.getByText("Nothing read from it yet")).toBeInTheDocument();
  });

  it("login folder not found: when it was last read, and nothing since", () => {
    const at = "2026-10-14T22:10:00Z";
    render(<AccountExtras label="Claude work" harness="claude" home="~/.claude-work" support={HERE} onRename={() => {}} onStop={() => {}} now={NOW}
      row={{ harness: "claude", ok: false, error: "login_folder_missing", last_good_at: at, windows: [] }} />);
    const time = new Date(at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    expect(screen.getByText(new RegExp(`^Last read (today|yesterday|\\d+ days ago) at ${time} · nothing read since$`))).toBeInTheDocument();
  });

  it("signed out: figures kept, with how old they are", () => {
    render(<AccountExtras label="Claude second" harness="claude" home="~/.claude-second" support={HERE} onRename={() => {}} onStop={() => {}} now={NOW}
      row={{ harness: "claude", ok: false, error: "not_signed_in", last_good_at: "2026-10-13T15:00:00Z", windows: [] }} />);
    expect(screen.getByText("Signed out; figures from 2 days ago")).toBeInTheDocument();
  });

  const refs: AccountRefs = {
    rules: [{ n: 4, text: "Use Claude client only for client repos" }],
    note: true,
    holds: [{ id: "h1a2b3c4", text: "client work only", until: "2026-10-19T13:00:00Z" }],
    lanes: ["review-1", "docs"],
    priority: { place: 2, next: "Codex" },
    pausedUntil: "2026-10-16T13:00:00Z",
    last: false,
  };
  const acct = { key: "claude|~/.claude-client", label: "Claude client", harness: "claude", home: "~/.claude-client" };

  it("Rename: what is and isn't updated, Edit links, and the rename call", async () => {
    fakeKhipu({ nineB: true });
    const renamed = vi.fn();
    render(<RenameDialog open account={acct} refs={refs} taken={["Claude client", "Claude main", "Codex"]} support={HERE}
      onClose={() => {}} onRenamed={renamed} />);
    expect(screen.getByRole("heading", { name: /Rename Claude client/ })).toBeInTheDocument();
    expect(screen.getByText(/Plans, Accounts and routing, your priority list, 1 hold and 2 lanes/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit rule 4" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit your note" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Codex" } });
    expect(screen.getByRole("button", { name: "Rename" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Claude work 2" } });
    fireEvent.click(screen.getByRole("button", { name: "Rename" }));
    await waitFor(() => expect(calls("khipu_accounts_rename")).toHaveLength(1));
    expect(calls("khipu_accounts_rename")[0][1]).toEqual({ account: "claude|~/.claude-client", name: "Claude work 2", expect: "v12" });
  });

  it("Stop tracking: the defaults, what is not changed, and the stop call", async () => {
    fakeKhipu({ nineB: true });
    render(<StopTrackingDialog open account={acct} refs={refs} support={HERE} onClose={() => {}} onStopped={() => {}} />);
    expect(screen.getByRole("checkbox", { name: /Remove it from your priority list \(it is second; Codex moves up\)/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /End its pause now/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Remove it from 2 lanes: review-1, docs" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /End the hold “client work only”/ })).not.toBeChecked();
    expect(screen.getByText(/and its sign-in\. Conversations that run on it keep working/)).toBeInTheDocument();
    expect(screen.getByText(/as a found account, until you choose Offer again/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: /End the hold/ }));
    fireEvent.click(screen.getByRole("button", { name: "Stop tracking" }));
    await waitFor(() => expect(calls("khipu_accounts_stop")).toHaveLength(1));
    expect(calls("khipu_accounts_stop")[0][1]).toEqual({
      account: "claude|~/.claude-client", expect: "v12", keepPriority: false, keepPause: false, keepLanes: false,
      endHolds: ["h1a2b3c4"], confirmLast: false,
    });
  });

  it("Stop tracking the last account asks first and says Khipu won't go back to every program", () => {
    render(<StopTrackingDialog open account={{ key: "claude|", label: "Claude main", harness: "claude", home: null }}
      refs={{ ...refs, rules: [], note: false, holds: [], lanes: [], priority: null, pausedUntil: undefined, last: true }} support={HERE}
      onClose={() => {}} onStopped={() => {}} />);
    expect(screen.getByRole("heading", { name: /Stop tracking your last account\?/ })).toBeInTheDocument();
    expect(screen.getByText(/Claude main is the only account Khipu tracks on this Mac/)).toBeInTheDocument();
    expect(screen.getByText("Khipu won't go back to reading every installed program on its own.")).toBeInTheDocument();
  });

  it("Stop tracking the last account still lists its holds, kept unless ticked, and the other choices", async () => {
    fakeKhipu();
    render(<StopTrackingDialog open account={acct} refs={{ ...refs, last: true }} support={HERE} onClose={() => {}} onStopped={() => {}} />);
    expect(screen.getByRole("heading", { name: /Stop tracking your last account\?/ })).toBeInTheDocument();
    const hold = screen.getByRole("checkbox", { name: /End the hold “client work only”/ });
    expect(hold).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Remove it from your priority list/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /End its pause now/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Remove it from 2 lanes: review-1, docs" })).toBeChecked();
    fireEvent.click(screen.getByRole("checkbox", { name: /Remove it from 2 lanes/ }));
    fireEvent.click(screen.getByRole("button", { name: "Stop tracking" }));
    await waitFor(() => expect(calls("khipu_accounts_stop")).toHaveLength(1));
    expect(calls("khipu_accounts_stop")[0][1]).toEqual({
      account: "claude|~/.claude-client", expect: "v12", keepPriority: false, keepPause: false, keepLanes: true,
      endHolds: [], confirmLast: true,
    });
  });

  it("Edit rule leaves the Rename dialog, so focus can reach the editor behind it", async () => {
    const onEdit = vi.fn();
    function Host() {
      const [open, setOpen] = useState(true);
      return (
        <>
          <button type="button">Rule editor</button>
          {open ? (
            <RenameDialog open account={acct} refs={refs} taken={[]} support={HERE} onClose={() => setOpen(false)} onRenamed={() => {}}
              onEdit={() => {
                onEdit();
                screen.getByRole("button", { name: "Rule editor", hidden: true }).focus();
              }} />
          ) : null}
        </>
      );
    }
    render(<Host />);
    fireEvent.click(screen.getByRole("button", { name: "Edit rule 4" }));
    expect(onEdit).toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Rule editor" }));
  });
});

// ---------------------------------------------------------------------------
// Home
// ---------------------------------------------------------------------------

describe("Home's cards", () => {
  it("the Found card: one find, with Close, Track… and Not this one", () => {
    render(<HomeFoundCard offers={[CLIENT]} support={HERE} onTrack={() => {}} onDecline={() => {}} onClose={() => {}} />);
    const card = screen.getByRole("region", { name: "Found an account" });
    expect(card).toHaveTextContent("Found a Claude account you use: ~/.claude-client");
    expect(card).toHaveTextContent("4 of your conversations this week ran on it, and it is signed in (Pro).");
    expect(within(card).getByRole("button", { name: /^Close this card/ })).toBeInTheDocument();
  });

  it("the Found card lists every find, with plurals", () => {
    render(<HomeFoundCard offers={[CLIENT, GROK_SECOND]} support={HERE} onTrack={() => {}} onDecline={() => {}} onClose={() => {}} />);
    expect(screen.getByText("Found 2 accounts your conversations use")).toBeInTheDocument();
    expect(screen.getByText(/4 conversations this week/)).toBeInTheDocument();
    expect(screen.getByText(/2 conversations this week/)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /^Track the/ })).toHaveLength(2);
  });

  it("the automatic-tracking card: Undo, Rename… and Close", () => {
    render(<HomeAutoCard card={{ key: "claude|~/.claude-client", label: "Claude client", path: "~/.claude-client", conversations: 4 }}
      onUndo={() => {}} onRename={() => {}} onClose={() => {}} />);
    expect(screen.getByText("Khipu started tracking Claude client")).toBeInTheDocument();
    expect(screen.getByText(/You turned on Track new accounts automatically\./)).toBeInTheDocument();
    for (const n of ["Undo: stop tracking Claude client", "Rename Claude client", "Close this notice"]) {
      expect(screen.getByRole("button", { name: n })).toBeInTheDocument();
    }
  });

  it("Close never turns anything down, and the card stays closed for the same folder", async () => {
    fakeKhipu({ disc: discovery({ candidates: [CLIENT] }) });
    render(<HomeAccountCards active />);
    const card = await screen.findByRole("region", { name: "Found an account" });
    fireEvent.click(within(card).getByRole("button", { name: /^Close this card/ }));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Found an account" })).toBeNull());
    expect(calls("khipu_accounts_decline")).toHaveLength(0);
    cleanup();
    render(<HomeAccountCards active />);
    await waitFor(() => expect(calls("khipu_accounts_discover").length).toBeGreaterThan(1));
    expect(screen.queryByRole("region", { name: "Found an account" })).toBeNull();
    expect(calls("khipu_accounts_decline")).toHaveLength(0);
  });

  it("a different folder brings the card back after Close", async () => {
    window.localStorage.setItem("khipu.accounts.foundClosed", JSON.stringify([CLIENT.id]));
    fakeKhipu({ disc: discovery({ candidates: [CLIENT, GROK_SECOND] }) });
    render(<HomeAccountCards active />);
    expect(await screen.findByRole("region", { name: "Found accounts" })).toBeInTheDocument();
  });

  it("a login left unticked in setup reaches Home once a later conversation uses it", () => {
    addQuietLogins([CLIENT]);
    const d = (n: number) => discovery({ candidates: [{ ...CLIENT, conversations_7d: n, offer_on_home: true }] });
    expect(homeOffersOf(d(4), quietLogins())).toEqual([]);
    const later = { ...CLIENT, conversations_7d: 5, offer_on_home: true };
    expect(homeOffersOf(discovery({ candidates: [later] }), quietLogins())).toEqual([later]);
  });

  it("the week's count falling then rising still brings a quiet login to Home", async () => {
    addQuietLogins([CLIENT]);
    fakeKhipu({ disc: discovery({ candidates: [{ ...CLIENT, conversations_7d: 1 }] }) });
    render(<HomeAccountCards active />);
    await waitFor(() => expect(calls("khipu_accounts_discover").length).toBeGreaterThan(0));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
    expect(screen.queryByRole("region", { name: "Found an account" })).toBeNull();
    expect(quietLogins()).toEqual([{ id: CLIENT.id, seen: 1 }]);
    expect(homeOffersOf(discovery({ candidates: [{ ...CLIENT, conversations_7d: 2 }] }), quietLogins())).toHaveLength(1);
  });

  it("automatic tracking off saves nothing", async () => {
    fakeKhipu({ disc: discovery({ candidates: [CLIENT, OFFICE] }), status: status9B({ auto_track: false }) });
    render(<HomeAccountCards active />);
    await screen.findByRole("region", { name: "Found accounts" });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
    for (const write of ["khipu_accounts_track", "khipu_config_set", "khipu_config_unset", "khipu_accounts_auto_track"]) {
      expect(calls(write)).toHaveLength(0);
    }
  });
});

// ---------------------------------------------------------------------------
// Setup's step
// ---------------------------------------------------------------------------

describe("setup's Accounts step with what discovery found", () => {
  function fakeSetup(disc: DiscoverResult) {
    let accounts = [{ label: "Claude Code", harness: "claude" }];
    let source = "default";
    let version = 1;
    invokeMock.mockImplementation(async (cmd: string, args?: Record<string, unknown>) => {
      switch (cmd) {
        case "khipu_usage_status":
        case "khipu_usage_refresh":
          return JSON.stringify({ accounts: [{ harness: "claude", account: "Claude Code", home: null, ok: true, plan: "max", windows: [] }] });
        case "khipu_config_show":
          return JSON.stringify({ usage_accounts: { source, accounts, user_home: "/home/someone", version: `a${version}` } });
        case "khipu_config_set":
          version += 1;
          accounts = JSON.parse(String(args?.value));
          source = "file";
          return JSON.stringify({ ok: true });
        case "khipu_features_show":
          return JSON.stringify({ features: { routing_push: { enabled: true, source: "default" } } });
        case "khipu_route_view":
          return JSON.stringify({ accounts: [], priority: null });
        case "khipu_accounts_discover":
          return JSON.stringify(disc);
        case "khipu_accounts_status":
          throw new Error("khipu accounts status failed");
        default:
          return JSON.stringify({ ok: true });
      }
    });
  }

  it("lists found logins: signed in ticked, not signed in off; Add another account… is there", async () => {
    fakeSetup(discovery({ candidates: [CLIENT, OLD] }));
    render(<SetupAccountsStep active onBack={() => {}} onNext={() => {}} />);
    const row = (await screen.findByText("Claude Code, second login")).closest("li")!;
    expect(row).toHaveTextContent("~/.claude-client: signed in, Pro. 4 conversations this week ran on it.");
    expect(within(row).getByRole("checkbox")).toBeChecked();
    const old = screen.getByText("Claude Code, another folder").closest("li")!;
    expect(old).toHaveTextContent("~/.claude-old: not signed in. Leave it off, or sign in to it after setup.");
    expect(within(old).getByRole("checkbox")).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Add another account…" })).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Track new accounts automatically" })).toBeDisabled();
  });

  it("Next tracks a ticked login; an unticked one reaches no Home card", async () => {
    fakeSetup(discovery({ candidates: [CLIENT, GROK_SECOND] }));
    const next = vi.fn();
    render(<SetupAccountsStep active onBack={() => {}} onNext={next} />);
    const grok = (await screen.findByText("Grok Build, second login")).closest("li")!;
    fireEvent.click(within(grok).getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: /Next/ }));
    await waitFor(() => expect(next).toHaveBeenCalled());
    // The one save path: the ticked login goes to Track, never into a list
    // the app writes itself.
    expect(calls("khipu_config_set")).toHaveLength(0);
    const sent = calls("khipu_accounts_track")[0][1] as { entries: Array<{ home?: string | null; label?: string }>; shownDefaults?: unknown; expect?: string };
    // The version of the list setup read, so a list changed meanwhile is never saved over.
    expect(sent.expect).toBe("a1");
    expect(sent.entries).toEqual([{ harness: "claude", home: "~/.claude-client", label: "Claude client" }]);
    expect(sent.shownDefaults).toEqual([{ harness: "claude", home: null, label: "Claude Code" }]);
    cleanup();
    fakeKhipu({ disc: discovery({ candidates: [GROK_SECOND] }) });
    render(<HomeAccountCards active />);
    await waitFor(() => expect(calls("khipu_accounts_discover").length).toBeGreaterThan(0));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
    expect(screen.queryByRole("region", { name: /Found an? accounts?/ })).toBeNull();
  });

  it("Skip for now writes nothing and quiets nothing", async () => {
    fakeSetup(discovery({ candidates: [CLIENT] }));
    const next = vi.fn();
    render(<SetupAccountsStep active onBack={() => {}} onNext={next} />);
    await screen.findByText("Claude Code, second login");
    fireEvent.click(screen.getByRole("button", { name: "Skip for now" }));
    expect(next).toHaveBeenCalled();
    expect(calls("khipu_config_set")).toHaveLength(0);
    expect(window.localStorage.getItem("khipu.accounts.quiet")).toBeNull();
  });

  it("nothing found: Check again and Add another account…", async () => {
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "khipu_usage_status" || cmd === "khipu_usage_refresh") return JSON.stringify({ accounts: [] });
      if (cmd === "khipu_config_show") return JSON.stringify({ usage_accounts: { source: "default", accounts: [], user_home: "/home/someone" } });
      if (cmd === "khipu_accounts_discover") return JSON.stringify(discovery({ candidates: [], clis: CLIS_NONE }));
      if (cmd === "khipu_accounts_status") throw new Error("khipu accounts status failed");
      return JSON.stringify({ ok: true });
    });
    render(<SetupAccountsStep active onBack={() => {}} onNext={() => {}} />);
    expect(await screen.findByText("No accounts found on this Mac")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Check again" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add another account…" })).toBeInTheDocument();
  });

  it("a default login that is not signed in: Sign in now and Check again", async () => {
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === "khipu_usage_status" || cmd === "khipu_usage_refresh") {
        return JSON.stringify({ accounts: [
          { harness: "claude", account: "Claude Code", home: null, ok: false, error: "not_signed_in", windows: [] },
          { harness: "grok", account: "Grok Build", home: null, ok: false, error: "not_signed_in", windows: [] },
        ] });
      }
      if (cmd === "khipu_config_show") {
        return JSON.stringify({ usage_accounts: { source: "default", accounts: [{ label: "Claude Code", harness: "claude" }, { label: "Grok Build", harness: "grok" }], user_home: "/home/someone" } });
      }
      if (cmd === "khipu_accounts_discover") return JSON.stringify(discovery({ candidates: [] }));
      if (cmd === "khipu_accounts_status") throw new Error("khipu accounts status failed");
      if (cmd === "khipu_account_sign_in") return JSON.stringify({ ok: true });
      return JSON.stringify({ ok: true });
    });
    render(<SetupAccountsStep active onBack={() => {}} onNext={() => {}} />);
    const signIn = await screen.findByRole("button", { name: "Sign in to Claude Code now" });
    expect(screen.getByRole("button", { name: "Sign in with a code" })).toBeDisabled();
    expect(screen.getByText(COMING.codeSignIn)).toBeInTheDocument();
    fireEvent.click(signIn);
    await waitFor(() => expect(calls("khipu_account_sign_in")).toHaveLength(1));
    expect(calls("khipu_account_sign_in")[0][1]).toEqual({ harness: "claude", home: null });
  });
});

describe("What's new", () => {
  it("adds one sentence to the Accounts and routing item, no new item", () => {
    const release = NOTICES.find((n) => n.version === "0.4.16")!;
    const items = release.items.filter((i) => i.title === "Accounts and routing");
    expect(items).toHaveLength(1);
    expect(items[0].text).toBe("Khipu tells your conversations which accounts have room. It also finds your other logins and lets you add one.");
    expect(release.items.filter((i) => /logins/.test(i.title))).toHaveLength(0);
  });
});

describe("every account save carries the version it read", () => {
  const notSignedIn = { candidate: OLD, read: { ok: false, row: { harness: "claude", ok: false, error: "not_signed_in", windows: [] } } } as AddAccountStart;
  const acct = { key: "claude|~/.claude-client", label: "Claude client", harness: "claude", home: "~/.claude-client" };
  const noRefs: AccountRefs = { rules: [], note: false, holds: [], lanes: [], priority: null, pausedUntil: undefined, last: false };

  it("the dialog's Save sends the version of the list it read, never one fetched at save time", async () => {
    fakeKhipu({ status: status9B({ version: "v99" }) });
    render(<Dialog start={notSignedIn} support={{ state: "here", status: status9B({ version: "v12" }) }} />);
    fireEvent.click(screen.getByRole("button", { name: "Save without signing in" }));
    await waitFor(() => expect(calls("khipu_accounts_track")).toHaveLength(1));
    expect(calls("khipu_accounts_track")[0][1]).toMatchObject({ expect: "v12" });
    expect(calls("khipu_accounts_status")).toHaveLength(0);
  });

  it("a list read with no version sends nothing and says so, with Reload", async () => {
    fakeKhipu();
    const out = await saveNewAccount({ harness: "claude", home: "~/.claude-old", label: "Claude old" },
      { state: "here", status: status9B({ version: "" }) }, null);
    expect(out.ok).toBe(false);
    if (!out.ok) {
      expect(out.problem.message).toBe(LIST_MOVED);
      expect(out.problem.reload).toBe(true);
    }
    expect(calls("khipu_accounts_track")).toHaveLength(0);
  });

  it("version moved: the dialog says it in plain words, and Reload reads the list again", async () => {
    fakeKhipu({ trackRefusal: { ok: false, error: "the account list changed after it was read; read it again", code: "version_moved" } });
    const reload = vi.fn(async () => undefined);
    render(
      <AddAccountDialog open onClose={() => {}} discovery={discovery()} onRediscover={async () => null}
        onCheck={async () => ({ ok: false, error: "folder_not_found" })} tracked={TRACKED} shownDefaults={TRACKED}
        support={HERE} start={notSignedIn} onSaved={() => {}} onReload={reload} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Save without signing in" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(LIST_MOVED);
    expect(alert).not.toHaveTextContent(/read it again$/);
    fireEvent.click(within(alert).getByRole("button", { name: "Reload" }));
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByText(LIST_MOVED)).not.toBeInTheDocument());
  });

  it("version moved on Rename and Stop tracking: plain words and Reload, nothing else claimed", async () => {
    fakeKhipu();
    invokeMock.mockImplementation(async () =>
      JSON.stringify({ ok: false, error: "the account list changed after it was read; read it again and redo the change", code: "version_moved" }));
    const reload = vi.fn(async () => undefined);
    const renamed = vi.fn();
    render(<RenameDialog open account={acct} refs={noRefs} taken={["Claude client"]} support={HERE}
      onClose={() => {}} onRenamed={renamed} onReload={reload} />);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Claude work 2" } });
    fireEvent.click(screen.getByRole("button", { name: "Rename" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(LIST_MOVED);
    expect(calls("khipu_accounts_rename")[0][1]).toMatchObject({ expect: "v12" });
    expect(renamed).not.toHaveBeenCalled();
    fireEvent.click(within(alert).getByRole("button", { name: "Reload" }));
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
    cleanup();

    const stopped = vi.fn();
    render(<StopTrackingDialog open account={acct} refs={noRefs} support={HERE} onClose={() => {}} onStopped={stopped} onReload={reload} />);
    fireEvent.click(screen.getByRole("button", { name: /Stop tracking/ }));
    const stopAlert = await screen.findByRole("alert");
    expect(stopAlert).toHaveTextContent(LIST_MOVED);
    expect(within(stopAlert).getByRole("button", { name: "Reload" })).toBeInTheDocument();
    expect(stopped).not.toHaveBeenCalled();
  });

  it("the app's own command refusing a missing version counts as moved too", () => {
    expect(versionRefused({ ok: false, error: "the account list save needs the version it read; reload and try again" })).toBe(true);
    expect(versionRefused({ ok: false, error: "x", refusal: { code: "version_needed" } })).toBe(true);
    expect(versionRefused({ ok: false, error: "taken", refusal: { code: "name_taken" } })).toBe(false);
  });
});
