// Sign in to another account (settings clarity, phase 9D): the dialog's
// sign-in step, driven by an injected fake `call` so no CLI runs.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { SignInNewAccount } from "../SignInNewAccount";
import type { SignInCli } from "../SignInNewAccount";
import type { CliResult } from "../settingsCli";

// The lifecycle tests at the end run through the real callCli, with invoke
// answering the JSON the Python CLI prints; the rest inject a fake `call`.
const invoke = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({ invoke: (...a: unknown[]) => invoke(...a) }));

const NOW_MS = Date.parse("2026-10-15T15:25:00Z");
const STARTED = Math.floor(Date.parse("2026-10-15T15:22:00Z") / 1000);
const ATTEMPT = { id: "0123456789abcdef", cli: "claude", cli_name: "Claude", folder: "~/.claude-client", kind: "browser", started: STARTED, deadline: STARTED + 900 };

type Reply = CliResult<any>;
type Handler = (args?: Record<string, unknown>) => Reply | Promise<Reply>;

function fake(overrides: Record<string, Handler> = {}) {
  const handlers: Record<string, Handler> = {
    khipu_account_new_folder: () => ({ ok: true, data: { ok: true, folder: "~/.claude-client", reused: false } }),
    khipu_account_sign_in_new: () => ({ ok: true, data: { ok: true, attempt: ATTEMPT, state: "waiting" } }),
    khipu_account_sign_in_status: () => ({ ok: true, data: { ok: true, attempt: ATTEMPT, state: "waiting" } }),
    khipu_account_sign_in_cancel: () => ({ ok: true, data: { ok: true, state: "cancelled", stopped: true } }),
    ...overrides,
  };
  const call = vi.fn(async (command: string, args?: Record<string, unknown>) => handlers[command](args));
  return { call, handlers };
}

function setup(call: ReturnType<typeof fake>["call"], extra: Partial<{ cli: SignInCli; cliName: string; offered: boolean }> = {}) {
  const onSignedIn = vi.fn();
  const onBack = vi.fn();
  const onCancel = vi.fn();
  render(
    <SignInNewAccount
      cli="claude"
      cliName="Claude"
      accountName="Claude client"
      offered
      call={call as any}
      onSignedIn={onSignedIn}
      onBack={onBack}
      onCancel={onCancel}
      now={() => NOW_MS}
      {...extra}
    />,
  );
  return { onSignedIn, onBack, onCancel };
}

async function start() {
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Open the sign-in page" }));
  });
}

async function poll(ms = 2000) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

const commands = (call: ReturnType<typeof fake>["call"]) => call.mock.calls.map((c) => c[0]);

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("SignInNewAccount", () => {
  it("renders nothing when sign-in to a new account is not offered", () => {
    const { call } = fake();
    const { container } = render(
      <SignInNewAccount cli="codex" cliName="Codex" accountName="x" offered={false} call={call as any}
        onSignedIn={vi.fn()} onBack={vi.fn()} onCancel={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText(/sign in to another account/i)).toBeNull();
  });

  it("makes the folder, then starts, sending only the harness, name and again", async () => {
    const { call } = fake();
    setup(call);
    expect(screen.getByText(/Its login folder will be next to your other Claude logins/)).toBeInTheDocument();
    await start();
    expect(call.mock.calls).toEqual([
      ["khipu_account_new_folder", { harness: "claude", name: "Claude client" }],
      ["khipu_account_sign_in_new", { harness: "claude", again: false }],
    ]);
  });

  it("shows the waiting words and the folder", async () => {
    const { call } = fake();
    setup(call);
    await start();
    expect(screen.getByText(/Waiting for you to finish signing in in your browser\. Started 3 min ago; Khipu stops waiting after 15 minutes\./)).toBeInTheDocument();
    expect(screen.getByText("~/.claude-client")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop and cancel" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start again" })).toBeInTheDocument();
  });

  it("shows a Grok code and link, and writes nothing to localStorage", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const { call } = fake({
      khipu_account_sign_in_new: () => ({
        ok: true,
        data: { ok: true, attempt: { ...ATTEMPT, cli: "grok", cli_name: "Grok Build", folder: "~/.grok-second", kind: "code" }, state: "waiting", link: "https://accounts.x.ai/device", code: "WDQK-7P" },
      }),
    });
    setup(call, { cli: "grok", cliName: "Grok Build" });
    await start();
    expect(screen.getByText("WDQK-7P")).toBeInTheDocument();
    expect(screen.getByText(/Open the sign-in page that Grok Build named/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the page" })).toHaveAttribute("href", "https://accounts.x.ai/device");
    expect(screen.getByRole("button", { name: "Copy the code" })).toBeInTheDocument();
    expect(screen.getByText(/never keeps it/)).toBeInTheDocument();
    expect(setItem).not.toHaveBeenCalled();
    expect(JSON.stringify({ ...localStorage })).not.toContain("WDQK");
    setItem.mockRestore();
  });

  it("shows the no-code state", async () => {
    const { call } = fake({ khipu_account_sign_in_new: () => ({ ok: false, error: "no_code", refusal: { ok: false, error: "no_code" } }) });
    setup(call, { cli: "grok", cliName: "Grok Build" });
    await start();
    expect(screen.getByText("Grok Build didn't show a sign-in code")).toBeInTheDocument();
    expect(screen.getByText(/Khipu waited 20 seconds for it\. Nothing was saved\./)).toBeInTheDocument();
  });

  it("shows the failed state with details when the sign-in times out", async () => {
    let n = 0;
    const { call } = fake({
      khipu_account_sign_in_status: () => ({ ok: true, data: { ok: true, attempt: ATTEMPT, state: ++n < 2 ? "waiting" : "timeout", error: "sign_in_timeout" } }),
    });
    setup(call);
    await start();
    await poll();
    expect(screen.queryByText("The sign-in didn't finish")).toBeNull();
    await poll();
    expect(screen.getByText("The sign-in didn't finish")).toBeInTheDocument();
    expect(screen.getByText(/Nothing was saved\. The empty login folder stays, so trying again uses the same one\./)).toBeInTheDocument();
    expect(screen.getByText(/Code: sign_in_timeout/)).toBeInTheDocument();
  });

  it("asks before closing on Escape, and Keep waiting keeps polling", async () => {
    const { call } = fake();
    const { onCancel } = setup(call);
    await start();
    await act(async () => {
      fireEvent.keyDown(document.body, { key: "Escape" });
    });
    const confirm = screen.getByRole("dialog");
    expect(within(confirm).getByText("Stop signing in?")).toBeInTheDocument();
    expect(onCancel).not.toHaveBeenCalled();
    await act(async () => {
      fireEvent.click(within(confirm).getByRole("button", { name: "Keep waiting" }));
    });
    expect(screen.queryByRole("dialog")).toBeNull();
    const before = commands(call).filter((c) => c === "khipu_account_sign_in_status").length;
    await poll(4000);
    expect(commands(call).filter((c) => c === "khipu_account_sign_in_status").length).toBe(before + 2);
    expect(commands(call)).not.toContain("khipu_account_sign_in_cancel");
  });

  it("Stop and cancel opens the same confirm and then cancels", async () => {
    const { call } = fake();
    const { onCancel } = setup(call);
    await start();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Stop and cancel" }));
    });
    const confirm = screen.getByRole("dialog");
    expect(within(confirm).getByText("Stop signing in?")).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(within(confirm).getByRole("button", { name: "Stop and cancel" }));
    });
    expect(call).toHaveBeenCalledWith("khipu_account_sign_in_cancel", { removeFolder: false });
    expect(onCancel).toHaveBeenCalled();
  });

  it("Start again starts a new sign-in with again true", async () => {
    const { call } = fake();
    setup(call);
    await start();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Start again" }));
    });
    expect(call).toHaveBeenLastCalledWith("khipu_account_sign_in_new", { harness: "claude", again: true });
    expect(commands(call).filter((c) => c === "khipu_account_new_folder")).toHaveLength(1);
  });

  it("Cancel from the failed state removes the empty folder", async () => {
    const { call } = fake({ khipu_account_sign_in_new: () => ({ ok: false, error: "busy", refusal: { ok: false, error: "busy" } }) });
    const { onCancel } = setup(call);
    await start();
    expect(screen.getByText("The sign-in didn't finish")).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    });
    expect(call).toHaveBeenCalledWith("khipu_account_sign_in_cancel", { removeFolder: true });
    expect(onCancel).toHaveBeenCalled();
  });

  it("hands the folder to onSignedIn when the sign-in lands", async () => {
    const { call } = fake({
      khipu_account_sign_in_status: () => ({ ok: true, data: { ok: true, attempt: ATTEMPT, state: "signed_in" } }),
    });
    const { onSignedIn } = setup(call);
    await start();
    await poll();
    expect(onSignedIn).toHaveBeenCalledTimes(1);
    expect(onSignedIn).toHaveBeenCalledWith("~/.claude-client");
  });
});

describe("SignInNewAccount lifecycle through the real callCli", () => {
  afterEach(() => invoke.mockReset());

  function mount(onCancel = vi.fn()) {
    render(
      <SignInNewAccount cli="claude" cliName="Claude" accountName="Claude client" offered
        onSignedIn={vi.fn()} onBack={vi.fn()} onCancel={onCancel} now={() => STARTED * 1000 + 60_000} />,
    );
    return onCancel;
  }

  const answer = (cmd: string) => {
    if (cmd === "khipu_account_new_folder") return JSON.stringify({ ok: true, folder: "~/.claude-client", reused: false });
    if (cmd === "khipu_account_sign_in_new") return JSON.stringify({ ok: true, attempt: ATTEMPT, state: "waiting" });
    throw new Error(`unexpected ${cmd}`);
  };

  it.each(["timeout", "not_signed_in", "couldnt_check"])("shows the failed state when status answers %s", async (state) => {
    invoke.mockImplementation(async (cmd: string) =>
      cmd === "khipu_account_sign_in_status" ? JSON.stringify({ ok: true, attempt: ATTEMPT, state }) : answer(cmd));
    mount();
    await start();
    await poll(4000);
    expect(screen.getByText("The sign-in didn't finish")).toBeInTheDocument();
  });

  it("shows the failed state when an older CLI sends a timeout as ok: false", async () => {
    let polls = 0;
    invoke.mockImplementation(async (cmd: string) => {
      if (cmd !== "khipu_account_sign_in_status") return answer(cmd);
      polls += 1;
      if (polls === 1) return JSON.stringify({ ok: false, attempt: ATTEMPT, state: "timeout", error: "sign_in_timeout" });
      return JSON.stringify({ ok: false, error: "no_attempt" });
    });
    mount();
    await start();
    await poll(8000);
    expect(screen.getByText("The sign-in didn't finish")).toBeInTheDocument();
    expect(screen.getByText(/Code: sign_in_timeout/)).toBeInTheDocument();
  });

  it("shows the failed state when the attempt is gone", async () => {
    invoke.mockImplementation(async (cmd: string) =>
      cmd === "khipu_account_sign_in_status" ? JSON.stringify({ ok: false, error: "no_attempt" }) : answer(cmd));
    mount();
    await start();
    await poll(4000);
    expect(screen.getByText("The sign-in didn't finish")).toBeInTheDocument();
  });

  it("Stop and cancel while the sign-in is starting stops the login that start then returns", async () => {
    let releaseStart: (v: string) => void = () => {};
    let rustAttempt: string | null = null; // models SignInNewState.attempt in lib.rs
    const cancels: string[] = [];
    invoke.mockImplementation(async (cmd: string) => {
      if (cmd === "khipu_account_sign_in_new") {
        const raw = await new Promise<string>((r) => { releaseStart = r; });
        rustAttempt = ATTEMPT.id;
        return raw;
      }
      if (cmd === "khipu_account_sign_in_cancel") {
        if (!rustAttempt) throw "No sign-in is running"; // lib.rs khipu_account_sign_in_cancel
        cancels.push(rustAttempt);
        rustAttempt = null;
        return JSON.stringify({ ok: true, state: "cancelled", stopped: "killed" });
      }
      if (cmd === "khipu_account_sign_in_status") return JSON.stringify({ ok: true, attempt: ATTEMPT, state: "waiting" });
      return answer(cmd);
    });
    const onCancel = mount();
    await start();
    expect(screen.getByText(/Starting the sign-in/)).toBeInTheDocument();
    await act(async () => { fireEvent.keyDown(document, { key: "Escape" }); });
    await act(async () => { fireEvent.click(screen.getAllByRole("button", { name: "Stop and cancel", hidden: true }).slice(-1)[0]); });
    // Nothing to stop yet: the dialog waits for start, and Escape now does nothing.
    expect(onCancel).not.toHaveBeenCalled();
    expect(screen.getByText(/Stopping the sign-in/)).toBeInTheDocument();
    await act(async () => { fireEvent.keyDown(document, { key: "Escape" }); });
    expect(onCancel).not.toHaveBeenCalled();
    // The CLI's start returns afterwards: the browser login is now running.
    await act(async () => { releaseStart(JSON.stringify({ ok: true, attempt: ATTEMPT, state: "waiting" })); });
    await poll(4000);
    expect(onCancel).toHaveBeenCalled();
    expect(cancels).toEqual([ATTEMPT.id]);
    expect(invoke.mock.calls.map((c) => c[0])).not.toContain("khipu_account_sign_in_status");
  });

  it("keeps the dialog open with a plain error when the cancel is refused", async () => {
    invoke.mockImplementation(async (cmd: string) => {
      if (cmd === "khipu_account_sign_in_cancel") return JSON.stringify({ ok: false, error: "busy" });
      if (cmd === "khipu_account_sign_in_status") return JSON.stringify({ ok: true, attempt: ATTEMPT, state: "waiting" });
      return answer(cmd);
    });
    const onCancel = mount();
    await start();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Stop and cancel" })); });
    await act(async () => { fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Stop and cancel" })); });
    expect(onCancel).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("Khipu couldn't stop the sign-in just now");
  });
});

describe("SignInNewAccount Try again after a folder refusal", () => {
  it("makes the folder again before starting", async () => {
    let made = 0;
    const { call } = fake({
      khipu_account_new_folder: () =>
        ++made === 1
          ? { ok: false, error: "folder_taken", refusal: { ok: false, error: "folder_taken" } }
          : { ok: true, data: { ok: true, folder: "~/.claude-client", reused: false } },
    });
    setup(call);
    await start();
    expect(screen.getByText("The sign-in didn't finish")).toBeInTheDocument();
    expect(commands(call)).not.toContain("khipu_account_sign_in_new");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });
    expect(commands(call)).toEqual(["khipu_account_new_folder", "khipu_account_new_folder", "khipu_account_sign_in_new"]);
    expect(call).toHaveBeenLastCalledWith("khipu_account_sign_in_new", expect.objectContaining({ harness: "claude" }));
    expect(screen.getByText(/Waiting for you to finish signing in/)).toBeInTheDocument();
  });
});

// Second review of 419e9c6: replies that cross a Start again, and a stop the
// CLI refuses or cannot finish.
describe("SignInNewAccount stop and Start again races", () => {
  const deferred = () => {
    let resolve!: (x: Reply) => void;
    const promise = new Promise<Reply>((r) => { resolve = r; });
    return { promise, resolve };
  };

  it("a status reply from before Start again never finishes the replacement attempt", async () => {
    const oldStatus = deferred();
    const replacement = deferred();
    let starts = 0;
    const { call } = fake({
      khipu_account_sign_in_new: () => (++starts === 1 ? { ok: true, data: { ok: true, attempt: ATTEMPT, state: "waiting" } } : replacement.promise),
      khipu_account_sign_in_status: () => oldStatus.promise,
    });
    const { onSignedIn } = setup(call);
    await start();
    await poll(2000);
    expect(commands(call).filter((c) => c === "khipu_account_sign_in_status")).toHaveLength(1);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Start again" })); });
    expect(starts).toBe(2);
    await act(async () => { oldStatus.resolve({ ok: true, data: { ok: true, state: "signed_in", attempt: ATTEMPT } }); });
    expect(onSignedIn).not.toHaveBeenCalled();
    // Once the replacement answers, a reply naming the old attempt is ignored too.
    const next = { ...ATTEMPT, id: "fedcba9876543210" };
    await act(async () => { replacement.resolve({ ok: true, data: { ok: true, attempt: next, state: "waiting" } }); });
    expect(onSignedIn).not.toHaveBeenCalled();
  });

  it("a status reply that names another attempt is ignored", async () => {
    const { call } = fake({
      khipu_account_sign_in_status: () => ({ ok: true, data: { ok: true, state: "signed_in", attempt: { ...ATTEMPT, id: "fedcba9876543210" } } }),
    });
    const { onSignedIn } = setup(call);
    await start();
    await poll(4000);
    expect(onSignedIn).not.toHaveBeenCalled();
    expect(screen.getByText(/Waiting for you to finish signing in/)).toBeInTheDocument();
  });

  it("Stop during start keeps the dialog open when the late cancel is refused", async () => {
    const starting = deferred();
    const { call } = fake({
      khipu_account_sign_in_new: () => starting.promise,
      khipu_account_sign_in_cancel: () => ({ ok: false, error: "stop_failed" }),
    });
    const { onCancel } = setup(call);
    await start();
    fireEvent.keyDown(document, { key: "Escape" });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Stop and cancel" })); });
    await act(async () => { starting.resolve({ ok: true, data: { ok: true, attempt: ATTEMPT, state: "waiting" } }); });
    expect(commands(call).filter((c) => c === "khipu_account_sign_in_cancel")).toHaveLength(1);
    expect(onCancel).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("Khipu couldn't stop the sign-in just now");
    expect(screen.getByRole("button", { name: "Stop again" })).toBeInTheDocument();
  });

  it("Stop never treats a surviving process group as a finished cancel, and Stop again can finish it", async () => {
    let cancels = 0;
    const { call } = fake({
      khipu_account_sign_in_cancel: () =>
        ++cancels === 1
          ? { ok: true, data: { ok: true, state: "cancelled", stopped: "left", left: [1000050] } }
          : { ok: true, data: { ok: true, state: "cancelled", stopped: "killed" } },
    });
    const { onCancel, onSignedIn } = setup(call);
    await start();
    fireEvent.click(screen.getByRole("button", { name: "Stop and cancel" }));
    await act(async () => { fireEvent.click(screen.getAllByRole("button", { name: "Stop and cancel" })[1]); });
    expect(onCancel).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("Khipu could not fully stop the sign-in");
    // No more polling once a stop was asked: a late sign-in never moves it on.
    await poll(4000);
    expect(onSignedIn).not.toHaveBeenCalled();
    expect(commands(call).filter((c) => c === "khipu_account_sign_in_status")).toHaveLength(0);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Stop again" })); });
    expect(cancels).toBe(2);
    expect(onCancel).toHaveBeenCalled();
  });

  it("the CLI's not_stopped refusal through the real callCli keeps the dialog open", async () => {
    invoke.mockImplementation(async (cmd: string) => {
      if (cmd === "khipu_account_new_folder") return JSON.stringify({ ok: true, folder: "~/.claude-client", reused: false });
      if (cmd === "khipu_account_sign_in_new") return JSON.stringify({ ok: true, attempt: ATTEMPT, state: "waiting" });
      if (cmd === "khipu_account_sign_in_status") return JSON.stringify({ ok: true, attempt: ATTEMPT, state: "waiting" });
      if (cmd === "khipu_account_sign_in_cancel")
        return JSON.stringify({ ok: false, error: "not_stopped", attempt: ATTEMPT, state: "not_stopped", stopped: "left", left: [1000050] });
      throw new Error(`unexpected ${cmd}`);
    });
    const onCancel = vi.fn();
    render(
      <SignInNewAccount cli="claude" cliName="Claude" accountName="Claude client" offered
        onSignedIn={vi.fn()} onBack={vi.fn()} onCancel={onCancel} now={() => NOW_MS} />,
    );
    await start();
    await act(async () => { fireEvent.keyDown(document, { key: "Escape" }); });
    await act(async () => { fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Stop and cancel" })); });
    expect(onCancel).not.toHaveBeenCalled();
    expect(screen.getByText("The sign-in is still running")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop again" })).toBeInTheDocument();
    invoke.mockReset();
  });

  it("a timed-out login that could not be fully stopped stays open with Stop again", async () => {
    const { call } = fake({
      khipu_account_sign_in_status: () => ({ ok: true, data: { ok: true, attempt: ATTEMPT, state: "not_stopped", stopped: "left", left: [1000050] } }),
    });
    const { onCancel } = setup(call);
    await start();
    await poll(2000);
    expect(onCancel).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("Khipu could not fully stop the sign-in");
    expect(screen.getByRole("button", { name: "Stop again" })).toBeInTheDocument();
  });

  it("Start again that finds the earlier login still running offers Stop again", async () => {
    let starts = 0;
    const { call } = fake({
      khipu_account_sign_in_new: () =>
        ++starts === 1
          ? { ok: true, data: { ok: true, attempt: ATTEMPT, state: "waiting" } }
          : { ok: false, error: "busy", refusal: { ok: false, error: "busy", attempt: ATTEMPT, left: [1000050] } },
    });
    setup(call);
    await start();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Start again" })); });
    expect(screen.getByRole("alert")).toHaveTextContent("Khipu could not fully stop the sign-in");
    expect(screen.getByRole("button", { name: "Stop again" })).toBeInTheDocument();
  });
});
