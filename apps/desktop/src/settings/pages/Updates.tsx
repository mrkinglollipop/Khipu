import { useState } from "react";
import type { ReactNode } from "react";
import { Loader2 } from "lucide-react";
import { check } from "@tauri-apps/plugin-updater";
import { relaunch } from "@tauri-apps/plugin-process";

/** Says only what is true: the version on this Mac, and a button that does what
 *  the old "Check for updates" did (check, then download, install, relaunch). */
export function UpdatesPage({ appVersion, thisMac, whatsNew }: { appVersion: string; thisMac: string | null; whatsNew?: ReactNode }) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  async function checkNow() {
    setBusy(true);
    setMsg(null);
    let stage: "check" | "download" = "check";
    try {
      const update = await check();
      if (!update) {
        setMsg(`Up to date (v${appVersion}).`);
        return;
      }
      setMsg(`Downloading v${update.version}…`);
      stage = "download";
      await update.downloadAndInstall();
      setMsg("Update installed. Khipu is restarting…");
      await relaunch();
    } catch (e) {
      const text = e instanceof Error ? e.message : String(e ?? "unknown error");
      setMsg(stage === "download" ? `The update could not be installed: ${text}` : `Khipu could not check for updates: ${text}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <p>Khipu {appVersion} on {thisMac ?? "this Mac"}.</p>
      <div className="st-actions">
        <button type="button" className="primary" disabled={busy} onClick={() => void checkNow()}>
          {busy ? <Loader2 size={14} className="spin" aria-hidden /> : null}
          {busy ? "Checking…" : "Check now"}
        </button>
      </div>
      {msg ? <p className="st-muted" role="status">{msg}</p> : null}
      <p className="st-muted">Each update is signed, and Khipu checks the signature before it replaces anything. It installs on this Mac only.</p>
      {whatsNew}
    </>
  );
}
