import { Ban, Check, Contrast, Minus, TriangleAlert, X } from "lucide-react";
import type { FunctionRow } from "./harnessFunctions";

/** Check-line marks. `ok`/`err`/`warn` are verdicts; `off` is "nothing to do
 *  here, by design"; `wait` is installed but not yet seen working (amber, not
 *  a fault); `na` is a limit of the harness itself. None of the last three may
 *  read as a failure. */
export type Mark = "ok" | "warn" | "err" | "off" | "wait" | "na";

export function CheckMark({ mark }: { mark: Mark }) {
  const cls = `ck ${mark}`;
  if (mark === "ok") return <Check size={14} className={cls} aria-hidden />;
  if (mark === "err") return <X size={14} className={cls} aria-hidden />;
  if (mark === "warn") return <TriangleAlert size={14} className={cls} aria-hidden />;
  if (mark === "wait") return <Contrast size={14} className={cls} aria-hidden />;
  if (mark === "na") return <Ban size={14} className={cls} aria-hidden />;
  return <Minus size={14} className={cls} aria-hidden />;
}

/** What a mark says, for the reader who cannot see it. */
export const MARK_WORD: Record<Mark, string> = {
  ok: "installed",
  warn: "partly installed",
  err: "not installed",
  off: "not checked",
  wait: "installed, waiting for proof",
  na: "not possible here",
};

/** One check line per function, with its proof tag. */
export function FunctionChecks({ rows }: { rows: FunctionRow[] }) {
  return (
    <>
      {rows.map((r) => (
        <div key={r.key}>
          <CheckMark mark={r.mark} />
          <span>
            {r.text}
            {r.tag ? <span className={`ftag ${r.tag.tone}`}>{r.tag.label}</span> : null}
          </span>
        </div>
      ))}
    </>
  );
}
