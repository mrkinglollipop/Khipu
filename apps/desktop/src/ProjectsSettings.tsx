/* Settings -> Projects: projects that have more than one name, the "same
   project?" question, and the pairs the owner kept separate (mock:
   docs/mockups/2026-10-19-repo-renames/, plan: docs/plans/2026-10-19-repo-renames.md,
   slice D).

   Every change is one fixed-argv Tauri command that runs `khipu project ...
   --from-app`; the section then re-reads `khipu project list --json`, so what
   it shows is what the CLI reports. Undo of an older name removes that name;
   Undo of a kept-separate answer is the owner-actions Undo of the line that
   recorded it. Reading never runs the GitHub check. */

import { useEffect, useId, useState } from "react";
import { Loader2, MessageSquare, HelpCircle } from "lucide-react";
import { Tag } from "./ui";
import { callCli } from "./settingsCli";
import type { OwnerAction } from "./OwnerActions";
import "./Projects.css";

export type ProjectName = {
  name: string;
  role: "canonical" | "alias";
  source?: string | null;
  created_at?: string | null;
  open_commitments: number;
  standing_decisions: number;
  episodes: number;
};

export type ProjectGroup = {
  canonical: string;
  names: ProjectName[];
  totals: { open_commitments: number; standing_decisions: number; episodes: number };
};

export type ProjectPair = {
  a: string;
  b: string;
  state: "asked" | "merged" | "kept_separate" | string;
  created_at?: string | null;
  decided_at?: string | null;
};

export type ProjectsList = {
  groups: ProjectGroup[];
  pairs: ProjectPair[];
  pair_counts?: Record<string, { open_commitments: number; episodes: number; last_used?: string | null }>;
  sweep?: { at?: string | null; result?: string | null; reason?: string | null };
};

export type ProjectsState = {
  data: ProjectsList | null;
  error: string | null;
  loading: boolean;
  reload: () => Promise<ProjectsList | null>;
};

/** Pairs waiting on the owner: the count beside Projects in the section list. */
export function askedCount(data: ProjectsList | null | undefined): number {
  return (data?.pairs ?? []).filter((p) => p.state === "asked").length;
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

const SLUG = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;

function sameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

/** "Today, 1:40 PM" for today, "Oct 12" for any other day. */
export function dayWords(iso: string | null | undefined, now = new Date()): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return sameDay(d, now) ? `Today, ${time}` : d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function atWords(iso: string | null | undefined, now = new Date()): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return sameDay(d, now) ? `today at ${time}` : `${d.toLocaleDateString([], { month: "short", day: "numeric" })} at ${time}`;
}

const TAG: Record<string, string> = { github: "GitHub", checkout: "Same checkout", owner: "You" };

/** How Khipu knew, in one plain sentence. */
export function whySentence(n: ProjectName, canonical: string): string {
  const saved = n.episodes > 0 ? ` ${plural(n.episodes, "conversation was", "conversations were")} saved under the older name.` : "";
  switch (n.source) {
    case "github":
      return `GitHub says this repo is now ${canonical}.${saved}`;
    case "checkout":
      return `The same checkout on this Mac was renamed from ${n.name} to ${canonical}. Same files, same history.${saved}`;
    case "owner":
      return `You said ${n.name} and ${canonical} are the same project.${saved}`;
    default:
      return `Khipu reads ${n.name} as the same project as ${canonical}.${saved}`;
  }
}

function sweepWords(s: ProjectsList["sweep"]): string {
  if (!s || !s.result) return "GitHub has not been checked yet";
  const at = atWords(s.at);
  if (s.result === "ok") return `GitHub checked ${at}`;
  if (s.result === "skipped") return `GitHub was not checked ${at}${s.reason ? `: ${s.reason}` : ""}`;
  return `The last GitHub check failed ${at}${s.reason ? `: ${s.reason}` : ""}`;
}

function newest(g: ProjectGroup): string {
  return g.names.map((n) => n.created_at ?? "").sort().pop() ?? "";
}

export function ProjectsSettings({ active, state }: { active: boolean; state: ProjectsState }) {
  const { data, error, loading, reload } = state;
  const [busy, setBusy] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [adding, setAdding] = useState(false);
  const [older, setOlder] = useState("");
  const [current, setCurrent] = useState("");
  const [showAs, setShowAs] = useState<Record<string, string>>({});
  const base = useId();

  useEffect(() => {
    if (active) void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);

  async function act(id: string, run: () => Promise<{ ok: true } | { ok: false; error: string }>): Promise<boolean> {
    setBusy(id);
    const res = await run();
    if (res.ok) await reload();
    setErrors((prev) => {
      const out = { ...prev };
      if (res.ok) delete out[id];
      else out[id] = res.error;
      return out;
    });
    setBusy(null);
    return res.ok;
  }

  const groups = [...(data?.groups ?? [])].sort((x, y) => newest(y).localeCompare(newest(x)));
  const asked = (data?.pairs ?? []).filter((p) => p.state === "asked");
  const kept = (data?.pairs ?? []).filter((p) => p.state === "kept_separate");
  const counts = data?.pair_counts ?? {};

  async function undoKept(p: ProjectPair) {
    await act(`kept:${p.a}|${p.b}`, async () => {
      const log = await callCli<{ actions: OwnerAction[] }>("khipu_owner_direction_actions");
      if (!log.ok) return { ok: false, error: `Could not read the record of your answer: ${log.error}` };
      const line = log.data.actions.find(
        (a) =>
          a.action === "project.keep_separate" &&
          a.target === `${p.a}|${p.b}` &&
          !a.no_undo &&
          (a.state === "applied" || a.state == null),
      );
      if (!line) {
        return {
          ok: false,
          error: "Khipu has no undoable record of that answer, so it was left as it is. Ask any conversation to undo it.",
        };
      }
      const res = await callCli("khipu_owner_direction_undo", { id: line.id });
      return res.ok ? { ok: true } : { ok: false, error: res.error };
    });
  }

  function question(p: ProjectPair) {
    const key = `${p.a}|${p.b}`;
    const sides = [p.a, p.b];
    const lastUsed = (n: string) => counts[n]?.last_used ?? "";
    const dflt = lastUsed(p.b) > lastUsed(p.a) ? p.b : p.a;
    const pick = showAs[key] ?? dflt;
    const pickId = `${base}-show-${key}`;
    return (
      <section className="ask" aria-labelledby={`${base}-ask-${key}`} key={key}>
        <div className="ask-head">
          <HelpCircle size={16} aria-hidden />
          <b id={`${base}-ask-${key}`}>These look like the same project</b>
          <span className="meta">Asked {dayWords(p.created_at).replace(/^Today, /, "today, ")}</span>
        </div>
        <div className="ask-body">
          <div className="pair">
            {sides.map((n) => (
              <div className="pair-side" key={n}>
                <span className="proj-name">{n}</span>
                <span>
                  {plural(counts[n]?.episodes ?? 0, "conversation")} · {plural(counts[n]?.open_commitments ?? 0, "open commitment")}
                </span>
                {lastUsed(n) ? <span>Last used {dayWords(lastUsed(n)).replace(/^Today, .*/, "today")}</span> : null}
              </div>
            ))}
          </div>
          <p>
            Both start from the same first commit. That happens when a repo is renamed, and also when one is a copy (a
            fork) of the other. GitHub doesn&apos;t say either one was renamed, so Khipu won&apos;t decide this for you.
            Until you answer, they stay two projects.
          </p>
          <div className="choices">
            <div className="choice">
              <button
                type="button"
                className="sm"
                disabled={busy !== null}
                onClick={() =>
                  void act(`merge:${key}`, async () => {
                    const res = await callCli("khipu_project_merge", { a: p.a, b: p.b, showAs: pick });
                    return res.ok ? { ok: true } : { ok: false, error: res.error };
                  })
                }
              >
                {busy === `merge:${key}` ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                Merge
              </button>
              <p className="muted">
                Search, commitments, decisions and the summary at the start of each session show both as one project.
                Conversations keep the name they were saved with.
              </p>
              <div className="pick">
                <label className="label-sm" htmlFor={pickId}>
                  Show it as
                </label>
                <select id={pickId} value={pick} onChange={(e) => setShowAs((m) => ({ ...m, [key]: e.target.value }))}>
                  {sides.map((n) => (
                    <option key={n} value={n}>
                      {n}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            <div className="choice">
              <button
                type="button"
                className="sm"
                disabled={busy !== null}
                onClick={() =>
                  void act(`keep:${key}`, async () => {
                    const res = await callCli("khipu_project_keep_separate", { a: p.a, b: p.b });
                    return res.ok ? { ok: true } : { ok: false, error: res.error };
                  })
                }
              >
                {busy === `keep:${key}` ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                Keep separate
              </button>
              <p className="muted">
                They stay two projects, and Khipu stops asking about this pair. It moves to Kept separate below.
              </p>
            </div>
          </div>
          {errors[`merge:${key}`] || errors[`keep:${key}`] ? (
            <p className="set-error" role="alert">
              {errors[`merge:${key}`] ?? errors[`keep:${key}`]}
            </p>
          ) : null}
          <p className="muted">Either answer can be undone from this page. Nothing is deleted or rewritten either way.</p>
          <div className="chat-hint">
            <MessageSquare size={14} aria-hidden />
            <span>
              You can also answer in any conversation. Your agent sees this question when a session starts in either
              project, and asks you; <q>merge them</q> or <q>keep them separate</q> is enough.
            </span>
          </div>
        </div>
      </section>
    );
  }

  const nothingYet = !!data && groups.length === 0 && kept.length === 0 && asked.length === 0;

  return (
    <div className="projects-stack">
      {error && !data ? (
        <p className="set-error" role="alert">
          Could not read your projects: {error}
          <button type="button" className="sm" onClick={() => void reload()} style={{ marginLeft: 8 }}>
            Try again
          </button>
        </p>
      ) : null}
      {error && data ? (
        <p className="set-error" role="alert">
          Could not refresh your projects: {error}
        </p>
      ) : null}
      {!data && !error && loading ? (
        <p className="muted" role="status">
          <Loader2 size={14} className="spin" aria-hidden /> Reading your projects…
        </p>
      ) : null}

      {data ? asked.map(question) : null}

      {data ? (
        <div className="section-card" aria-labelledby={`${base}-names`}>
          <div className="section-head" id={`${base}-names`}>
            Projects with more than one name
          </div>
          <div className="section-body">
            <p className="muted">
              When a repo or folder is renamed, Khipu keeps its old and new conversations together as one project under
              the newest name. Each conversation keeps the name it was saved with; nothing is rewritten, so Undo puts
              everything back exactly as it was.
            </p>
            {groups.length === 0 ? (
              <p className="muted">
                {nothingYet
                  ? "No project has more than one name."
                  : "No project has more than one name yet. A question above, once you answer it, can join two."}
              </p>
            ) : null}
            {groups.map((g) => {
              const aliases = g.names.filter((n) => n.role === "alias");
              return (
                <div className="proj" role="group" aria-label={g.canonical} key={g.canonical}>
                  <div className="proj-head">
                    <span className="proj-name">{g.canonical}</span>
                    <Tag tone="ok">Current name</Tag>
                    <span className="meta">
                      {plural(g.totals.episodes, "conversation")} · {plural(g.totals.open_commitments, "open commitment")}
                      {" · "}
                      {plural(g.totals.standing_decisions, "decision")}, {g.names.length === 2 ? "both" : "all"} names together
                      {SLUG.test(g.canonical) ? "" : " · folder, no GitHub"}
                    </span>
                  </div>
                  {aliases.map((n) => {
                    const id = `alias:${n.name}`;
                    return (
                      <div className="oldname" key={n.name}>
                        <div className="oldname-id">
                          <div className="oldname-line">
                            <span className="label-sm">Older name</span>
                            <span className="oldname-name">{n.name}</span>
                            <Tag>{TAG[n.source ?? ""] ?? "Other"}</Tag>
                          </div>
                          <span className="oldname-why">{whySentence(n, g.canonical)}</span>
                        </div>
                        <div className="oldname-acts">
                          <span className="meta">{dayWords(n.created_at)}</span>
                          <button
                            type="button"
                            className="sm"
                            aria-label={`Undo: ${n.name} is no longer an older name of ${g.canonical}`}
                            disabled={busy !== null}
                            onClick={() =>
                              void act(id, async () => {
                                const res = await callCli("khipu_project_name_remove", { older: n.name });
                                return res.ok ? { ok: true } : { ok: false, error: res.error };
                              })
                            }
                          >
                            {busy === id ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                            Undo
                          </button>
                        </div>
                        {errors[id] ? (
                          <p className="set-error proj-row-error" role="alert">
                            {errors[id]}
                          </p>
                        ) : null}
                      </div>
                    );
                  })}
                </div>
              );
            })}

            {adding ? (
              <form
                className="proj-add"
                onSubmit={(e) => {
                  e.preventDefault();
                  void act("add", async () => {
                    const res = await callCli("khipu_project_name_add", { older: older.trim(), current: current.trim() });
                    if (res.ok) {
                      setAdding(false);
                      setOlder("");
                      setCurrent("");
                      return { ok: true };
                    }
                    return { ok: false, error: res.error };
                  });
                }}
              >
                <label>
                  <span className="label-sm">Older name</span>
                  <input className="mono" value={older} autoComplete="off" onChange={(e) => setOlder(e.target.value)} />
                </label>
                <label>
                  <span className="label-sm">Current name</span>
                  <input className="mono" value={current} autoComplete="off" onChange={(e) => setCurrent(e.target.value)} />
                </label>
                <button type="submit" className="sm" disabled={busy !== null || !older.trim() || !current.trim()}>
                  {busy === "add" ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                  Add
                </button>
                <button type="button" className="sm" onClick={() => setAdding(false)}>
                  Cancel
                </button>
              </form>
            ) : null}
            {errors.add ? (
              <p className="set-error" role="alert">
                {errors.add}
              </p>
            ) : null}
            <div className="inline">
              {adding ? null : (
                <button type="button" className="sm" onClick={() => setAdding(true)}>
                  Add another name…
                </button>
              )}
              <span className="meta push">{sweepWords(data.sweep)}</span>
            </div>
          </div>
        </div>
      ) : null}

      {data && kept.length > 0 ? (
        <div className="section-card" aria-labelledby={`${base}-kept`}>
          <div className="section-head" id={`${base}-kept`}>
            Kept separate
          </div>
          <div className="section-body">
            <div className="kept-list">
              {kept.map((p) => {
                const id = `kept:${p.a}|${p.b}`;
                return (
                  <div className="oldname" key={id}>
                    <div className="oldname-id">
                      <div className="oldname-line">
                        <span className="oldname-name">{p.a}</span>
                        <span className="label-sm">and</span>
                        <span className="oldname-name">{p.b}</span>
                      </div>
                      <span className="oldname-why">
                        <Tag>You</Tag> They start from the same first commit, and you said they are different projects.
                        Khipu won&apos;t ask about this pair again.
                      </span>
                    </div>
                    <div className="oldname-acts">
                      <span className="meta">{dayWords(p.decided_at)}</span>
                      <button
                        type="button"
                        className="sm"
                        aria-label={`Undo: ${p.a} and ${p.b} are no longer kept separate`}
                        disabled={busy !== null}
                        onClick={() => void undoKept(p)}
                      >
                        {busy === id ? <Loader2 size={14} className="spin" aria-hidden /> : null}
                        Undo
                      </button>
                    </div>
                    {errors[id] ? (
                      <p className="set-error proj-row-error" role="alert">
                        {errors[id]}
                      </p>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      ) : null}

      {data ? (
        <div className="chat-hint">
          <MessageSquare size={14} aria-hidden />
          <span>
            You don&apos;t need this page to check or change any of this. In any conversation, ask{" "}
            <q>is olivewood one project now?</q> and your agent shows the same list. Say{" "}
            <q>undo the church-harness rename</q> and it is undone; Owner actions keeps a record of it.
          </span>
        </div>
      ) : null}
    </div>
  );
}
