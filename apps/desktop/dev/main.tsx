// Every phase 9 (9C) state, one per URL hash, with fictional props: for
// reviewers and headless screenshots (see vite.config.ts here). Not part of
// the app's build.
import { StrictMode, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { ChevronRight } from "lucide-react";
import "../src/App.css";
import "../src/routingOverview.css";
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
import { RoutingSettings } from "../src/RoutingSettings";
import { OfflineBanner, SavedOnThisMac, WaitingToSync } from "../src/OfflineBanner";
import { OfflineWaitsCard } from "../src/SettingsControls";
import { SetupAccountsStep } from "../src/SetupAccounts";
import { PlanBlock } from "../src/PlansCard";
import { ProjectsSettings } from "../src/ProjectsSettings";
import type { ProjectsList } from "../src/ProjectsSettings";
import { useCliState } from "../src/settingsCli";
import type { UsageAccount } from "../src/PlansCard";
import { PaceSection } from "../src/RoutingSettings";
import { paceModel } from "../src/routingPace";
import { PACE_NOW, PACE_PLANS, PACE_VIEW } from "./paceFake";
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
function WholeAccount({ label, home, row }: { label: string; home: string; row: UsageAccount }) {
  return (
    <div className="route-settings">
      <AccountExtras label={label} harness="claude" home={home} row={row} support={HERE} now={NOW} onRename={() => {}} onStop={() => {}} />
      <PlanBlock row={row} now={NOW} />
    </div>
  );
}

function FinderHost() {
  const finder = useAccountsFinder(true);
  return (
    <div className="route-settings">
      <FinderTop finder={finder} />
      <FinderBottom finder={finder} />
    </div>
  );
}

/** Owed with the hub away (offline slice 6): each banner as the tab shows it,
 *  and a row with a queued change. Fictional data. */
function OfflineOwedStates() {
  const refreshed = new Date(2026, 9, 16, 9, 5).toISOString();
  return (
    <div className="route-settings">
      <p className="meta">Answered from this Mac's copy</p>
      <OfflineBanner state={{ kind: "stale", refreshedAt: refreshed }} />
      <p className="meta">Answered from this Mac's copy, no time on record</p>
      <OfflineBanner state={{ kind: "stale", refreshedAt: null }} />
      <p className="meta">The load timed out</p>
      <OfflineBanner state={{ kind: "unreachable" }} />
      <p className="meta">After Done or Snooze while the hub is away</p>
      <SavedOnThisMac show />
      <p className="meta">A row with a queued change</p>
      <div className="card">
        <div className="card-head owed-head">
          <span className="w88">Kind</span>
          <span className="grow">What you owe</span>
        </div>
        <div className="owed-row" style={{ display: "flex", gap: 12, padding: "8px 12px", alignItems: "center" }}>
          <span className="w88">Follow-up</span>
          <span className="grow owed-text">Send the invoice to Acme</span>
          <WaitingToSync pending={{ action: "close" }} />
        </div>
      </div>
    </div>
  );
}

/** The Pace group's body inside the page's own group card, with the dev
 *  fixture shaped like `khipu route --json`. Screenshots only. */
function PaceGroup() {
  const model = paceModel(PACE_VIEW, PACE_PLANS, PACE_NOW);
  const folds = { isOpen: () => false, toggle: () => {}, openKeys: () => {} };
  return (
    <div className="route-settings">
      <div className="fold disclose route-group" data-open="true">
        <button type="button" className="disclosure fold-btn" aria-expanded="true" data-settings-group="pace">
          <ChevronRight size={14} className="disclosure-chevron" aria-hidden />
          <span className="gname">Pace, caps and advice</span>
          <span className="gline">{model.line}</span>
          <span className="gtags">
            {model.tags.map((t) => (
              <span key={t.text} className={["tag", t.tone === "neutral" ? "" : t.tone].filter(Boolean).join(" ")}>
                <span className="dot" aria-hidden />{t.text}
              </span>
            ))}
          </span>
        </button>
        <div className="fold-body">
          <div className="disclose-body">
            <PaceSection data={PACE_VIEW} plans={PACE_PLANS} now={PACE_NOW} folds={folds} />
          </div>
        </div>
      </div>
    </div>
  );
}

const PN = (name: string, role: "canonical" | "alias", extra: object = {}) => ({ name, role, open_commitments: 0, standing_decisions: 0, episodes: 0, ...extra });
const PROJECTS_FIXTURE = {
  groups: [
    { canonical: "mrkinglollipop/olivewood", totals: { open_commitments: 9, standing_decisions: 31, episodes: 412 }, names: [
      PN("mrkinglollipop/olivewood", "canonical", { episodes: 14 }),
      PN("mrkinglollipop/church-harness", "alias", { source: "github", created_at: new Date().toISOString(), episodes: 398 }),
    ] },
    { canonical: "field-notes", totals: { open_commitments: 2, standing_decisions: 4, episodes: 57 }, names: [
      PN("field-notes", "canonical", { episodes: 50 }),
      PN("notes-scratch", "alias", { source: "checkout", created_at: "2026-10-12T17:40:00Z", episodes: 7 }),
    ] },
  ],
  pairs: [
    { a: "lollipop-labs/olivewood", b: "mrkinglollipop/olivewood", state: "asked", created_at: new Date().toISOString() },
    { a: "mrkinglollipop/khipu-site", b: "mrkinglollipop/site-2025", state: "kept_separate", decided_at: "2026-10-03T16:00:00Z" },
  ],
  pair_counts: {
    "mrkinglollipop/olivewood": { open_commitments: 9, episodes: 412, last_used: new Date().toISOString() },
    "lollipop-labs/olivewood": { open_commitments: 1, episodes: 14, last_used: "2026-10-17T16:00:00Z" },
  },
  sweep: { at: new Date().toISOString(), result: "ok" },
};

function ProjectsHost({ withQuestion = true }: { withQuestion?: boolean }) {
  const state = useCliState<ProjectsList>("khipu_projects_list", true);
  const waiting = withQuestion ? 1 : 0;
  const tabs = ["Database", "Capture & models", "Accounts and routing", "Owner actions", "Work", "Projects", "Optional features", "Embeddings"];
  return (
    <div className="settings-body" style={{ maxWidth: "none" }}>
      <div className="settings-split">
        <div className="subnav" role="tablist" aria-label="Settings sections">
          {tabs.map((t) => (
            <button key={t} type="button" role="tab" aria-selected={t === "Projects"} className={t === "Projects" ? "on" : undefined}>
              {t}
              {t === "Projects" && waiting ? <span className="nav-count">{waiting}</span> : null}
            </button>
          ))}
        </div>
        <div className="settings-pane"><ProjectsSettings active state={state} /></div>
      </div>
    </div>
  );
}

 /** Settings -> Accounts and routing with only Proposals and holds open
 *  (slice 15b), against the fictional record in fakeTauri. */
function RoutingProposals() {
  useState(() => {
    try {
      window.localStorage.setItem("khipu.routing.open", JSON.stringify(["holds"]));
    } catch {
      // The group can be opened by hand.
    }
  });
  return (
    <div className="settings-pane" style={{ width: 1184, maxWidth: "none", margin: "0 auto" }}>
      <RoutingSettings active />
    </div>
  );
}

const STATES: Record<string, { title: string; fake?: Fake; render: () => ReactNode }> = {
  pace: { title: "Pace, caps and advice (fictional mock data)", render: () => <PaceGroup /> },
  "projects": { title: "Settings: Projects", fake: { projects: PROJECTS_FIXTURE }, render: () => <ProjectsHost /> },
  "projects-empty": { title: "Settings: Projects, none", fake: { projects: { groups: [], pairs: [], sweep: { result: null } } }, render: () => <ProjectsHost withQuestion={false} /> },
  "routing-proposals": { title: "Proposals and holds (slice 15b)", fake: { route15b: true }, render: () => <RoutingProposals /> },
  "offline-owed": { title: "Owed with the hub away: the banners and a waiting row", render: () => <OfflineOwedStates /> },
  "offline-settings": { title: "Settings: When the hub is away", render: () => <div className="route-settings"><OfflineWaitsCard active /></div> },
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
  "account-folder-missing": {
    title: "Account: login folder not found (the whole account, as Settings shows it)",
    render: () => <WholeAccount label="Claude work" home="~/.claude-work" row={{ harness: "claude", account: "Claude work", home: "~/.claude-work", ok: false, error: "login_folder_missing", last_good_at: "2026-10-14T22:10:00Z", windows: [] }} />,
  },
  "account-signed-out": {
    title: "Account: signed out (the whole account, as Settings shows it)",
    render: () => <WholeAccount label="Claude second" home="~/.claude-second" row={{ harness: "claude", account: "Claude second", home: "~/.claude-second", ok: false, error: "not_signed_in", last_good_at: "2026-10-13T15:00:00Z", windows: [{ name: "weekly", used_pct: 60, window_mins: 10080, resets_at: "2026-10-19T15:00:00Z" }] }} />,
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
  if (hash === "routing-proposals") {
    // The Settings pane at its app width, uncapped by the 960 state frame.
    return (
      <main style={{ padding: 24 }} data-state={hash}>
        <h1 style={{ fontSize: 14, color: "var(--muted)" }}>{st.title}</h1>
        {st.render()}
      </main>
    );
  }
  return (
    // The pace group is screenshotted at the Settings pane's own 720px width,
    // so it wraps where the approved mock does rather than across a wider page.
    <main style={{ padding: 24, maxWidth: hash === "pace" ? 744 : hash.startsWith("projects") ? "none" : 960 }} data-state={hash}>
      <h1 style={{ fontSize: 14, color: "var(--muted)" }}>{st.title}</h1>
      {st.render()}
    </main>
  );
}

const initial = STATES[window.location.hash.replace(/^#/, "")];
setFake(initial?.fake ?? {});
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Page />
  </StrictMode>,
);
