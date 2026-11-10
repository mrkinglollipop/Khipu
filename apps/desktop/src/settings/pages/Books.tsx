import { useState } from "react";
import { callCli, useCliState } from "../../settingsCli";
import type { LibraryRow } from "../../EmbeddingsPanel";

/** One switch over every book library. On: each library is enabled (the same
 *  `library enable` the old screen calls, with the library's own settings). Off:
 *  `library disable` for each. */
export function BooksPage({ active }: { active: boolean }) {
  const st = useCliState<{ libraries: LibraryRow[] }>("khipu_library_list", active);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const libs = st.data?.libraries ?? [];
  const all = libs.length > 0 && libs.every((l) => l.enabled);
  const some = libs.some((l) => l.enabled);

  async function set(want: boolean) {
    setBusy(true);
    setErr(null);
    const failed: string[] = [];
    for (const l of libs) {
      if (l.enabled === want) continue;
      const res = await callCli(want ? "khipu_library_enable" : "khipu_library_disable", { name: l.name });
      if (!res.ok) failed.push(`${l.name}: ${res.error}`);
    }
    if (failed.length) setErr(failed.join(" "));
    await st.reload();
    setBusy(false);
  }

  if (st.loading && !st.data) return <p className="st-muted" role="status">Reading…</p>;
  if (!st.data) return <p className="st-muted" role="status">Khipu could not read your books just now{st.error ? `: ${st.error}` : "."}</p>;
  if (libs.length === 0) return <p>No books have been added to Khipu, so there is nothing to include.</p>;
  return (
    <>
      <div className="st-switch">
        <input
          id="st-books"
          className="switch"
          type="checkbox"
          role="switch"
          checked={all}
          disabled={busy}
          onChange={(e) => void set(e.target.checked)}
        />
        <label htmlFor="st-books">Include your books in search</label>
      </div>
      <p className="st-muted">
        {all ? "On: searches look through your books too." : some ? "Some books are on and some are off. Turning this on includes all of them." : "Off: books are searched only when you ask."}
      </p>
      <p className="st-muted">{libs.length === 1 ? "1 book collection" : `${libs.length} book collections`}: {libs.map((l) => l.name).join(", ")}.</p>
      {err ? <p className="st-muted" role="alert">{err}</p> : null}
    </>
  );
}
