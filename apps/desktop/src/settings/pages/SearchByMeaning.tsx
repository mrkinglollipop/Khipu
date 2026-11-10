import { useCliState } from "../../settingsCli";
import type { ProfileRow } from "../../EmbeddingsPanel";

/** Read-only. The old Embeddings screen has no on/off control: it only picks
 *  which model is active and how much is covered. So this page says what is true
 *  and has no switch that would do nothing. */
export function SearchByMeaningPage({ active }: { active: boolean }) {
  const st = useCliState<{ profiles: ProfileRow[] }>("khipu_embed_profiles_list", active);
  if (st.loading && !st.data) return <p className="st-muted" role="status">Reading…</p>;
  if (!st.data) return <p className="st-muted" role="status">Khipu could not read this just now{st.error ? `: ${st.error}` : "."}</p>;
  const on = st.data.profiles.find((p) => p.is_active);
  if (!on) return <p>Search by meaning is off: no search model is set up yet.</p>;
  const mem = on.coverage?.memory;
  return (
    <>
      <p>Search by meaning is on. It finds a conversation by what it was about, not only by the exact words.</p>
      <div className="st-kvs">
        <div className="st-kv"><div className="st-k">Model</div><div className="st-v">{on.model}</div></div>
        {mem ? (
          <div className="st-kv">
            <div className="st-k">Covered</div>
            <div className="st-v">{Math.round(mem.pct)}% of your memory ({mem.missing.toLocaleString()} still to do)</div>
          </div>
        ) : null}
      </div>
    </>
  );
}
