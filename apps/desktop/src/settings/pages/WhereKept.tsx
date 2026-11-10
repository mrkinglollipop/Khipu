import { Tag } from "../../ui";
import type { SettingsCtx } from "../context";
import type { HealthState } from "../data";
import { CheckedLine } from "./CheckedLine";

/** Where the memory lives and whether it answers. The move flow is the old one
 *  (the same dialog the old Database page opens). */
export function WhereKeptPage({ ctx, health }: { ctx: SettingsCtx; health: HealthState }) {
  const area = health.data?.areas.find((a) => a.key === "database") ?? null;
  const tone = ctx.dsnOk === false || ctx.dbReachable === false ? "err" : ctx.dbReachable ? "ok" : "neutral";
  const word = ctx.dsnOk === false ? "Not set up" : ctx.dbReachable === false ? "Not reachable" : ctx.dbReachable ? "Connected" : "Not checked";
  return (
    <>
      <p><Tag tone={tone} dot>{word}</Tag></p>
      <p className="st-muted">Khipu keeps your memory in one database. Every Mac that uses the same connection reads and writes the same memory. The connection is stored in the login Keychain on this Mac, never in a file in the repo.</p>
      {area ? <p>{area.line}{area.khipu_doing ? ` ${area.khipu_doing}` : ""}</p> : null}
      <CheckedLine health={health} />
      <div className="st-actions">
        <button type="button" onClick={ctx.onMoveMemory}>Move this memory to another database…</button>
      </div>
    </>
  );
}
