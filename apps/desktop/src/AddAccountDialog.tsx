/* The Add an account dialog (docs/plans/2026-10-15-settings-clarity.md,
   "Adding an account by hand (the dialog)"; mocks add-account.html and
   add-account-states.html). Three steps: the service, which login, then the
   name and one read of the unsaved account. Opened from Add an account… in
   Settings → Accounts and routing → Accounts, from setup's Add another
   account…, and from a Found row's Track… (straight to step 3).

   Nothing is saved until Save account (or Sign in now, which saves first and
   then starts the account's own sign-in). The new-folder sign-in (slice 9D)
   shows disabled with words saying it comes later. */

import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, ReactNode, RefObject } from "react";
import { open as openPicker } from "@tauri-apps/plugin-dialog";
import { Loader2 } from "lucide-react";
import { Dialog, Tag } from "./ui";
import {
  CLI_IDS,
  COMING,
  DOWNLOAD_PAGE,
  checkErrorWords,
  checkHelp,
  cliName,
  nameProblem,
  plural,
  reasonWords,
  readSignInOffers,
  saveCliSetting,
  saveNewAccount,
  serviceName,
  signInNow,
  suggestedName,
  tryAccount,
} from "./accountsApi";
import type { Candidate, CliId, CliState, DiscoverResult, SaveProblem, SignInOffers, Support, TryResult } from "./accountsApi";
import { fmtPct, meterTone, resetPhrase, signInOutcome, tightestWindow, windowLabel } from "./PlansCard";
import type { UsageAccount, UsageAccountEntry } from "./PlansCard";
import { SignInNewAccount } from "./SignInNewAccount";
import "./accounts.css";

// ---------------------------------------------------------------------------
// A dialog that keeps focus inside itself and gives it back to its opener
// ---------------------------------------------------------------------------

const FOCUSABLE =
  'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])';

function focusables(root: HTMLElement | null): HTMLElement[] {
  return root ? Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)) : [];
}

/** The native modal (ui.tsx) plus a Tab trap that does not depend on the
 *  webview, Escape as Cancel, and focus returned to whatever opened it when
 *  it closes or unmounts. */
export function AccountsDialog({
  open,
  labelledBy,
  onCancel,
  initialFocusRef,
  children,
}: {
  open: boolean;
  labelledBy: string;
  onCancel: () => void;
  initialFocusRef?: RefObject<HTMLElement | null>;
  children: ReactNode;
}) {
  const body = useRef<HTMLDivElement>(null);
  /** The control that opened the dialog, kept until focus is back on it. */
  const giveBack = useRef<HTMLElement | null>(null);
  const returnFocus = () => {
    const el = giveBack.current;
    // A control that closed the dialog to move on (an Edit link) has already
    // put focus where it belongs; only a focus left on nothing goes back.
    const now = document.activeElement;
    if (now && now !== document.body && now !== el && !body.current?.contains(now)) return true;
    if (el && el.isConnected) el.focus();
    return el != null && document.activeElement === el;
  };
  // The opener is read in a layout effect: it runs before the native
  // <Dialog>'s own effect calls showModal(), which moves focus inside.
  const isOpen = useRef(false);
  useLayoutEffect(() => {
    if (!open) return;
    isOpen.current = true;
    const active = document.activeElement;
    if (active instanceof HTMLElement && active !== document.body && !body.current?.contains(active)) giveBack.current = active;
    return () => {
      isOpen.current = false;
      // The modal is still open here, so the opener is inert: give focus
      // back once it has closed (or unmounted), and again on its close event.
      window.setTimeout(() => {
        if (!isOpen.current && returnFocus()) giveBack.current = null;
      }, 0);
    };
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const t = window.setTimeout(() => {
      const first = initialFocusRef?.current ?? focusables(body.current)[0];
      first?.focus();
    }, 0);
    return () => window.clearTimeout(t);
  }, [open, initialFocusRef]);
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      onCancel();
      return;
    }
    if (e.key !== "Tab") return;
    const list = focusables(body.current);
    if (!list.length) return;
    const first = list[0];
    const last = list[list.length - 1];
    const here = document.activeElement;
    if (e.shiftKey && (here === first || !body.current?.contains(here))) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && (here === last || !body.current?.contains(here))) {
      e.preventDefault();
      first.focus();
    }
  };
  return (
    <Dialog open={open} className="kit-dialog aa-dialog" ariaLabelledBy={labelledBy} onCancel={onCancel}
      onClose={() => {
        if (!isOpen.current && giveBack.current && returnFocus()) giveBack.current = null;
      }}>
      <div ref={body} className="kit-dialog-body" onKeyDown={onKeyDown}>
        {children}
      </div>
    </Dialog>
  );
}

/** A native folder or file picker; focus goes back to `opener` when it
 *  returns, picked or not. */
export async function pickPath(
  opener: HTMLElement | null,
  kind: "folder" | "file",
  defaultPath?: string | null,
): Promise<string | null> {
  try {
    const picked = await openPicker({
      directory: kind === "folder",
      multiple: false,
      ...(defaultPath ? { defaultPath } : {}),
    });
    return typeof picked === "string" && picked ? picked : null;
  } catch (e) {
    console.error("picker failed:", e);
    return null;
  } finally {
    opener?.focus();
  }
}

// ---------------------------------------------------------------------------
// Words for a login's state
// ---------------------------------------------------------------------------

function shortVersion(v: string | null | undefined): string | null {
  const m = /^(\d+)\.(\d+)/.exec(v ?? "");
  return m ? `${m[1]}.${m[2]}` : null;
}

function serviceLine(cli: CliId, st: CliState | undefined, alreadyTracked: boolean): string {
  if (!st || st.state === "not_installed") return "Not found on this Mac.";
  if (st.state === "cant_run") return st.path ? `Found at ${st.path}, but Khipu can't start it.` : "Found, but Khipu can't start it.";
  const ver = shortVersion(st.version);
  const is = `${cliName(cli)}${ver ? ` ${ver}` : ""} is installed.`;
  if (cli === "cursor") return alreadyTracked ? "Cursor has one login per Mac, and Khipu already tracks it." : `${is} Cursor has one login per Mac.`;
  if (cli === "muse") return alreadyTracked ? "Muse has one login per Mac, and Khipu already tracks it." : `${is} Muse has one login per Mac.`;
  if (cli === "grok") return `${is} Khipu can use a login you made in Grok Build.`;
  return `${is} Can hold several logins.`;
}

function loginStateWords(c: Candidate): string {
  const parts: string[] = [];
  if (c.state === "signed_in") {
    const plan = (c.plan ?? "").trim();
    parts.push(plan ? `Signed in · ${plan.charAt(0).toUpperCase()}${plan.slice(1)}` : "Signed in, plan not read yet");
  } else if (c.state === "not_signed_in") parts.push("Not signed in");
  else if (c.state === "couldnt_check") parts.push(`Couldn't check: ${reasonWords(c.cli, c.reason)}`);
  else parts.push("Not checked yet");
  const n = c.conversations_7d;
  if (n != null) parts.push(n === 0 ? "no conversation used it this week" : `used by ${plural(n, "conversation", "conversations")} this week`);
  return parts.join(" · ");
}

function loginTag(c: Candidate): ReactNode {
  if (c.state === "signed_in") return <Tag tone="ok" dot>Signed in</Tag>;
  if (c.state === "not_signed_in") return <Tag>Not signed in</Tag>;
  if (c.state === "couldnt_check") return <Tag tone="warn" dot>Couldn't check</Tag>;
  return <Tag>Not checked</Tag>;
}

/** Whether the owner may pick this login in step 2: a folder found only by
 *  its name must be checked first (decision 20). */
function pickable(c: Candidate): boolean {
  return c.state !== "not_checked" && c.state !== "pending";
}

function candidateCheckSummary(cli: CliId, candidates: Candidate[]): string {
  const name = serviceName(cli);
  const count = (state: Candidate["state"]) => candidates.filter((candidate) => candidate.state === state).length;
  const checked = count("signed_in") + count("not_signed_in");
  const words = (n: number) => `${n} ${name} ${n === 1 ? "login" : "logins"}`;
  const parts: string[] = [checked ? `Khipu checked ${words(checked)}.` : `Khipu did not check any ${name} logins.`];
  const couldntCheck = count("couldnt_check");
  // A pending probe has not told Khipu whether this login is signed in yet,
  // so say that it is unchecked rather than quietly counting it as checked.
  const unchecked = count("not_checked") + count("pending");
  if (couldntCheck) parts.push(`${words(couldntCheck)} could not be checked.`);
  if (unchecked) parts.push(`${words(unchecked)} ${unchecked === 1 ? "is" : "are"} unchecked.`);
  return parts.join(" ");
}

/** Where each CLI keeps its default login, as the mocks show it. */
const DEFAULT_HOME: Record<string, string> = { claude: "~/.claude", codex: "~/.codex", grok: "~/.grok" };

function folderOf(c: Candidate): string | null {
  return c.default || c.cli === "muse" || c.cli === "cursor" || c.cli === "glm" ? null : c.path;
}

// ---------------------------------------------------------------------------
// The dialog
// ---------------------------------------------------------------------------

type Read =
  | { kind: "reading" }
  | { kind: "done"; result: TryResult }
  | { kind: "failed" };

type SignIn = { kind: "running"; label: string } | { kind: "done"; text: string };

export type AddAccountStart = {
  cli?: CliId;
  /** Track… on a Found row: that login, straight to step 3. */
  candidate?: Candidate;
  step?: 1 | 2 | 3;
  /** For previews and tests: the step-3 read, so none runs. */
  read?: TryResult | "reading" | "failed";
  name?: string;
  problem?: SaveProblem;
};

export function AddAccountDialog({
  open,
  onClose,
  discovery,
  onRediscover,
  onCheck,
  tracked,
  shownDefaults,
  support,
  start,
  onSaved,
  onShowAccount,
  onReload,
  canSignInNew,
}: {
  open: boolean;
  onClose: () => void;
  discovery: DiscoverResult | null;
  /** Check again: a new discovery run (it reads every CLI's state too). */
  onRediscover: (now?: boolean) => Promise<DiscoverResult | null>;
  /** The owner's Check on one folder. */
  onCheck: (folder: string) => Promise<{ ok: boolean; candidate?: Candidate | null; error?: string | null }>;
  /** The accounts tracked now. */
  tracked: UsageAccountEntry[];
  /** The default accounts as shown when the dialog opened (no list saved). */
  shownDefaults: UsageAccountEntry[] | null;
  support: Support;
  start?: AddAccountStart;
  onSaved: (label: string) => void;
  onShowAccount?: (label: string) => void;
  /** Reads the account list again after a save refused for its version. */
  onReload?: () => Promise<unknown>;
  /** Whether Sign in to another account is offered; left out, the dialog
   *  asks the CLI which services it is offered for. */
  canSignInNew?: boolean;
}) {
  const uid = useId().replace(/:/g, "");
  const titleId = `aa-title-${uid}`;
  const [step, setStep] = useState<1 | 2 | 3>(start?.step ?? (start?.candidate ? 3 : start?.cli ? 2 : 1));
  const [cli, setCli] = useState<CliId | null>(start?.candidate?.cli ?? start?.cli ?? null);
  const [chosenId, setChosenId] = useState<string | null>(start?.candidate?.id ?? null);
  const [extra, setExtra] = useState<Candidate | null>(start?.candidate ?? null);
  const [name, setName] = useState(start?.name ?? (start?.candidate ? suggestedName(start.candidate) : ""));
  const [read, setRead] = useState<Read | null>(
    start?.read === "reading" ? { kind: "reading" } : start?.read === "failed" ? { kind: "failed" } : start?.read ? { kind: "done", result: start.read } : null,
  );
  const [problem, setProblem] = useState<SaveProblem | null>(start?.problem ?? null);
  const [saving, setSaving] = useState(false);
  const [signIn, setSignIn] = useState<SignIn | null>(null);
  const [said, setSaid] = useState<string | null>(null);
  const [checkingAgain, setCheckingAgain] = useState(false);
  const [checkingFolder, setCheckingFolder] = useState<string | null>(null);
  const [way, setWay] = useState<"have" | "new">("have");
  const [newName, setNewName] = useState("");
  const [offers, setOffers] = useState<SignInOffers | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const headRef = useRef<HTMLHeadingElement>(null);
  const chooseFolderRef = useRef<HTMLButtonElement>(null);
  const loginRadioId = (cid: string) => `${uid}-l-${cid.replace(/[^a-zA-Z0-9_-]/g, "_")}`;

  // A new step moves focus to its heading ("Step 2 of 3 ..."), so the old
  // Next button unmounting never leaves focus on the page behind.
  const shownStep = useRef(step);
  useEffect(() => {
    if (shownStep.current === step) return;
    shownStep.current = step;
    // After the dialog's own first focus, which also waits a tick.
    const t = window.setTimeout(() => headRef.current?.focus(), 0);
    return () => window.clearTimeout(t);
  }, [step]);

  // Where focus goes once a check or a picked folder has been read: the
  // control it disabled while it ran is enabled again, or gone.
  const focusAfter = useRef<(() => HTMLElement | null) | null>(null);
  useEffect(() => {
    const pick = focusAfter.current;
    if (!pick) return;
    focusAfter.current = null;
    pick()?.focus();
  });

  // Which services Sign in to another account is offered for (Rust refuses
  // any other whatever the CLI says).
  useEffect(() => {
    if (!open || canSignInNew !== undefined || offers) return;
    let live = true;
    void readSignInOffers().then((next) => {
      if (live) setOffers(next);
    });
    return () => {
      live = false;
    };
  }, [open, canSignInNew, offers]);

  const clis = discovery?.clis ?? null;
  const singleTracked = (h: CliId) => (h === "muse" || h === "cursor") && tracked.some((t) => t.harness === h && !t.invalid);
  const candidates = useMemo(() => {
    const list = (discovery?.candidates ?? []).filter((c) => c.cli === cli);
    if (extra && extra.cli === cli && !list.some((c) => c.id === extra.id)) list.push(extra);
    return list;
  }, [discovery, cli, extra]);
  const chosen = candidates.find((c) => c.id === chosenId) ?? (extra && extra.id === chosenId ? extra : null);
  const trackedHere = tracked.filter((t) => t.harness === cli && !t.invalid);

  // As in the mocks: step 1 opens on the first installed service, step 2 on
  // the first signed-in login, so Next is ready when the usual choice is right.
  useEffect(() => {
    if (!open || step !== 1 || cli != null || !clis) return;
    const first = CLI_IDS.find((c) => clis[c]?.state === "installed" && !singleTracked(c));
    if (first) setCli(first);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, step, clis]);
  useEffect(() => {
    if (!open || step !== 2 || chosenId != null) return;
    const first = candidates.find((c) => c.state === "signed_in");
    if (first) setChosenId(first.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, step, cli, candidates]);

  // Step 3's one read of the unsaved account.
  const readSeq = useRef(0);
  const runRead = async (c: Candidate) => {
    const mine = ++readSeq.current;
    setRead({ kind: "reading" });
    const res = await tryAccount(c.cli, folderOf(c));
    if (mine !== readSeq.current) return;
    if (res.ok) setRead({ kind: "done", result: res.data });
    else {
      const refusal = res.refusal as TryResult | undefined;
      if (refusal && typeof refusal.error === "string") setRead({ kind: "done", result: refusal });
      else {
        console.error("khipu usage --try failed:", res.error);
        setRead({ kind: "failed" });
      }
    }
  };
  useEffect(() => {
    if (open && step === 3 && chosen && read == null) void runRead(chosen);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, step, chosenId]);

  const close = () => {
    readSeq.current++;
    onClose();
  };

  // ----- step 1 -------------------------------------------------------------
  const st = cli ? clis?.[cli] : undefined;
  const nothingInstalled = !!clis && CLI_IDS.every((c) => (clis[c]?.state ?? "not_installed") === "not_installed");
  const step1Next = !!cli && st?.state === "installed" && !singleTracked(cli);

  async function checkAgain() {
    if (!cli && !nothingInstalled) return;
    const before = cli ? clis?.[cli] : undefined;
    setCheckingAgain(true);
    setSaid(null);
    try {
      const d = await onRediscover(false);
      if (!d) {
        setSaid("Khipu couldn't check just now. Try again in a minute.");
        return;
      }
      if (!cli) {
        const now = CLI_IDS.filter((c) => d.clis?.[c]?.state === "installed").map((c) => cliName(c));
        setSaid(now.length ? `Found now: ${now.join(", ")}.` : "Still nothing installed on this Mac.");
        return;
      }
      const after = d.clis?.[cli];
      if (after?.state === "installed") {
        const v = shortVersion(after.version);
        setSaid(before?.state === "installed" ? `${cliName(cli)} is installed${v ? ` (${v})` : ""}.` : `${cliName(cli)} starts now${v ? `: version ${v}` : ""}.`);
      } else if (after?.state === "cant_run") {
        setSaid(`No change: Khipu still can't start ${cliName(cli)}. ${reasonWords(cli, after.reason)}.`);
      } else {
        setSaid(`No change: ${cliName(cli)} is still not installed on this Mac.`);
      }
    } finally {
      setCheckingAgain(false);
    }
  }

  async function setNodeFolder(opener: HTMLElement | null) {
    const near = st?.path ? st.path.replace(/\/[^/]*$/, "") : null;
    const picked = await pickPath(opener, "folder", near);
    if (!picked) return;
    const res = await saveCliSetting("node_folder", picked);
    if (!res.ok) {
      console.error("khipu config --set node_folder refused:", res.error);
      setSaid("That folder has no node program Khipu can run. Choose the folder that holds node.");
      return;
    }
    setSaid(`Saved the Node folder ${picked}. Choose Check again to try ${cliName(cli)} with it.`);
  }

  async function chooseCli(opener: HTMLElement | null) {
    const which = cli ?? "claude";
    const picked = await pickPath(opener, "file", st?.path ?? null);
    if (!picked) return;
    const res = await saveCliSetting(`cli_path.${which}`, picked);
    if (!res.ok) {
      console.error(`khipu config --set cli_path.${which} refused:`, res.error);
      setSaid(`That file isn't a program Khipu can run. Choose the ${cliName(which)} program itself.`);
      return;
    }
    setSaid(`Saved ${picked} as ${cliName(which)}. Choose Check again to try it.`);
  }

  // ----- step 2 -------------------------------------------------------------
  async function checkOne(c: Candidate) {
    if (!c.path) return;
    setCheckingFolder(c.path);
    setSaid(null);
    let target = c.id;
    try {
      const res = await onCheck(c.path);
      if (!res.ok) setSaid(checkErrorWords(res.error, c.cli));
      else if (res.candidate) {
        setExtra(res.candidate);
        if (pickable(res.candidate)) setChosenId(res.candidate.id);
        target = res.candidate.id;
        setSaid(`${res.candidate.path ?? "That login"}: ${loginStateWords(res.candidate)}.`);
      }
    } finally {
      setCheckingFolder(null);
      // Its radio once it can be chosen, else its Check button again.
      focusAfter.current = () => {
        const radio = document.getElementById(loginRadioId(target)) as HTMLInputElement | null;
        if (radio && !radio.disabled) return radio;
        return document.getElementById(`${loginRadioId(target)}-check`) ?? chooseFolderRef.current;
      };
    }
  }

  async function chooseFolder(opener: HTMLElement | null) {
    const picked = await pickPath(opener, "folder");
    if (!picked) return;
    setCheckingFolder(picked);
    setSaid(null);
    let target: string | null = null;
    try {
      const res = await onCheck(picked);
      if (!res.ok || !res.candidate) {
        setSaid(checkErrorWords(res.error, cli));
        return;
      }
      if (res.candidate.cli !== cli) {
        setSaid(`That folder is a ${cliName(res.candidate.cli)} login, not ${cliName(cli)}. Go Back and choose ${serviceName(res.candidate.cli)}.`);
        return;
      }
      setExtra(res.candidate);
      setChosenId(res.candidate.id);
      target = res.candidate.id;
      setSaid(`${res.candidate.path ?? picked}: ${loginStateWords(res.candidate)}.`);
    } finally {
      setCheckingFolder(null);
      // The chosen login's radio, else Choose another folder… again.
      focusAfter.current = () => {
        const radio = target ? (document.getElementById(loginRadioId(target)) as HTMLInputElement | null) : null;
        return radio && !radio.disabled ? radio : chooseFolderRef.current;
      };
    }
  }

  // A new login folder that signed in: step 3 reads and names it, as any
  // other login (nothing is saved until Save).
  const signedInNew = (folder: string) => {
    if (!cli || !folder) return;
    const made: Candidate = {
      id: `new:${cli}:${folder}`, cli, cli_name: cliName(cli), path: folder, default: false, sources: [],
      name: null, first_seen: null, last_checked: null, state: "signed_in", reason: null, plan: null,
      conversations_7d: null, needs_owner_check: false, offer_on_home: false,
    };
    setExtra(made);
    setChosenId(made.id);
    setName(newName.trim());
    setProblem(null);
    setRead(null);
    setSaid(null);
    setWay("have");
    setStep(3);
  };

  const goStep3 = () => {
    if (!chosen) return;
    setName(suggestedName(chosen));
    setProblem(null);
    setRead(null);
    setSaid(null);
    setStep(3);
  };

  // ----- step 3 -------------------------------------------------------------
  const takenNames = tracked.filter((t) => !t.invalid).map((t) => t.label);
  const typedProblem = chosen ? nameProblem(name, chosen.default, takenNames) : null;
  const nameErr = typedProblem ?? (problem?.field === "name" ? problem.message : null);
  const row: UsageAccount | null = read?.kind === "done" ? (read.result.row ?? null) : null;
  const readCode = read?.kind === "done" ? (read.result.row ? (read.result.row.ok === false ? read.result.row.error ?? null : null) : read.result.error ?? null) : null;
  const folderMissing = readCode === "folder_not_found" || readCode === "login_folder_missing";
  const notSignedIn = readCode === "not_signed_in";
  const duplicate = problem?.code === "login_taken";
  const canSignIn = chosen ? chosen.cli === "claude" || chosen.cli === "codex" : false;
  const entry = chosen ? { harness: chosen.cli, home: folderOf(chosen), label: name.trim() } : null;

  async function save() {
    if (!entry || typedProblem) return;
    setSaving(true);
    setProblem(null);
    try {
      const out = await saveNewAccount(entry, support, shownDefaults);
      if (!out.ok) {
        setProblem(out.problem);
        if (out.problem.field === "name") nameRef.current?.focus();
        return;
      }
      onSaved(out.label);
      close();
    } finally {
      setSaving(false);
    }
  }

  async function saveAndSignIn() {
    if (!entry || typedProblem) return;
    setSaving(true);
    setProblem(null);
    setSignIn({ kind: "running", label: entry.label });
    try {
      const out = await signInNow(entry, support, shownDefaults);
      if (!out.saved) {
        setSignIn(null);
        setProblem(out.problem);
        if (out.problem.field === "name") nameRef.current?.focus();
        return;
      }
      onSaved(out.account.label);
      const text = signInOutcome(out.signIn);
      setSignIn({
        kind: "done",
        text: out.signIn.ok && (out.signIn.data as { ok?: boolean } | undefined)?.ok
          ? `${out.account.label} is tracked. ${text}`
          : `${out.account.label} is tracked and shows "Not signed in" until a sign-in works. ${text}`,
      });
    } finally {
      setSaving(false);
    }
  }

  // ----- render -------------------------------------------------------------
  const stepLabel = <span className="aa-step">Step {step} of 3</span>;
  const footNote = <p className="aa-note">Accounts you add are tracked on this Mac only.</p>;
  const status = said ? (
    <p className="aa-note" role="status">
      {said}
    </p>
  ) : null;

  let body: ReactNode;
  if (step === 1) {
    body = (
      <>
        <h2 className="aa-step-heading" id={titleId} ref={headRef} tabIndex={-1}>
          {stepLabel}Add an account
        </h2>
        <p>Which service is the account with?</p>
        {!clis ? (
          <div className="aa-wait" role="status">
            <span className="aa-spin" aria-hidden />
            <span>Checking which programs are installed…</span>
          </div>
        ) : (
          <ul className="aa-list" role="radiogroup" aria-label="Service">
            {CLI_IDS.map((c) => {
              const s = clis[c];
              const off = singleTracked(c);
              const id = `${uid}-svc-${c}`;
              return (
                <li key={c} className={["aa-opt", cli === c ? "chosen" : null, off ? "off" : null].filter(Boolean).join(" ")}>
                  <input type="radio" name={`${uid}-svc`} id={id} checked={cli === c} disabled={off}
                    onChange={() => {
                      setCli(c);
                      setChosenId(null);
                      setExtra(null);
                      setSaid(null);
                    }} />
                  <label htmlFor={id}>
                    <b>{serviceName(c)}</b>
                    <span className="t2">{serviceLine(c, s, singleTracked(c))}</span>
                  </label>
                  {off ? (
                    <Tag>Already tracked</Tag>
                  ) : s?.state === "installed" ? (
                    <Tag tone="ok" dot>Installed</Tag>
                  ) : s?.state === "cant_run" ? (
                    <Tag tone="warn" dot>Can't run it</Tag>
                  ) : (
                    <Tag>Not installed</Tag>
                  )}
                </li>
              );
            })}
          </ul>
        )}
        {nothingInstalled ? (
          <p className="aa-note">
            Khipu reads a plan through the service's own program, so install one first:{" "}
            <a href={DOWNLOAD_PAGE.claude} target="_blank" rel="noreferrer">Claude Code download page</a> or{" "}
            <a href={DOWNLOAD_PAGE.codex} target="_blank" rel="noreferrer">Codex download page</a>. Grok Build and Muse: install
            them from their makers. Installed somewhere unusual?{" "}
            <button type="button" className="link sm" onClick={(e) => void chooseCli(e.currentTarget)}>
              Choose the CLI…
            </button>
          </p>
        ) : cli && st?.state === "cant_run" ? (
          <div className="callout warn">
            <div className="txt">
              <b>Khipu found {cliName(cli)} but can't start it</b>
              <span>
                {st.reason === "node_missing"
                  ? `${cliName(cli)} needs Node, and Khipu couldn't find it next to ${cliName(cli)}. Show Khipu the folder that holds node (for example a Node version under ~/.nvm/versions/node), or choose the ${cliName(cli)} program yourself.`
                  : `${reasonWords(cli, st.reason)}. Choose the ${cliName(cli)} program yourself, or, if it needs Node, show Khipu the folder that holds node.`}
              </span>
            </div>
            <div className="cta">
              <button type="button" className={st.reason === "node_missing" ? "sm primary" : "sm"} onClick={(e) => void setNodeFolder(e.currentTarget)}>
                Set the Node folder…
              </button>
              <button type="button" className={st.reason === "node_missing" ? "sm" : "sm primary"} onClick={(e) => void chooseCli(e.currentTarget)}>
                Choose the CLI…
              </button>
              <button type="button" className="sm" disabled={checkingAgain} aria-busy={checkingAgain || undefined} onClick={() => void checkAgain()}>
                {checkingAgain ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                Check again
              </button>
            </div>
          </div>
        ) : cli && clis && st?.state !== "installed" && !singleTracked(cli) ? (
          <div className="callout">
            <div className="txt">
              <b>{cliName(cli)} is not installed on this Mac</b>
              <span>
                Khipu reads a plan through the service's own program, so it needs {cliName(cli)} first.{" "}
                {DOWNLOAD_PAGE[cli] ? `Get it from the ${cliName(cli)} download page` : "Get it from its maker"}, open it once,
                then choose Check again. Nothing is saved until then.
              </span>
            </div>
            <div className="cta">
              {DOWNLOAD_PAGE[cli] ? (
                <a className="sm btn-link" href={DOWNLOAD_PAGE[cli]} target="_blank" rel="noreferrer">
                  Open the {cliName(cli)} download page
                </a>
              ) : null}
              <button type="button" className="sm" disabled={checkingAgain} aria-busy={checkingAgain || undefined} onClick={() => void checkAgain()}>
                {checkingAgain ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                Check again
              </button>
            </div>
          </div>
        ) : null}
        {status}
        {footNote}
        <div className="aa-actions">
          <button type="button" className="aa-left" ref={cancelRef} onClick={close}>
            Cancel
          </button>
          {nothingInstalled ? (
            <button type="button" disabled={checkingAgain} onClick={() => void checkAgain()}>
              Check again
            </button>
          ) : null}
          <button type="button" className="primary" disabled={!step1Next}
            onClick={() => {
              setSaid(null);
              if (cli === "muse" || cli === "cursor") {
                const muse = (discovery?.candidates ?? []).find((c) => c.cli === cli) ?? {
                  id: `${cli}:default`, cli, cli_name: cliName(cli), path: null, default: true, sources: ["installed"],
                  name: serviceName(cli), first_seen: null, last_checked: null, state: "pending" as const, reason: null, plan: null,
                  conversations_7d: null, needs_owner_check: false, offer_on_home: false,
                };
                setExtra(muse);
                setChosenId(muse.id);
                setName(suggestedName(muse));
                setRead(null);
                setStep(3);
              } else setStep(2);
            }}>
            Next
          </button>
        </div>
      </>
    );
  } else if (step === 2 && cli) {
    const svc = serviceName(cli);
    const defaultTracked = trackedHere.find((t) => !t.home);
    const offeredNew = canSignInNew ?? offers?.[cli]?.offered === true;
    const ways = (
      <div className="aa-ways" role="radiogroup" aria-label="How to add it">
        <div className={way === "have" || !offeredNew ? "aa-way chosen" : "aa-way"}>
          <input type="radio" name={`${uid}-way`} id={`${uid}-w1`} checked={way === "have" || !offeredNew}
            onChange={() => setWay("have")} />
          <label htmlFor={`${uid}-w1`}>
            <b>Use a login I already have</b>
            <span>Pick one of the {svc} logins on this Mac.</span>
          </label>
        </div>
        <div className={!offeredNew ? "aa-way off" : way === "new" ? "aa-way chosen" : "aa-way"}>
          <input type="radio" name={`${uid}-way`} id={`${uid}-w2`} checked={offeredNew && way === "new"} disabled={!offeredNew}
            onChange={() => setWay("new")} aria-describedby={offeredNew ? undefined : `${uid}-w2-coming`} />
          <label htmlFor={`${uid}-w2`}>
            <b>Sign in to another account</b>
            <span>
              {cli === "grok"
                ? "Khipu makes a new login folder and shows you a code to approve in your browser."
                : `Khipu makes a new login folder and opens the ${svc} sign-in page.`}
            </span>
            {offeredNew ? null : <span className="aa-coming" id={`${uid}-w2-coming`}>{COMING.signInNew}</span>}
          </label>
        </div>
      </div>
    );
    if (offeredNew && way === "new") {
      const newFieldId = `${uid}-new-name`;
      const newErr = newName.trim() ? nameProblem(newName, false, takenNames) : null;
      body = (
        <SignInNewAccount
          cli={cli as Exclude<CliId, "glm" | "cursor">}
          cliName={cliName(cli)}
          accountName={newName.trim()}
          offered
          canBegin={!!newName.trim() && !newErr}
          heading={(title) => (
            <h2 className="aa-step-heading" id={titleId} ref={headRef} tabIndex={-1}>
              {stepLabel}
              {title}
            </h2>
          )}
          introExtra={
            <>
              {ways}
              <div className="aa-field">
                <label htmlFor={newFieldId}>Name for this account</label>
                <input id={newFieldId} value={newName} maxLength={64} autoComplete="off" spellCheck={false}
                  aria-invalid={newErr ? true : undefined} aria-describedby={`${newFieldId}-h`}
                  onChange={(e) => setNewName(e.target.value)} />
                <p className="set-help" id={`${newFieldId}-h`}>
                  {newErr ? <span className="aa-err">{newErr}</span> : "Khipu makes the login folder from this name."}
                </p>
              </div>
            </>
          }
          onSignedIn={signedInNew}
          onBack={() => setWay("have")}
          onCancel={close}
        />
      );
    } else body = (
      <>
        <h2 className="aa-step-heading" id={titleId} ref={headRef} tabIndex={-1}>
          {stepLabel}
          {svc}: which login?
        </h2>
        {ways}
        {candidates.length === 0 ? (
          <div className="empty">
            <b>No other {svc} logins found</b>
            <span>
              {defaultTracked
                ? `The only ${svc} login on this Mac is the default one, and Khipu already tracks it as ${defaultTracked.label}. To add a second account, sign in to it here.`
                : `Khipu found no ${svc} login it doesn't already track. Sign in to one in ${cliName(cli)} first, or choose its folder.`}
            </span>
            <button type="button" className="sm" disabled={!offeredNew} aria-describedby={offeredNew ? undefined : `${uid}-w2-coming`}
              onClick={() => setWay("new")}>
              Sign in to another account
            </button>
          </div>
        ) : (
          <ul className="aa-list" role="radiogroup" aria-label={`${svc} logins on this Mac`}>
            {trackedHere.map((t, i) => {
              const id = `${uid}-t-${i}`;
              return (
                <li key={`t-${t.home ?? ""}`} className="aa-opt off">
                  <input type="radio" name={`${uid}-login`} id={id} disabled />
                  <label htmlFor={id}>
                    <b>
                      {t.home ? (
                        <span className="aa-mono">{t.home}</span>
                      ) : DEFAULT_HOME[t.harness ?? ""] ? (
                        <>
                          Default login <span className="meta aa-mono">{DEFAULT_HOME[t.harness ?? ""]}</span>
                        </>
                      ) : (
                        <>Default login</>
                      )}
                    </b>
                    <span className="t2">Khipu already reads its plan.</span>
                  </label>
                  <Tag>Tracked as {t.label}</Tag>
                </li>
              );
            })}
            {candidates.map((c) => {
              const id = loginRadioId(c.id);
              const can = pickable(c);
              const helpId = `${id}-help`;
              return (
                <li key={c.id} className={["aa-opt", chosenId === c.id ? "chosen" : null, can ? null : "off"].filter(Boolean).join(" ")}>
                  <input type="radio" name={`${uid}-login`} id={id} checked={chosenId === c.id} disabled={!can}
                    onChange={() => setChosenId(c.id)} />
                  <label htmlFor={id}>
                    <b>
                      {c.default ? (
                        <>
                          Default login <span className="meta aa-mono">{c.path}</span>
                        </>
                      ) : (
                        <span className="aa-mono">{c.path}</span>
                      )}
                    </b>
                    <span className="t2">{loginStateWords(c)}</span>
                    {c.state === "not_checked" ? (
                      <span className="t2" id={helpId}>{checkHelp(c.cli)}</span>
                    ) : null}
                  </label>
                  <span className="aa-opt-end">
                    {loginTag(c)}
                    {c.state === "not_checked" || c.state === "couldnt_check" ? (
                      <button type="button" className="sm" id={`${id}-check`} aria-describedby={c.state === "not_checked" ? helpId : undefined}
                        disabled={checkingFolder != null} aria-busy={checkingFolder === c.path || undefined}
                        aria-label={`${c.state === "not_checked" ? "Check" : "Check again"} the ${cliName(c.cli)} login at ${c.path}`}
                        onClick={() => void checkOne(c)}>
                        {checkingFolder === c.path ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                        {c.state === "not_checked" ? "Check" : "Check again"}
                      </button>
                    ) : null}
                  </span>
                </li>
              );
            })}
          </ul>
        )}
        {candidates.length ? (
          <p className="aa-note">
            Khipu looked at folder names in your home folder and at the logins your conversations ran under. {candidateCheckSummary(cli, candidates)} It never opened what is inside them.
          </p>
        ) : null}
        <p className="aa-note">
          <button type="button" className="link sm" ref={chooseFolderRef} disabled={checkingFolder != null} onClick={(e) => void chooseFolder(e.currentTarget)}>
            Choose another folder…
          </button>{" "}
          {candidates.length ? "for a login kept somewhere else." : "if you keep a login somewhere Khipu does not look."}
        </p>
        {status}
        {footNote}
        <div className="aa-actions">
          <button type="button" className="aa-left" ref={cancelRef} onClick={close}>
            Cancel
          </button>
          <button type="button" onClick={() => { setSaid(null); setStep(1); }}>
            Back
          </button>
          <button type="button" className="primary" disabled={!chosen || !pickable(chosen)} onClick={goStep3}>
            Next
          </button>
        </div>
      </>
    );
  } else if (chosen) {
    const where = (chosen.cli === "muse" || chosen.cli === "cursor") ? `${serviceName(chosen.cli)}'s one login on this Mac` : chosen.path ?? "its default login";
    const label = name.trim() || suggestedName(chosen);
    const w = row && row.ok !== false ? tightestWindow(row) : null;
    const pct = w && typeof w.used_pct === "number" ? w.used_pct : null;
    let result: ReactNode;
    if (read == null || read.kind === "reading") {
      result = (
        <div className="aa-result" role="status">
          <div className="aa-wait">
            <span className="aa-spin" aria-hidden />
            <span>Reading its plan once…</span>
          </div>
        </div>
      );
    } else if (folderMissing) {
      result = (
        <div className="callout warn" role="alert">
          <div className="txt">
            <b>Khipu can't find the login folder {chosen.path}</b>
            <span>It may have been moved or removed. Go Back to choose another login, or another folder.</span>
          </div>
        </div>
      );
    } else if (read.kind === "failed") {
      result = (
        <div className="aa-result" role="status">
          <div className="aa-result-head">
            <span className="hdot warn" aria-hidden />
            <b>Khipu couldn't read it just now</b>
          </div>
          <p>The read didn't run. You can save it now; Plans reads it on its next check.</p>
          <p>
            <button type="button" className="link sm" onClick={() => void runRead(chosen)}>Read again now</button>
          </p>
        </div>
      );
    } else if (notSignedIn) {
      result = (
        <div className="aa-result" role="status">
          <div className="aa-result-head">
            <span className="hdot warn" aria-hidden />
            <b>Not signed in</b>
          </div>
          <p>
            {cliName(chosen.cli)} says <span className="aa-mono">{where}</span> is not signed in, so there is no plan to read.{" "}
            {canSignIn
              ? `Sign in now tracks ${label} first, then opens the sign-in page; if you stop there, it stays tracked as not signed in. Or save it and sign in later from Plans.`
              : `Sign in from ${cliName(chosen.cli)} itself, or save it now and sign in later. ${COMING.codeSignIn}`}
          </p>
        </div>
      );
    } else if (row && row.ok !== false) {
      result = (
        <div className="aa-result" role="status">
          <div className="aa-result-head">
            <span className="hdot ok" aria-hidden />
            <b>{pct != null ? "Live: read just now" : "Signed in: read just now, no figures yet"}</b>
          </div>
          {w && pct != null ? (
            <div className="aa-plan">
              <span className="plan-name">
                {label}
                {row.plan ? <span className="meta"> {row.plan.charAt(0).toUpperCase() + row.plan.slice(1)}</span> : null}
              </span>
              <span className={["plan-meter", meterTone(pct)].filter(Boolean).join(" ")} role="meter" aria-label={`${label} ${windowLabel(w)}`}
                aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.min(100, Math.max(0, pct))} aria-valuetext={`${fmtPct(pct)} used`}>
                <i style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
              </span>
              <span className="plan-pct">{fmtPct(pct)}</span>
              <span className="plan-when meta">
                {windowLabel(w)}
                {resetPhrase(w.resets_at) ? ` · ${resetPhrase(w.resets_at)}` : ""}
              </span>
            </div>
          ) : null}
          <p>
            {(chosen.cli === "muse" || chosen.cli === "cursor") ? `${serviceName(chosen.cli)}'s one login on this Mac.` : <>Login folder <span className="aa-mono">{where}</span>.</>} Reading a plan uses no turn and costs nothing.
          </p>
        </div>
      );
    } else {
      const code = readCode;
      result = (
        <div className="aa-result" role="status">
          <div className="aa-result-head">
            <span className="hdot warn" aria-hidden />
            <b>Signed in, but its plan can't be read</b>
          </div>
          <p>
            {reasonWords(chosen.cli, code)}. Khipu will try again on its next read, about every 2 minutes while the app is open.
            You can save it now; Plans shows "Can't read its plan" until a read works.
          </p>
          <p>
            <button type="button" className="link sm" onClick={() => void runRead(chosen)}>Read again now</button>
          </p>
        </div>
      );
    }
    const fieldId = `${uid}-name`;
    const otherName = problem?.other ?? null;
    body = (
      <>
        <h2 className="aa-step-heading" id={titleId} ref={headRef} tabIndex={-1}>
          {stepLabel}Check and save
        </h2>
        <div className="aa-field">
          <label htmlFor={fieldId}>Name</label>
          <input id={fieldId} ref={nameRef} value={name} maxLength={64} autoComplete="off" spellCheck={false}
            aria-invalid={nameErr ? true : undefined} aria-describedby={`${fieldId}-h`}
            disabled={saving || signIn != null}
            onChange={(e) => {
              setName(e.target.value);
              if (problem?.field === "name") setProblem(null);
            }} />
          <p className="set-help" id={`${fieldId}-h`}>
            {nameErr ? <span className="aa-err">{nameErr}</span> : "Shown in Plans, Accounts and routing, and to your conversations."}
          </p>
        </div>
        {duplicate ? (
          <div className="callout warn" role="alert">
            <div className="txt">
              <b>{problem?.message}</b>
              <span>
                <span className="aa-mono">{where}</span> is a login Khipu already reads{otherName ? ` as ${otherName}` : ""}, so both would read
                the same plan. Nothing new is saved; {otherName ?? "that account"} stays as it is, and you can rename it there.
              </span>
            </div>
          </div>
        ) : (
          result
        )}
        {problem && !duplicate && problem.field !== "name" ? (
          <p className="set-error" role="alert">
            {problem.message}
            {problem.reload && onReload ? (
              <>
                {" "}
                <button type="button" className="sm" disabled={saving}
                  onClick={async () => { await onReload(); setProblem(null); }}>
                  Reload
                </button>
              </>
            ) : null}
          </p>
        ) : null}
        {signIn?.kind === "running" ? (
          <div className="aa-wait" role="status">
            <span className="aa-spin" aria-hidden />
            <span>Khipu tracks {signIn.label} first, then opens the sign-in page. Finish signing in in your browser; Khipu reads the account again when you're done.</span>
          </div>
        ) : signIn?.kind === "done" ? (
          <p className="aa-note" role="status">{signIn.text}</p>
        ) : null}
        {footNote}
        <div className="aa-actions">
          {signIn?.kind === "done" ? (
            <button type="button" className="primary" onClick={close}>
              Done
            </button>
          ) : (
            <>
              <button type="button" className="aa-left" ref={cancelRef} onClick={close} disabled={signIn?.kind === "running"}>
                Cancel
              </button>
              <button type="button" disabled={saving || signIn?.kind === "running"}
                onClick={() => { readSeq.current++; setStep((chosen.cli === "muse" || chosen.cli === "cursor") ? 1 : 2); setRead(null); setProblem(null); setSaid(null); }}>
                Back
              </button>
              {duplicate ? (
                <button type="button" className="primary" onClick={() => { if (otherName) onShowAccount?.(otherName); close(); }}>
                  {otherName ? `Show ${otherName}` : "Close"}
                </button>
              ) : notSignedIn ? (
                <>
                  <button type="button" disabled={saving || !!typedProblem} onClick={() => void save()}>
                    Save without signing in
                  </button>
                  {canSignIn ? (
                    <button type="button" className="primary" disabled={saving || !!typedProblem} aria-busy={saving || undefined}
                      onClick={() => void saveAndSignIn()}>
                      Sign in now
                    </button>
                  ) : null}
                </>
              ) : (
                <button type="button" className="primary" disabled={saving || !!nameErr || folderMissing || read?.kind === "reading" || read == null}
                  aria-busy={saving || undefined} onClick={() => void save()}>
                  {saving ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                  Save account
                </button>
              )}
            </>
          )}
        </div>
      </>
    );
  } else {
    body = (
      <>
        <h2 className="aa-step-heading" id={titleId} ref={headRef} tabIndex={-1}>Add an account</h2>
        <p>Choose a login first.</p>
        <div className="aa-actions">
          <button type="button" className="aa-left" onClick={close}>Cancel</button>
          <button type="button" onClick={() => setStep(cli ? 2 : 1)}>Back</button>
        </div>
      </>
    );
  }

  return (
    <AccountsDialog open={open} labelledBy={titleId} onCancel={() => { if (signIn?.kind !== "running") close(); }}>
      {body}
    </AccountsDialog>
  );
}
