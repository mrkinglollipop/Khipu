// The rows in Memory's list: search results (the matched line, highlighted)
// and recent conversations (grouped by day). Both are buttons in a list the
// arrow keys move through; the open one carries aria-current.
import { highlight, metaLabel } from "./model";
import type { DayGroup, MemoryRow } from "./model";

export type Searched = { query: string; mine: boolean; exact: boolean };

/** `value` with the parts that match the search in <mark>. */
export function Marked({ value, searched }: { value: string; searched: Searched | null }) {
  if (!searched) return <>{value}</>;
  return (
    <>
      {highlight(value, searched.query, searched.exact).map((p, i) =>
        p.hit ? <mark key={i}>{p.text}</mark> : <span key={i}>{p.text}</span>,
      )}
    </>
  );
}

export function ResultList({
  rows,
  searched,
  selectedId,
  onOpen,
}: {
  rows: MemoryRow[];
  searched: Searched;
  selectedId: number | null;
  onOpen: (row: MemoryRow) => void;
}) {
  return (
    <>
      {rows.map((row) => (
        <button
          key={row.id}
          type="button"
          className="sh-row mem-hit"
          aria-current={selectedId === row.id}
          onClick={() => onOpen(row)}
        >
          <span className="mem-meta">{metaLabel(row, { youSaid: searched.mine })}</span>
          <span className="mem-title">{row.title}</span>
          {row.quote ? (
            <span className="mem-quote">
              “<Marked value={row.quote} searched={searched} />”
            </span>
          ) : null}
        </button>
      ))}
    </>
  );
}

export function RecentList({
  groups,
  selectedId,
  onOpen,
}: {
  groups: DayGroup[];
  selectedId: number | null;
  onOpen: (row: MemoryRow) => void;
}) {
  return (
    <>
      {groups.map((g) => (
        <div key={g.label} role="group" aria-label={g.label}>
          <h3 className="mem-day">{g.label}</h3>
          {g.rows.map((row) => (
            <button
              key={row.id}
              type="button"
              className="sh-row mem-hit"
              aria-current={selectedId === row.id}
              onClick={() => onOpen(row)}
            >
              <span className="mem-meta">{metaLabel({ ...row, ts: null }) || "Conversation"}</span>
              <span className="mem-title">{row.title}</span>
            </button>
          ))}
        </div>
      ))}
    </>
  );
}
