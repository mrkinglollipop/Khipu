// Memory: search what you and your agents said, read a conversation beside the
// list, copy a link for an agent, or forget it. The list is on the left (the
// search box, "Only my words", "Exact words", the Try links, then results, or
// recent conversations by day when the box is empty); the open conversation is
// on the right. Under 900 px it is one column: the list, then the conversation
// with a Back button.
//
// The toolbar switch has two views: Search (above) and Project work lists
// (`ProjectLists`, which draws its own list and detail).
import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Loader2, Search } from "lucide-react";
import { Body, Split, Toolbar, ViewSwitch, useListKeys } from "../shell";
import { ConversationPage } from "./Conversation";
import { CorrectSummary } from "./CorrectSummary";
import { PendingForgets } from "../PendingForgets";
import { ProjectLists } from "./ProjectLists";
import { ForgetDialog } from "./ForgetDialog";
import { forgetConversation } from "./forget";
import { RecentList, ResultList } from "./Results";
import { conversationOf, groupByDay, readSearch, rowsOf } from "./model";
import type { Conversation, MemoryRow, Run } from "./model";
import "./memory.css";

type MemoryView = "search" | "projects";
const VIEWS: { id: MemoryView; label: string }[] = [
  { id: "search", label: "Search" },
  { id: "projects", label: "Project work lists" },
];

const TRY = ["room for Grok Bot", "update together"];

type Searched = { query: string; mine: boolean; exact: boolean };
type Found =
  | { state: "none" }
  | { state: "busy" }
  | { state: "rows"; rows: MemoryRow[]; searched: Searched }
  | { state: "error"; message: string; needsHub: boolean; searched: Searched };

export function Memory({
  run,
  onForgotten,
  onOwedClose,
}: {
  run: Run;
  /** Called after a conversation was forgotten, so other screens drop it too. */
  onForgotten?: (id: number) => void;
  /** Called when a work-list line that finishes an owed item is marked done. */
  onOwedClose?: (id: number) => Promise<void> | void;
}) {
  const [view, setView] = useState<MemoryView>("search");
  const [query, setQuery] = useState("");
  const [mine, setMine] = useState(true);
  const [exact, setExact] = useState(false);
  const [found, setFound] = useState<Found>({ state: "none" });
  const [recent, setRecent] = useState<{ state: "busy" | "done" | "error"; rows: MemoryRow[] }>({ state: "busy", rows: [] });
  const [selected, setSelected] = useState<MemoryRow | null>(null);
  const [page, setPage] = useState<{ state: "busy" | "done" | "error"; data: Conversation | null }>({ state: "done", data: null });
  const [forgetting, setForgetting] = useState<MemoryRow | null>(null);
  const [forgetErr, setForgetErr] = useState<string | null>(null);
  const [forgetBusy, setForgetBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const searchSeq = useRef(0);
  const pageSeq = useRef(0);
  const keys = useListKeys();

  const loadRecent = useCallback(async () => {
    try {
      const raw = await run(["activity", "--limit", "40"]);
      const parsed = JSON.parse(raw) as { recent?: unknown };
      setRecent({ state: "done", rows: rowsOf(parsed.recent) });
    } catch {
      setRecent({ state: "error", rows: [] });
    }
  }, [run]);

  useEffect(() => {
    void loadRecent();
  }, [loadRecent]);

  const search = useCallback(
    async (next?: { query?: string; mine?: boolean; exact?: boolean }) => {
      const q = (next?.query ?? query).trim();
      const m = next?.mine ?? mine;
      const x = next?.exact ?? exact;
      const seq = ++searchSeq.current;
      if (!q) {
        setFound({ state: "none" });
        return;
      }
      const searched: Searched = { query: q, mine: m, exact: x };
      setFound({ state: "busy" });
      setSelected(null);
      try {
        // "Only my words" is passed only while it is on. A value starting with
        // a dash is read as a flag unless it comes after "--".
        const args = ["search", "--mode", x ? "literal" : "hybrid", "--limit", "20"];
        if (m) args.push("--owner-words");
        args.push("--", q);
        const out = readSearch(await run(args));
        if (seq !== searchSeq.current) return;
        setFound(
          out.kind === "rows"
            ? { state: "rows", rows: out.rows, searched }
            : { state: "error", message: out.message, needsHub: out.needsHub, searched },
        );
      } catch (e) {
        if (seq !== searchSeq.current) return;
        setFound({ state: "error", message: String(e), needsHub: false, searched });
      }
    },
    [query, mine, exact, run],
  );

  const open = useCallback(
    async (row: MemoryRow) => {
      const seq = ++pageSeq.current;
      setSelected(row);
      setNotice(null);
      setPage({ state: "busy", data: null });
      try {
        const data = conversationOf(JSON.parse(await run(["activity", `--show=${row.id}`])));
        if (seq !== pageSeq.current) return;
        setPage(data ? { state: "done", data } : { state: "error", data: null });
      } catch {
        if (seq !== pageSeq.current) return;
        setPage({ state: "error", data: null });
      }
    },
    [run],
  );

  const submit = (e: FormEvent) => {
    e.preventDefault();
    void search();
  };

  const toggle = (which: "mine" | "exact") => {
    const next = which === "mine" ? { mine: !mine } : { exact: !exact };
    if (which === "mine") setMine(!mine);
    else setExact(!exact);
    if (query.trim()) void search(next);
  };

  const tryIt = (q: string) => {
    setQuery(q);
    void search({ query: q });
  };

  const confirmForget = async () => {
    if (!forgetting) return;
    const row = forgetting;
    setForgetBusy(true);
    setForgetErr(null);
    const result = await forgetConversation(run, row.id);
    setForgetBusy(false);
    if (!result.ok) {
      setForgetErr(result.error);
      return;
    }
    setForgetting(null);
    pageSeq.current++;
    setSelected(null);
    setPage({ state: "done", data: null });
    setRecent((r) => ({ ...r, rows: r.rows.filter((x) => x.id !== row.id) }));
    setFound((f) => (f.state === "rows" ? { ...f, rows: f.rows.filter((x) => x.id !== row.id) } : f));
    setNotice(result.warning ?? `Forgot “${row.title}”.`);
    onForgotten?.(row.id);
  };

  const searching = found.state !== "none";
  const searched = found.state === "rows" || found.state === "error" ? found.searched : null;

  const list = (
    <>
      <form className="mem-search" role="search" onSubmit={submit}>
        <Search size={18} aria-hidden />
        <input
          type="search"
          aria-label="Search what you and your agents said"
          placeholder="Search what you and your agents said"
          value={query}
          autoComplete="off"
          onChange={(e) => {
            setQuery(e.target.value);
            // Clearing the box (the field's own × too) goes back to recent
            // conversations instead of leaving old results under an empty box.
            if (!e.target.value.trim()) {
              searchSeq.current++;
              setFound({ state: "none" });
            }
          }}
        />
        {found.state === "busy" ? <Loader2 size={16} className="spin mem-busy" aria-label="Searching" role="status" /> : null}
      </form>
      {notice ? <p className="mem-notice" role="status">{notice}</p> : null}
      <div className="mem-opts" role="group" aria-label="Search options">
        <button type="button" className="mem-tog" aria-pressed={mine} onClick={() => toggle("mine")}>
          Only my words
        </button>
        <button type="button" className="mem-tog" aria-pressed={exact} onClick={() => toggle("exact")}>
          Exact words
        </button>
      </div>
      {!query.trim() ? (
        <p className="mem-try">
          Try:{" "}
          {TRY.map((t, i) => (
            <span key={t}>
              {i > 0 ? " · " : null}
              <button type="button" className="mem-link" onClick={() => tryIt(t)}>
                {t}
              </button>
            </span>
          ))}
        </p>
      ) : null}

      {searching ? (
        <section className="mem-results" aria-label="Search results" aria-busy={found.state === "busy"}>
          {found.state === "rows" && found.rows.length > 0 ? (
            <>
              <p className="mem-count" role="status">
                {found.rows.length === 1 ? "1 conversation" : `${found.rows.length} conversations`}
              </p>
              <div {...keys}>
                <ResultList rows={found.rows} searched={found.searched} selectedId={selected?.id ?? null} onOpen={(r) => void open(r)} />
              </div>
            </>
          ) : null}
          {found.state === "rows" && found.rows.length === 0 ? (
            <div className="mem-empty" role="status">
              <h2>Nothing matched “{found.searched.query}”.</h2>
              <p>
                {found.searched.mine
                  ? "Try fewer words, or turn off Only my words to search what agents said too."
                  : "Try fewer words, or turn off Exact words."}
              </p>
            </div>
          ) : null}
          {found.state === "error" ? (
            <div className="mem-empty" role="alert">
              <h2>{found.needsHub ? "Searching only your words isn’t available right now." : "Search didn’t finish."}</h2>
              <p>
                {found.needsHub
                  ? "Khipu can’t reach your saved memory, and this kind of search needs it. Nothing was searched, so this is not “no match”."
                  : found.message}
              </p>
              {found.needsHub && found.searched.mine ? (
                <button type="button" onClick={() => { setMine(false); void search({ mine: false }); }}>
                  Search everything instead
                </button>
              ) : (
                <button type="button" onClick={() => void search()}>
                  Try again
                </button>
              )}
            </div>
          ) : null}
        </section>
      ) : (
        <section className="mem-recent" aria-label="Recent conversations">
          <PendingForgets run={run} />
          <h2>Recent conversations</h2>
          {recent.state === "busy" ? <p className="mem-note" role="status">Loading…</p> : null}
          {recent.state === "error" ? (
            <p className="mem-note" role="alert">
              Couldn’t load recent conversations. <button type="button" className="mem-link" onClick={() => void loadRecent()}>Try again</button>
            </p>
          ) : null}
          {recent.state === "done" && recent.rows.length === 0 ? <p className="mem-note">Nothing saved yet.</p> : null}
          <div {...keys}>
            <RecentList groups={groupByDay(recent.rows)} selectedId={selected?.id ?? null} onOpen={(r) => void open(r)} />
          </div>
        </section>
      )}
    </>
  );

  const detail = selected ? (
    <ConversationPage
      row={selected}
      page={page}
      searched={searched}
      onBack={() => setSelected(null)}
      onForget={() => {
        setForgetErr(null);
        setForgetting(selected);
      }}
      correct={page.state === "done" ? (
        <CorrectSummary key={selected.id} id={selected.id} current={page.data?.about ?? ""} run={run}
          onSaved={() => void open(selected)} />
      ) : null}
    />
  ) : (
    <p className="mem-none">No conversation to show.</p>
  );

  return (
    <div className="mem">
      <Toolbar title="Memory">
        {VIEWS.length > 1 ? <ViewSwitch label="Memory view" options={VIEWS} value={view} onChange={setView} /> : null}
      </Toolbar>
      {view === "projects" ? (
        <ProjectLists onOwedClose={onOwedClose} />
      ) : (
        <Body>
          <Split
            list={list}
            listLabel="Search"
            detail={detail}
            detailLabel={selected ? selected.title : "No conversation open"}
            detailOpen={selected != null}
          />
        </Body>
      )}
      <ForgetDialog
        row={forgetting}
        busy={forgetBusy}
        error={forgetErr}
        onCancel={() => {
          if (!forgetBusy) setForgetting(null);
        }}
        onConfirm={() => void confirmForget()}
      />
    </div>
  );
}
