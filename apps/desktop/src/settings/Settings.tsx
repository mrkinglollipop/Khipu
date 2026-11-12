// Settings: 13 rows on the left, one short page on the right. Same on both
// Macs unless a row says "Only on <this Mac>".


import { useEffect, useState } from "react";
import { ChevronLeft } from "lucide-react";
import { Body, Split, useListKeys } from "../shell";
import { synthProviderInfo } from "../SynthProvider";
import { fmtAgoSince } from "../time";
import { LookPage } from "./pages/Look";
import { readLook } from "./lookModel";
import { useSettingsData, thisMacRow } from "./data";
import { refreshHealth } from "../shared/healthCache";
import { macsSummary, signinsSummary } from "./words";
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

export type RowId =
  | "macs" | "updates" | "signins" | "apps" | "summaries" | "meaning" | "books"
  | "backups" | "where" | "look" | "login" | "help" | "agent";

export const ROW_IDS: RowId[] = ["macs", "updates", "signins", "apps", "summaries", "meaning", "books", "backups", "where", "look", "login", "help", "agent"];

export function Settings({ ctx, active }: { ctx: SettingsCtx; active: boolean }) {
  const [sel, setSel] = useState<RowId>("macs");
  const [open, setOpen] = useState(false);
  // A deep link from a notice, a guide or another page opens that row.
  const openRow = ctx.openRow;
  useEffect(() => {
    if (openRow && (ROW_IDS as string[]).includes(openRow.id)) {
      setSel(openRow.id as RowId);
      setOpen(true);
    }
  }, [openRow]);
  const { macs, health, reloadMacs } = useSettingsData(active);
  const keys = useListKeys();
  const mac = ctx.thisMac ?? "this Mac";

  const lastBackup = health.data?.last_backup_at ?? thisMacRow(macs.data)?.health?.last_backup_at ?? null;
  const look = readLook();
  const synthName = synthProviderInfo(ctx.synth.provider)?.name ?? ctx.synth.provider;
  // The row says which model is in use, so the choice can be found from the list.
  const synthModel = ctx.synth.model_id.trim() || synthProviderInfo(ctx.synth.provider)?.defaultModel || "";

  const rows: Array<{ id: RowId; label: string; note?: string; value: string; warn?: boolean }> = [
    { id: "macs", label: "Your Macs", value: macsSummary(macs.data), warn: Boolean(macs.data?.macs.some((m) => m.stale)) },
    { id: "updates", label: "Updates", value: `Khipu ${ctx.appVersion}` },
    { id: "signins", label: "Sign-ins", value: signinsSummary(macs.data), warn: signinsSummary(macs.data).includes("needs") || signinsSummary(macs.data).includes("need ") },
    { id: "apps", label: "Apps that use Khipu", value: "" },
    { id: "summaries", label: "Models and API keys", value: synthModel ? `${synthName} · ${synthModel}` : synthName },
    { id: "meaning", label: "Search by meaning", value: "" },
    { id: "books", label: "Include your books in search", value: "" },
    { id: "backups", label: "Backups", value: lastBackup ? `Last ${fmtAgoSince(lastBackup) ?? "recorded"}` : health.refreshing ? "Checking…" : "None recorded" },
    { id: "where", label: "Where memory is kept", value: ctx.dsnOk === false ? "Not set up" : ctx.dbReachable === false ? "Not reachable" : ctx.dbReachable ? "Connected" : "" },
    { id: "look", label: "Look", note: `Only on ${mac}`, value: look === "system" ? "Follows this Mac" : look === "dark" ? "Dark" : "Light" },
    { id: "login", label: `Open Khipu when ${mac} starts`, note: `Only on ${mac}`, value: "" },
    { id: "help", label: "Help", value: "Opens the website" },
    { id: "agent", label: "Details for an agent", value: "Copy" },
  ];
  const cur = rows.find((r) => r.id === sel) ?? rows[0];

  const list = (
    <div {...keys} className="st-list">
      <p className="st-cap">Same on both Macs unless marked.</p>
      {rows.map((r) => (
        <button
          key={r.id}
          type="button"
          className="sh-row st-row"
          aria-current={sel === r.id ? "true" : undefined}
          onClick={() => { setSel(r.id); setOpen(true); }}
        >
          <span className="st-rk">
            {r.label}
            {r.note ? <small>{r.note}</small> : null}
          </span>
          {r.value ? <span className={r.warn ? "st-rv warn" : "st-rv"}>{r.value}</span> : null}
        </button>
      ))}
    </div>
  );

  const pageActive = active;
  const page = (() => {
    switch (cur.id) {
      case "macs": return <YourMacsPage macs={macs} active={pageActive} onNamed={reloadMacs} anotherMac={ctx.anotherMac} />;
      case "updates": return <UpdatesPage appVersion={ctx.appVersion} thisMac={ctx.thisMac} whatsNew={ctx.whatsNew} />;
      case "signins": return <SignInsPage macs={macs} active={pageActive} onSignedIn={reloadMacs} />;
      case "apps": return <AppsPage ctx={ctx} active={pageActive} onAnotherMac={() => { setSel("macs"); setOpen(true); }} />;
      case "summaries": return <SummariesPage ctx={ctx} active={pageActive} />;
      case "meaning": return <SearchByMeaningPage active={pageActive} />;
      case "books": return <BooksPage active={pageActive} />;
      case "backups": return <BackupsPage macs={macs} health={health} run={ctx.runKhipu} onBackedUp={() => { void refreshHealth(); void reloadMacs(); }} />;
      case "where": return <WhereKeptPage ctx={ctx} health={health} />;
      case "look": return <LookPage macName={ctx.thisMac} part="look" />;
      case "login": return <LookPage macName={ctx.thisMac} part="opening" />;
      case "help": return <HelpPage ctx={ctx} />;
      case "agent": return <AgentNotePage health={health} />;
    }
  })();

  const detail = (
    <div className="st-page">
      <button type="button" className="st-back" onClick={() => setOpen(false)}>
        <ChevronLeft size={16} aria-hidden /> Settings
      </button>
      <h1 className="st-h">{cur.label}</h1>
      {cur.note ? <p className="st-muted">{cur.note}.</p> : null}
      {page}
    </div>
  );

  return (
    <div className="st-fill"><Body>
      <Split list={list} listLabel="Settings" detail={detail} detailLabel={cur.label} detailOpen={open} />
    </Body></div>
  );
}
