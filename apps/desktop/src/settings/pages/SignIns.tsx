import { Tag } from "../../ui";
import { SignInControl } from "../../PlansCard";
import { useAccountsSupport } from "../../accountsApi";
import type { Loaded, MacRow, MacsDoc } from "../data";
import { macName } from "../data";
import { fmtAge } from "../../time";

/** Per Mac: each account and whether it is signed in. This Mac's accounts that
 *  need it get the same sign-in button the account cards use. The other Mac's
 *  list is read-only, and says how old it is. */
export function SignInsPage({ macs, active }: { macs: Loaded<MacsDoc>; active: boolean }) {
  const { support, reload } = useAccountsSupport(active);
  if (macs.loading && !macs.data) return <p className="st-muted" role="status">Reading sign-ins…</p>;
  if (!macs.data) return <p className="st-muted" role="status">Khipu could not read the sign-ins just now{macs.error ? `: ${macs.error}` : "."}</p>;
  const homes = new Map<string, string | null>();
  if (support.state === "here") {
    for (const a of support.status.accounts) homes.set(`${a.harness}|${a.label}`, a.home ?? null);
  }
  const block = (m: MacRow) => (
    <section key={m.mac} className="st-group" aria-label={`Sign-ins on ${macName(m)}`}>
      <h3>
        {macName(m)}
        {m.stale ? <Tag tone="warn">Not heard from lately</Tag> : null}
      </h3>
      {m.stale ? (
        <p className="st-muted">
          This is how it looked {m.age_s == null ? "a while ago" : `${fmtAge(m.age_s)} ago`}. It may have changed.
        </p>
      ) : null}
      {!m.signins || m.signins.length === 0 ? (
        <p className="st-muted">No sign-ins reported yet.</p>
      ) : (
        <div className="st-kvs">
          {m.signins.map((s) => {
            const key = `${s.harness}|${s.label}`;
            const canFix = m.this_mac && !s.signed_in && homes.has(key);
            return (
              <div className="st-kv" key={key}>
                <div className="st-k">{s.label}</div>
                <div className="st-v">
                  {s.signed_in ? "Signed in" : <strong>Needs signing in</strong>}
                  {canFix ? (
                    <span className="st-inline">
                      <SignInControl
                        row={{ harness: s.harness, account: s.label, home: homes.get(key) ?? null, ok: false, error: "not_signed_in" }}
                        onSignedIn={reload}
                      />
                    </span>
                  ) : null}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
  const others = macs.data.macs.filter((m) => !m.this_mac);
  return (
    <>
      {macs.data.macs.map(block)}
      {others.length === 0 ? <p className="st-muted">No other Mac has reported yet, so only this Mac is shown.</p> : null}
    </>
  );
}
