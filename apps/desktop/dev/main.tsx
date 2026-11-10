// Every phase 9 (9C) state, one per URL hash, with fictional props: for
// reviewers and headless screenshots (see vite.config.ts here). Not part of
// the app's build.
import { StrictMode, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { createRoot } from "react-dom/client";
import "../src/App.css";
import "../src/accounts.css";
import { AddAccountDialog } from "../src/AddAccountDialog";
import type { AddAccountStart } from "../src/AddAccountDialog";
import {
  AccountExtras,
  AccountsGroupBar,
  AutoTrackSwitch,
  CliSettingsRows,
  FinderBottom,
  FinderTop,
  FoundRows,
  HomeAccountCards,
  HomeAutoCard,
  HomeFoundCard,
  NoAccountsTracked,
  NotOfferedAgain,
  RenameDialog,
  StopTrackingDialog,
  movedPairs,
  useAccountsFinder,
} from "../src/FoundAccounts";
import type { AccountRefs } from "../src/FoundAccounts";
import App from "../src/App";
import { ProjectLists } from "../src/memory/ProjectLists";
import { applyStoredLook } from "../src/settings/lookModel";
import { OrderAndRules } from "../src/accounts/OrderAndRules";
import { SetupAccountsStep } from "../src/SetupAccounts";
import type { DiscoverResult, Support } from "../src/accountsApi";
import {
  CLIENT,
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
} from "../src/__tests__/accountsFixtures";
import type { Fake } from "./fakeTauri";

const READING: Support = { state: "reading" };
const HERE: Support = { state: "here", status: status9B() };

applyStoredLook();

function setFake(f: Fake) {
  (window as unknown as { __khipuFake?: Fake }).__khipuFake = f;
}

function Dlg({ start, disc, support = HERE, tracked = TRACKED }: { start?: AddAccountStart; disc?: DiscoverResult; support?: Support; tracked?: typeof TRACKED }) {
  return (
    <AddAccountDialog open onClose={() => {}} discovery={disc ?? discovery()} onRediscover={async () => discovery()}
      onCheck={async () => ({ ok: true, candidate: { ...CODEX_WORK, state: "signed_in", plan: "plus" } })} tracked={tracked}
      shownDefaults={TRACKED} support={support} start={start} onSaved={() => {}} />
  );
}

const REFS: AccountRefs = {
  rules: [{ n: 4, text: "Use Claude client only for client repos" }],
  note: true,
  holds: [{ id: "h1a2b3c4", text: "client work only", until: "2026-10-19T13:00:00Z" }],
  lanes: ["review-1", "docs"],
  priority: { place: 2, next: "Codex" },
  pausedUntil: "2026-10-16T13:00:00Z",
  last: false,
};
const ACCT = { key: "claude|~/.claude-client", label: "Claude client", harness: "claude", home: "~/.claude-client" };
const NOT_SIGNED = { ok: false, row: { harness: "claude", ok: false, error: "not_signed_in", windows: [] } };

/** One tracked account's fold body as Settings renders it: its extras, then
 *  its plan block (which holds Sign in again and when it was last read). */
function FinderHost() {
  const finder = useAccountsFinder(true);
  return (
    <div className="route-settings">
      <FinderTop finder={finder} />
      <FinderBottom finder={finder} />
    </div>
  );
}

const STATES: Record<string, { title: string; fake?: Fake; render: () => ReactNode }> = {
  "dialog-step1": { title: "Dialog, step 1: choose the service", render: () => <Dlg /> },
  "dialog-step1-not-installed": { title: "Dialog, step 1: service not installed", render: () => <Dlg disc={discovery({ clis: CLIS_CODEX_NOT })} start={{ cli: "codex", step: 1 }} /> },
  "dialog-step1-nothing-installed": { title: "Dialog, step 1: nothing installed", render: () => <Dlg disc={discovery({ clis: CLIS_NONE })} tracked={[]} /> },
  "dialog-step1-cant-run": { title: "Dialog, step 1: installed but can't run", render: () => <Dlg disc={discovery({ clis: CLIS_CODEX_CANT })} start={{ cli: "codex", step: 1 }} /> },
  "dialog-step2": { title: "Dialog, step 2: logins Khipu found", render: () => <Dlg start={{ cli: "claude", step: 2 }} /> },
  "dialog-step2-empty": { title: "Dialog, step 2: nothing besides what is tracked", render: () => <Dlg start={{ cli: "claude", step: 2 }} disc={discovery({ candidates: [] })} /> },
  "dialog-step2-not-checked": { title: "Dialog, step 2: a folder found only by name", render: () => <Dlg start={{ cli: "codex", step: 2 }} disc={discovery({ candidates: [CODEX_WORK, CODEX_LAB] })} /> },
  "dialog-step2-grok": { title: "Dialog, step 2: Grok Build", render: () => <Dlg start={{ cli: "grok", step: 2 }} disc={discovery({ candidates: [GROK_SECOND] })} tracked={[...TRACKED, { label: "Grok Build", harness: "grok" }]} /> },
  "dialog-step3-reading": { title: "Dialog, step 3: reading", render: () => <Dlg start={{ candidate: CLIENT, read: "reading" }} /> },
  "dialog-step3-live": { title: "Dialog, step 3: Live", render: () => <Dlg start={{ candidate: CLIENT, read: { ok: true, row: liveRow() } }} /> },
  "dialog-step3-read-fails": { title: "Dialog, step 3: signed in, read fails", render: () => <Dlg start={{ candidate: GROK_SECOND, read: { ok: false, row: { harness: "grok", ok: false, error: "timeout", windows: [] } } }} /> },
  "dialog-step3-not-signed-in": { title: "Dialog, step 3: not signed in", render: () => <Dlg start={{ candidate: OLD, read: NOT_SIGNED }} /> },
  "dialog-step3-duplicate": {
    title: "Dialog, step 3: duplicate login",
    render: () => <Dlg start={{ candidate: OFFICE, name: "Claude team", read: { ok: true, row: liveRow() }, problem: { code: "login_taken", field: "home", other: "Claude work", message: "Khipu already tracks this login as Claude work." } }} />,
  },
  "dialog-step3-name-taken": { title: "Dialog, step 3: name already used", render: () => <Dlg start={{ candidate: { ...CODEX_LAB, state: "signed_in" }, name: "Codex", read: { ok: true, row: liveRow({ harness: "codex" }) } }} /> },
  "dialog-step3-folder-missing": { title: "Dialog, step 3: login folder not found", render: () => <Dlg start={{ candidate: CLIENT, read: { ok: false, error: "folder_not_found" } }} /> },
  "found-rows": { title: "Found rows: Found, not checked, couldn't check", render: () => <FoundRows candidates={[CLIENT, CODEX_WORK, CODEX_LAB]} support={HERE} now={NOW} onTrack={() => {}} onCheck={() => {}} onDecline={() => {}} /> },
  "found-rows-reading": { title: "Found rows while the account list is read", render: () => <FoundRows candidates={[CLIENT, CODEX_WORK]} support={READING} now={NOW} onTrack={() => {}} onCheck={() => {}} onDecline={() => {}} /> },
  "found-moved": {
    title: "Found row: moved?",
    render: () => (
      <FoundRows candidates={[OFFICE]} support={HERE} onTrack={() => {}} onCheck={() => {}} onDecline={() => {}}
        moved={movedPairs([{ harness: "claude", account: "Claude work", home: "~/.claude-work", key: "claude|~/.claude-work", ok: false, error: "login_folder_missing", windows: [] }], [OFFICE])} />
    ),
  },
  "group-bar": { title: "Group bar: last run, Forget", render: () => <AccountsGroupBar looking={false} lastRun={{ at: NOW.toISOString(), couldnt_check: 1 }} onLookNow={() => {}} onAdd={() => {}} onForget={async () => "Khipu forgot which logins your conversations used."} /> },
  looking: { title: "Group bar: Looking…", render: () => <AccountsGroupBar looking lastRun={null} onLookNow={() => {}} onAdd={() => {}} /> },
  "accounts-live": { title: "Accounts group, live against the fake (9B not here)", fake: { disc: discovery({ candidates: [CLIENT, CODEX_WORK, CODEX_LAB] }) }, render: () => <FinderHost /> },
  "accounts-live-9b": { title: "Accounts group, live against the fake (9B here)", fake: { nineB: true, disc: discovery({ candidates: [CLIENT, CODEX_WORK] }) }, render: () => <FinderHost /> },
  "not-offered-again": {
    title: "Not offered again (open it)",
    render: () => (
      <NotOfferedAgain onOfferAgain={() => {}} support={{ state: "here", status: status9B({ declined: [
        { cli: "claude", path: "~/.claude-old", why: "you chose Not this one", at: "2026-10-09T15:00:00Z" },
        { cli: "grok", path: "~/.grok-test", why: "you stopped tracking it", at: "2026-10-02T15:00:00Z" },
      ] }) }} />
    ),
  },
  "auto-switch": { title: "Track new accounts automatically (read, then reading)", render: () => <><AutoTrackSwitch support={HERE} onChange={() => {}} /><AutoTrackSwitch support={READING} onChange={() => {}} /></> },
  "cli-settings": {
    title: "Where Khipu finds each program",
    render: () => (
      <CliSettingsRows onSet={() => {}} onCheckAgain={() => {}}
        clis={{ ...discovery().clis, grok: { cli: "grok", cli_name: "Grok Build", path: "/opt/fake/bin/grok", state: "cant_run", version: null, reason: "start_failed" } }}
        settings={{ node_folder: { value: "/home/someone/.nvm/versions/node/v20.11.0/bin", usable: true } }} />
    ),
  },
  "account-reading": { title: "Account: Rename and Stop tracking while the account list is read", render: () => <AccountExtras label="Claude work" harness="claude" home="~/.claude-work" support={READING} onRename={() => {}} onStop={() => {}} /> },
  rename: { title: "Rename dialog", render: () => <RenameDialog open account={ACCT} refs={REFS} taken={["Claude client", "Claude main", "Codex"]} support={HERE} onClose={() => {}} onRenamed={() => {}} /> },
  stop: { title: "Stop tracking dialog", render: () => <StopTrackingDialog open account={ACCT} refs={REFS} support={HERE} onClose={() => {}} onStopped={() => {}} /> },
  "stop-last": {
    title: "Stop tracking the last account",
    render: () => <StopTrackingDialog open account={{ key: "claude|", label: "Claude main", harness: "claude", home: null }} refs={{ ...REFS, rules: [], note: false, holds: [], lanes: [], priority: null, pausedUntil: undefined, last: true }} support={HERE} onClose={() => {}} onStopped={() => {}} />,
  },
  "no-accounts": { title: "No accounts tracked", render: () => <div className="card plans-card"><NoAccountsTracked onAdd={() => {}} /></div> },
  "home-found-card": { title: "Home: Found card, one find", render: () => <HomeFoundCard offers={[CLIENT]} support={HERE} onTrack={() => {}} onDecline={() => {}} onClose={() => {}} /> },
  "home-found-card-many": { title: "Home: Found card, two finds", render: () => <HomeFoundCard offers={[CLIENT, GROK_SECOND]} support={HERE} onTrack={() => {}} onDecline={() => {}} onClose={() => {}} /> },
  "home-auto-card": { title: "Home: automatic-tracking card", render: () => <HomeAutoCard card={{ key: "claude|~/.claude-client", label: "Claude client", path: "~/.claude-client", conversations: 4 }} onUndo={() => {}} onRename={() => {}} onClose={() => {}} /> },
  "home-cards-live": { title: "Home cards, live against the fake", fake: { disc: discovery({ candidates: [CLIENT] }) }, render: () => <HomeAccountCards active /> },
  "setup-step": { title: "Setup: Your accounts with what Khipu found", fake: { disc: discovery({ candidates: [CLIENT, OLD] }) }, render: () => <div className="onboard welcome accounts"><SetupAccountsStep active onBack={() => {}} onNext={() => {}} /></div> },
  "setup-nothing-found": { title: "Setup: nothing installed", fake: { disc: discovery({ candidates: [], clis: CLIS_NONE }), usage: { accounts: [] } }, render: () => <div className="onboard welcome accounts"><SetupAccountsStep active onBack={() => {}} onNext={() => {}} /></div> },
  // The whole window (sidebar, toolbar, old screens): `#app` opens on Today,
  // `#app-welcome` on first run, `#app-quiet` with nothing needing you.
  // Move around with the sidebar. Answers come from fakes/shell.ts.
  app: { title: "The whole window", render: () => <App /> },
  "app-quiet": { title: "The whole window, nothing needs you", fake: { quiet: true }, render: () => <App /> },
  "app-problem": { title: "The whole window, something wrong", fake: { healthProblem: true }, render: () => <App /> },
  "app-unknown": { title: "The whole window, a check could not run", fake: { healthUnknown: true }, render: () => <App /> },
  "app-nocheck": { title: "The whole window, health check fails", fake: { healthNone: true }, render: () => <App /> },
  "app-needs-more": { title: "Today: an item that can't be taken back, a sign-in, a shortened item", fake: { needsMore: true }, render: () => <App /> },
  "app-needs-changed": { title: "Today: the rule changes after it was read", fake: { needsChanged: true }, render: () => <App /> },
  "app-needs-slow": { title: "Today: answers take four seconds", fake: { needsSlow: true }, render: () => <App /> },
  // Memory: open the window, then pick Memory in the sidebar. Answers from fakes/memory.ts.
  "app-memory-pending": { title: "Memory, two forgets not yet sent", fake: { memPending: true }, render: () => <App /> },
  "app-memory-offline": { title: "The whole window, Only my words refused", fake: { memOffline: true }, render: () => <App /> },
  // Memory › Project work lists on its own, and with no project holding a list.
  "worklists": { title: "Project work lists", render: () => <ProjectLists /> },
  "worklists-none": { title: "Project work lists, none yet", fake: { noProjects: true }, render: () => <ProjectLists /> },
  "app-settings": { title: "The whole window; open Settings from the sidebar", render: () => <App /> },
  "app-settings-stale": { title: "Settings, the other Mac quiet for 40 minutes", fake: { settings: "stale" }, render: () => <App /> },
  "app-settings-samename": { title: "Settings, both Macs show the same computer name", fake: { settings: "samename" }, render: () => <App /> },
  "app-settings-one": { title: "Settings, no other Mac", fake: { settings: "one" }, render: () => <App /> },
  "app-settings-problem": { title: "Settings, a sign-in and the database need a look", fake: { settings: "problem" }, render: () => <App /> },
  // Accounts > Order and rules (slice A6): alone, and inside the window (`#app-rules`: open Accounts, then Order and rules). Answers come from fakes/orderRules.ts.
  "order-rules": { title: "Order and rules", fake: { orderRules: true }, render: () => <OrderAndRules active onSeeAll={() => {}} /> },
  "app-rules": { title: "The whole window, with Order and rules", fake: { orderRules: true }, render: () => <App /> },
  "app-welcome": { title: "The whole window, first run", fake: { welcome: true }, render: () => <App /> },
  // Accounts (slice A4): open the window, then press Accounts in the sidebar.
  "app-accounts": { title: "The whole window, Accounts", fake: { accounts: true }, render: () => <App /> },
  "app-accounts-problem": { title: "The whole window, Accounts, something wrong", fake: { accounts: true, accountsProblem: true }, render: () => <App /> },
  "app-accounts-ahead": { title: "The whole window, Accounts, one conversation running ahead", fake: { accounts: true, accountsAhead: true }, render: () => <App /> },
  "app-running": { title: "The whole window, Accounts, Running now", fake: { accounts: true, running: true }, render: () => <App /> },
  "app-running-silent": { title: "The whole window, Running now, a runner stopped reporting", fake: { accounts: true, running: true, runningSilent: true }, render: () => <App /> },
  "app-running-none": { title: "The whole window, Running now, no runner reporting", fake: { accounts: true, running: true, runningNone: true }, render: () => <App /> },
  "app-running-stale": { title: "The whole window, Running now, the other Mac is out of date", fake: { accounts: true, running: true, runningStale: true }, render: () => <App /> },
  "app-running-fail": { title: "The whole window, Running now, the read fails", fake: { accounts: true, running: true, runningFail: true }, render: () => <App /> },
  "app-accounts-all": { title: "The whole window, Accounts with all three views and every fake on", fake: { accounts: true, running: true, orderRules: true }, render: () => <App /> },
  "app-accounts-refuse": { title: "The whole window, Accounts, a pause is refused", fake: { accounts: true, accountsRefuse: true }, render: () => <App /> },
  "setup-signed-out": {
    title: "Setup: a default login that is not signed in",
    fake: { disc: discovery({ candidates: [] }), usage: { accounts: [
      { harness: "claude", account: "Claude Code", home: null, ok: false, error: "not_signed_in", windows: [] },
      { harness: "grok", account: "Grok Build", home: null, ok: false, error: "not_signed_in", windows: [] },
    ] } },
    render: () => <div className="onboard welcome accounts"><SetupAccountsStep active onBack={() => {}} onNext={() => {}} /></div>,
  },
};

function Page() {
  const [hash] = useState(() => window.location.hash.replace(/^#/, ""));
  // A new hash is a new state with its own fake: start over.
  useEffect(() => {
    const on = () => window.location.reload();
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const st = STATES[hash];
  if (!st) {
    return (
      <main style={{ padding: 24 }}>
        <h1>Accounts states (9C)</h1>
        <ul>
          {Object.entries(STATES).map(([k, v]) => (
            <li key={k}>
              <a href={`#${k}`}>#{k}</a>: {v.title}
            </li>
          ))}
        </ul>
      </main>
    );
  }
  if (hash.startsWith("app") || hash.startsWith("worklists") || hash === "routing-proposals" || hash === "routing-replication" || hash === "routing-needs" || hash === "routing-needs-worst" || hash === "routing-settings-cde" || hash === "routing-settings-cde-worst" || hash.startsWith("routing-overrides")) {
    // The Settings pane at its app width, uncapped by the 960 state frame.
    return <div style={{ height: "100%" }} data-state={hash}>{st.render()}</div>;
  }
  return (
    // The pace group is screenshotted at the Settings pane's own 720px width,
    // so it wraps where the approved mock does rather than across a wider page.
    <main style={{ padding: 24, maxWidth: hash.startsWith("macs") ? "none" : hash === "pace" ? 744 : hash.startsWith("delivery") ? 768 : hash.startsWith("projects") ? "none" : 960 }} data-state={hash}>
      <h1 style={{ fontSize: 14, color: "var(--muted)" }}>{st.title}</h1>
      {st.render()}
    </main>
  );
}

const initial = STATES[window.location.hash.replace(/^#/, "")];
setFake(initial?.fake ?? {});
// The whole-window states open past the one-time Welcome, except #app-welcome.
if (window.location.hash.startsWith("#app") && window.location.hash !== "#app-welcome") {
  try { window.localStorage.setItem("khipu.welcome.completed", "1"); } catch { /* preview only */ }
}
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Page />
  </StrictMode>,
);
