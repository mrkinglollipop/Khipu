import { openUrl } from "@tauri-apps/plugin-opener";
import type { SettingsCtx } from "../context";

/** Khipu's website. */
export const HELP_URL = "https://kinglollipop.com/khipu";

export function HelpPage({ ctx }: { ctx: Pick<SettingsCtx, "onOpenGuides" | "onFeedback"> }) {
  return (
    <>
      <p>Help is on the website.</p>
      <div className="st-actions">
        <button type="button" className="primary" onClick={() => void openUrl(HELP_URL)}>Open the website</button>
        <button type="button" onClick={ctx.onOpenGuides}>Guides in this app</button>
        <button type="button" onClick={ctx.onFeedback}>Send feedback</button>
      </div>
    </>
  );
}
