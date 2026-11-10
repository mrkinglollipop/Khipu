import { Tag } from "../../ui";
import type { Loaded, MacsDoc } from "../data";
import { macName } from "../data";
import { macLine } from "../words";

/** One line per Mac. A Mac that has gone quiet is shown as quiet, in words;
 *  a Mac that has not reported yet is said to be absent, not an error. */
export function YourMacsPage({ macs }: { macs: Loaded<MacsDoc> }) {
  if (macs.loading && !macs.data) return <p className="st-muted" role="status">Reading your Macs…</p>;
  if (!macs.data) {
    return (
      <p className="st-muted" role="status">
        Khipu could not read the list of Macs just now{macs.error ? `: ${macs.error}` : "."}
      </p>
    );
  }
  const doc = macs.data;
  const others = doc.macs.filter((m) => !m.this_mac);
  const versions = new Set(doc.macs.map((m) => m.version));
  return (
    <>
      <div className="st-kvs">
        {doc.macs.map((m) => (
          <div className="st-kv" key={m.mac}>
            <div className="st-k">
              {macName(m)}
              {m.this_mac ? <span className="st-sub"> · this window</span> : null}
              {m.stale ? <Tag tone="warn">Not heard from lately</Tag> : null}
            </div>
            <div className="st-v">{macLine(m)}</div>
          </div>
        ))}
      </div>
      {others.length === 0 ? (
        <p className="st-muted">{doc.note ?? "No other Mac has reported yet, so only this Mac is shown."}</p>
      ) : doc.macs.length > 1 && versions.size === 1 && !others.some((m) => m.stale) ? (
        <p className="st-muted">Both Macs run the same version.</p>
      ) : doc.macs.length > 1 && versions.size > 1 ? (
        <p className="st-muted">The Macs run different versions.</p>
      ) : null}
    </>
  );
}
