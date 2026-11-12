// Five Settings pages; old row ids remain accepted for notices and guides.


import { useEffect, useState, type ReactNode } from "react";
import { ChevronLeft, Sun, Sparkles, Database, Link, CircleHelp } from "lucide-react";
import { Body, Split, useListKeys } from "../shell";
import { modelSelection } from "./modelSources";
import { LookPage } from "./pages/Look";
import { useSettingsData } from "./data";
import { refreshHealth } from "../shared/healthCache";
import type { SettingsCtx } from "./context";
import { YourMacsPage } from "./pages/YourMacs";
import { UpdatesPage } from "./pages/Updates";
import { SignInsPage } from "./pages/SignIns";
import { AppsPage } from "./pages/Apps";
import { SummariesPage } from "./pages/Summaries";
import { SearchByMeaningPage } from "./pages/SearchByMeaning";
import { BooksPage } from "./pages/Books";
import { BackupsPage } from "./pages/Backups";
import { WhereKeptPage } from "./pages/WhereKept";
import { HelpPage } from "./pages/Help";
import { AgentNotePage } from "./pages/AgentNote";
import "./settings.css";

export const PAGE_IDS = ["general", "models", "storage", "connections", "help"] as const;
export type PageId = typeof PAGE_IDS[number];
export type RowId = PageId;
export const ROW_IDS = PAGE_IDS;
export const LEGACY_PAGES: Record<string, PageId> = {
  macs: "connections", updates: "general", signins: "connections", apps: "connections",
  summaries: "models", meaning: "general", books: "general", backups: "storage",
  where: "storage", look: "general", login: "general", agent: "help",
};
export function settingsPage(id: string): PageId | undefined {
  return PAGE_IDS.find((page) => page === id) ?? LEGACY_PAGES[id];
}
function Group({ title, children }: { title: string; children: ReactNode }) {
  return <section className="st-regroup" aria-label={title}><h2>{title}</h2><div className="st-card">{children}</div></section>;
}

export function Settings({ ctx, active }: { ctx: SettingsCtx; active: boolean }) {
  const [sel, setSel] = useState<PageId>("general");
  const [open, setOpen] = useState(false);
  // A deep link from a notice, a guide or another page opens that row.
  const openRow = ctx.openRow;
  useEffect(() => {
    if (openRow && settingsPage(openRow.id)) {
      setSel(settingsPage(openRow.id)!);
      setOpen(true);
    }
  }, [openRow]);
  const { macs, health, reloadMacs } = useSettingsData(active);
  const keys = useListKeys();
  const mac = ctx.thisMac ?? "this Mac";

  const synthModel = modelSelection(ctx.synth).model.replace("Claude ", "").replace("Other model…", "Other");
  const rows = [
    { id: "general", label: "General", icon: Sun, value: "" },
    { id: "models", label: "Models", icon: Sparkles, value: synthModel },
    { id: "storage", label: "Storage", icon: Database, value: "" },
    { id: "connections", label: "Connections", icon: Link, value: "" },
    { id: "help", label: "Help", icon: CircleHelp, value: "" },
  ] as const;
  const cur = rows.find((r) => r.id === sel) ?? rows[0];

  const list = (
    <div {...keys} className="st-list">
      <h2 className="st-cap">Settings</h2>
      {rows.map((r) => (
        <button
          key={r.id}
          type="button"
          className="sh-row st-row"
          aria-current={sel === r.id ? "true" : undefined}
          onClick={() => { setSel(r.id); setOpen(true); }}
        >
          <r.icon size={16} aria-hidden /><span className="st-rk">{r.label}</span>
          {r.value ? <span className="st-rv">{r.value}</span> : null}
        </button>
      ))}
    </div>
  );

  const pageActive = active;
  const page = (() => {
    switch (cur.id) {
      case "general": return <>
        <Group title="Appearance"><LookPage macName={mac} part="look" /></Group>
        <Group title="Starting up"><LookPage macName={mac} part="opening" /></Group>
        <Group title="Search"><SearchByMeaningPage active={pageActive} /><BooksPage active={pageActive} /></Group>
        <Group title="Updates"><UpdatesPage appVersion={ctx.appVersion} thisMac={ctx.thisMac} whatsNew={ctx.whatsNew} /></Group>
      </>;
      case "models": return <SummariesPage ctx={ctx} active={pageActive} />;
      case "storage": return <>
        <Group title="Database"><WhereKeptPage ctx={ctx} health={health} /></Group>
        <Group title="Backups"><BackupsPage macs={macs} health={health} run={ctx.runKhipu} onBackedUp={() => { void refreshHealth(); void reloadMacs(); }} /></Group>
      </>;
      case "connections": return <>
        <Group title="Apps"><AppsPage ctx={ctx} active={pageActive} onAnotherMac={() => document.getElementById("st-macs")?.scrollIntoView?.({ block: "start" })} /></Group>
        <Group title="Accounts"><SignInsPage macs={macs} active={pageActive} onSignedIn={reloadMacs} /></Group>
        <div id="st-macs"><Group title="Macs"><YourMacsPage macs={macs} active={pageActive} onNamed={reloadMacs} anotherMac={ctx.anotherMac} /></Group></div>
      </>;
      case "help": return <>
        <Group title="Help"><HelpPage ctx={ctx} /></Group>
        <Group title="When something is wrong"><h3 className="st-inline-title">Details for an agent</h3><AgentNotePage health={health} /></Group>
      </>;
    }
  })();

  const detail = (
    <div className="st-page">
      <button type="button" className="st-back" onClick={() => setOpen(false)}>
        <ChevronLeft size={16} aria-hidden /> Settings
      </button>
      <h1 className="st-h">{cur.label}</h1>
      {page}
    </div>
  );

  return (
    <div className="st-fill"><Body>
      <Split list={list} listLabel="Settings" detail={detail} detailLabel={cur.label} detailOpen={open} />
    </Body></div>
  );
}
