// The sign-in step of "Add an account" -> "Sign in to another account"
// (docs/plans/2026-10-15-settings-clarity.md, section 6). Props-driven: the
// parent owns the dialog, the name and the "Use a login I already have" list.
// The webview only names the CLI and the account name; Rust holds the login
// folder Khipu made and the attempt id, so nothing here ever sends a folder.
// A sign-in code or link lives in this component's state only: never in
// localStorage, never in a log.

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { Dialog } from "./ui";
import { callCli } from "./settingsCli";
import type { CliResult } from "./settingsCli";

export type SignInCli = "claude" | "codex" | "grok" | "muse";
type Call = <T = Record<string, unknown>>(command: string, args?: Record<string, unknown>) => Promise<CliResult<T>>;

type Attempt = { id: string; folder: string; started: number; deadline: number };
type StartAnswer = { attempt: Attempt; state?: string; link?: string; code?: string };
type StatusAnswer = { attempt?: Attempt; state: string; error?: string };
type FolderAnswer = { folder: string };

type Phase = "intro" | "starting" | "waiting" | "nocode" | "failed";

const POLL_MS = 2000;

const CLI_TITLE: Record<SignInCli, string> = { claude: "Claude", codex: "Codex", grok: "Grok Build", muse: "Muse" };

function minutesSince(startedSec: number, nowMs: number): number {
  return Math.max(0, Math.floor((nowMs - startedSec * 1000) / 60000));
}

function clock(sec: number): string {
  return new Date(sec * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function safeLink(link: string | undefined): string | null {
  return link && /^https:\/\/[^\s]+$/.test(link) ? link : null;
}

/** Plain words for the CLI's refusal codes. */
function startProblem(code: string | undefined, cliName: string): string {
  switch (code) {
    case "busy":
      return "Another sign-in is already running. Stop it first, then try again.";
    case "not_proven":
      return `Khipu can't sign in a new ${cliName} account yet. Sign in inside ${cliName}, then choose Use a login I already have.`;
    case "not_installed":
      return `${cliName} isn't installed on this Mac.`;
    case "bad_name":
      return "That name can't make a login folder. Try a different name.";
    case "folder_taken":
      return "Khipu couldn't make a login folder for that name. Try a different name.";
    case "refused":
      return "Khipu refused to start this sign-in.";
    default:
      return `The sign-in didn't start${code ? ` (${code})` : ""}.`;
  }
}

export function SignInNewAccount({
  cli,
  cliName,
  accountName,
  offered,
  call = callCli as Call,
  onSignedIn,
  onBack,
  onCancel,
  now = Date.now,
}: {
  cli: SignInCli;
  cliName: string;
  accountName: string;
  offered: boolean;
  call?: Call;
  onSignedIn: (folder: string) => void;
  onBack: () => void;
  onCancel: () => void;
  now?: () => number;
}) {
  const [phase, setPhase] = useState<Phase>("intro");
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [link, setLink] = useState<string | null>(null);
  const [code, setCode] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [detail, setDetail] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [tick, setTick] = useState(0);
  const [copied, setCopied] = useState(false);
  const titleId = useId();
  const doneRef = useRef(false);
  // The owner's Stop and cancel while start had not answered yet: Rust has no
  // attempt to stop then, so the attempt start returns is stopped at once.
  const cancelAskedRef = useRef(false);
  // Whether Khipu made (or reused) the login folder for this dialog; Try
  // again after a folder refusal makes it first.
  const folderMadeRef = useRef(false);
  const [stopProblem, setStopProblem] = useState<string | null>(null);
  const signedInRef = useRef(onSignedIn);
  signedInRef.current = onSignedIn;

  const active = phase === "starting" || phase === "waiting";

  const fail = useCallback((text: string, why: string | null) => {
    setProblem(text);
    setDetail(why);
    setCode(null);
    setLink(null);
    setPhase("failed");
  }, []);

  const begin = useCallback(
    async (again: boolean) => {
      setPhase("starting");
      cancelAskedRef.current = false;
      setProblem(null);
      setDetail(null);
      setStopProblem(null);
      setCode(null);
      setLink(null);
      if (!again || !folderMadeRef.current) {
        const made = await call<FolderAnswer>("khipu_account_new_folder", { harness: cli, name: accountName });
        if (cancelAskedRef.current) return;
        if (!made.ok) {
          const err = (made.refusal?.error as string | undefined) ?? undefined;
          fail(startProblem(err, cliName), err ? `Code: ${err}` : made.error);
          return;
        }
        folderMadeRef.current = true;
      }
      const res = await call<StartAnswer>("khipu_account_sign_in_new", { harness: cli, again });
      if (cancelAskedRef.current) {
        // The dialog already closed: stop the login start just began.
        if (res.ok) await call("khipu_account_sign_in_cancel", { remove_folder: false });
        return;
      }
      if (!res.ok) {
        const err = res.refusal?.error as string | undefined;
        if (err === "no_code") {
          setPhase("nocode");
          return;
        }
        fail(startProblem(err, cliName), err ? `Code: ${err}` : res.error);
        return;
      }
      setAttempt(res.data.attempt);
      setLink(safeLink(res.data.link));
      setCode(res.data.code ?? null);
      setPhase("waiting");
    },
    [call, cli, cliName, accountName, fail],
  );

  // Poll while waiting.
  useEffect(() => {
    if (phase !== "waiting") return;
    doneRef.current = false;
    const timer = setInterval(async () => {
      setTick((n) => n + 1);
      const res = await call<StatusAnswer>("khipu_account_sign_in_status");
      if (doneRef.current) return;
      // A finished attempt is an answer; an older CLI sent its end states as
      // ok: false, so a refusal that names one is read the same way.
      const answer = res.ok ? res.data : (res.refusal as StatusAnswer | undefined);
      if (!res.ok && answer?.error === "no_attempt") {
        doneRef.current = true;
        fail(`The sign-in ended before Khipu could check it.`, "Code: no_attempt");
        return;
      }
      if (!answer || typeof answer.state !== "string") return; // keep waiting; the next poll tries again
      const { state, attempt: seen, error } = answer;
      if (state === "signed_in") {
        doneRef.current = true;
        signedInRef.current((seen ?? attempt)?.folder ?? "");
      } else if (state === "not_signed_in" || state === "couldnt_check" || state === "timeout") {
        doneRef.current = true;
        const started = attempt ? `started ${clock(attempt.started)}` : "";
        const stopped = `stopped ${clock(Math.floor(now() / 1000))}`;
        fail(
          state === "timeout"
            ? `Khipu waited ${Math.round(((attempt?.deadline ?? 0) - (attempt?.started ?? 0)) / 60) || 15} minutes and ${cliName} reported no sign-in.`
            : state === "couldnt_check"
              ? `${cliName} didn't answer when Khipu checked the sign-in.`
              : `${cliName} reported no sign-in.`,
          `Code: ${error ?? `sign_in_${state}`}${started ? ` · ${started}` : ""} · ${stopped}`,
        );
      }
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [phase, call, attempt, cliName, fail, now]);

  // Escape while signing in asks first, the same as Stop and cancel.
  useEffect(() => {
    if (!active || confirming) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopPropagation();
      setConfirming(true);
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [active, confirming]);

  if (!offered) return null;

  const stopAndCancel = async () => {
    setConfirming(false);
    if (phase === "starting") {
      // Start hasn't answered: nothing to stop yet. begin() stops the
      // attempt start returns, and never moves on to waiting.
      cancelAskedRef.current = true;
      onCancel();
      return;
    }
    doneRef.current = true;
    const res = await call("khipu_account_sign_in_cancel", { remove_folder: false });
    if (!res.ok && attempt) {
      // The login may still be running: keep the dialog so the owner can try again.
      doneRef.current = false;
      setStopProblem("Khipu couldn't stop the sign-in just now. Choose Stop and cancel again.");
      return;
    }
    onCancel();
  };
  const cancelFailed = async () => {
    await call("khipu_account_sign_in_cancel", { remove_folder: true });
    onCancel();
  };
  const copyCode = async () => {
    try {
      await navigator.clipboard.writeText(code ?? "");
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  const folder = attempt?.folder ?? "";
  const byCode = cli === "grok" || cli === "muse";
  void tick;

  return (
    <div className="signin-new" aria-labelledby={titleId}>
      {phase === "intro" ? (
        <>
          <h3 id={titleId}>{cliName}: sign in to another account</h3>
          <p>
            Its login folder will be next to your other {cliName} logins. Khipu never deletes it.
          </p>
          <p>
            {byCode
              ? `${cliName} shows a code. Khipu shows it here so you can approve it in your browser; this window waits until you finish.`
              : `Your browser opens the ${cliName} sign-in page. Sign in with the account you want to add; this window waits until you finish.`}
          </p>
          <div className="kit-dialog-actions">
            <button type="button" onClick={onCancel}>Cancel</button>
            <button type="button" onClick={onBack}>Back</button>
            <button type="button" className="primary" onClick={() => void begin(false)}>
              Open the sign-in page
            </button>
          </div>
        </>
      ) : null}

      {phase === "starting" ? (
        <>
          <h3 id={titleId}>{cliName}: signing in…</h3>
          <p role="status">
            <Loader2 size={14} className="spin" aria-hidden /> Starting the sign-in.
          </p>
        </>
      ) : null}

      {phase === "waiting" && attempt ? (
        <>
          <h3 id={titleId}>{byCode ? `${cliName}: approve the code` : `${cliName}: signing in…`}</h3>
          {byCode && code ? (
            <>
              <p>Open the sign-in page that {cliName} named, and enter this code:</p>
              <p className="signin-code" aria-label="Sign-in code"><code>{code}</code></p>
              <div className="kit-dialog-actions">
                {link ? (
                  <a className="button" href={link} target="_blank" rel="noreferrer">Open the page</a>
                ) : null}
                <button type="button" onClick={() => void copyCode()}>
                  {copied ? "Copied" : "Copy the code"}
                </button>
              </div>
              <p className="meta" role="status">
                Waiting for you to approve it. Started {minutesSince(attempt.started, now())} min ago; Khipu stops
                waiting after 15 minutes.
              </p>
              <p className="meta">
                Signing in to <code>{folder}</code>. Khipu shows this code only here and never keeps it.
              </p>
            </>
          ) : (
            <>
              <p role="status">
                Waiting for you to finish signing in in your browser. Started {minutesSince(attempt.started, now())}{" "}
                min ago; Khipu stops waiting after 15 minutes.
              </p>
              <p className="meta">
                Signing in to <code>{folder}</code>. Didn't see the browser open? Start again stops this sign-in
                and starts a new one.
              </p>
            </>
          )}
          {stopProblem ? <p role="alert">{stopProblem}</p> : null}
          <div className="kit-dialog-actions">
            <button type="button" onClick={() => setConfirming(true)}>Stop and cancel</button>
            <button type="button" onClick={() => void begin(true)}>Start again</button>
          </div>
        </>
      ) : null}

      {phase === "nocode" ? (
        <>
          <h3 id={titleId}>{cliName}: sign-in didn't start</h3>
          <p><strong>{cliName} didn't show a sign-in code</strong></p>
          <p>
            Khipu waited 20 seconds for it. Nothing was saved. Start again, or sign in inside {cliName} and then
            choose Use a login I already have.
          </p>
          <div className="kit-dialog-actions">
            <button type="button" onClick={() => void cancelFailed()}>Cancel</button>
            <button type="button" onClick={onBack}>Back</button>
            <button type="button" className="primary" onClick={() => void begin(true)}>Start again</button>
          </div>
        </>
      ) : null}

      {phase === "failed" ? (
        <>
          <h3 id={titleId}>{cliName}: sign-in failed</h3>
          <p role="alert"><strong>The sign-in didn't finish</strong></p>
          <p>
            {problem} Nothing was saved. The empty login folder stays, so trying again uses the same one.
          </p>
          {detail ? (
            <details>
              <summary>Details</summary>
              <p className="meta">{detail}</p>
            </details>
          ) : null}
          <div className="kit-dialog-actions">
            <button type="button" onClick={() => void cancelFailed()}>Cancel</button>
            <button type="button" onClick={onBack}>Back</button>
            <button type="button" className="primary" onClick={() => void begin(true)}>Try again</button>
          </div>
        </>
      ) : null}

      <Dialog open={confirming} className="kit-dialog" ariaLabelledBy={`${titleId}-stop`} onCancel={() => setConfirming(false)}>
        <div className="kit-dialog-body">
          <h2 id={`${titleId}-stop`}>Stop signing in?</h2>
          <p>
            Khipu stops waiting and closes the sign-in. Nothing is saved. The login folder stays for next time.
          </p>
          <div className="kit-dialog-actions">
            <button type="button" onClick={() => setConfirming(false)}>Keep waiting</button>
            <button type="button" className="danger" onClick={() => void stopAndCancel()}>Stop and cancel</button>
          </div>
        </div>
      </Dialog>
    </div>
  );
}

export { CLI_TITLE as SIGN_IN_CLI_TITLE };
