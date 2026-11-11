// Project work lists for the dev preview (slice A8). Shaped as the CLI
// prints them: `khipu_ledger_projects`, `khipu_ledger_list`, `khipu_ledger_set`.
// Fictional. A change made in the preview is kept until the page reloads.
// Switch: window.__khipuFake.noProjects = true  (no project has a list)
import type { FakeHandler } from "./index";

const day = (n: number) => new Date(Date.now() - n * 86400000).toISOString();
const min = (n: number) => new Date(Date.now() - n * 60000).toISOString();

const VOCAB = {
  state: [
    ["NOT STARTED", "open"],
    ["IN FLIGHT", "open"],
    ["NEEDS OWNER", "needs"],
    ["DONE", "done"],
    ["VERIFIED", "done"],
    ["DROPPED", "dropped"],
  ],
  kind: ["goal", "slice", "loose-end"],
};

type Row = {
  id: string; title: string; kind: string; parent: string; state: string; evidence: string;
  owner: string; updated: string; link: string; refs: Record<string, unknown>; project: string;
  history: Array<{ ts: string; by: string; from?: string; to?: string }>; version: number;
};
const row = (id: string, title: string, state: string, owner: string, updated: string): Row => ({
  id, title, kind: "goal", parent: "", state, evidence: "", owner, updated, link: "", refs: {},
  project: "khipu", history: [], version: 1,
});

const KHIPU: Row[] = [
  row("g1", "Get M1's held conversations into memory", "IN FLIGHT", "Codex", day(4)),
  row("g2", "Make the needs-you list quiet", "IN FLIGHT", "", day(1)),
  row("g3", "One way to update both Macs", "NOT STARTED", "", day(3)),
  row("g4", "New app: these mocks", "NEEDS OWNER", "you", day(2)),
  row("d1", "Old list reset to real questions", "DONE", "", day(5)),
  row("d2", "Rules kept in one file", "DONE", "", day(5)),
  row("d3", "Nightly tidy-up under 15 minutes", "VERIFIED", "", day(6)),
  row("d4", "Search finds the words you typed", "DONE", "", day(12)),
  row("d5", "Backups checked each night", "VERIFIED", "", day(20)),
  row("d6", "Old Home tiles removed", "DROPPED", "", day(30)),
];
KHIPU[0].history = [{ ts: min(40), by: "codex, from a job" }];

export const projectsFakes: FakeHandler = (cmd, args, flags) => {
  const list = flags.noProjects ? [] : KHIPU;
  switch (cmd) {
    case "khipu_ledger_projects":
      return {
        ok: true,
        projects: [
          { project: "khipu", name: "Khipu", hidden: false, rows: list.length, open: list.filter((r) => ["NOT STARTED", "IN FLIGHT", "NEEDS OWNER"].includes(r.state)).length, needs_owner: list.filter((r) => r.state === "NEEDS OWNER").length },
          { project: "church-slides", name: "Church slides", hidden: false, rows: 0, open: 0, needs_owner: 0 },
          { project: "recipe-app", name: "Recipe app", hidden: false, rows: 0, open: 0, needs_owner: 0 },
        ],
      };
    case "khipu_ledger_list":
      if (args?.project !== "khipu") return { ok: true, project: String(args?.project ?? ""), offline: false, vocab: VOCAB, rows: [] };
      return { ok: true, project: "khipu", offline: false, as_of: min(1), vocab: VOCAB, rows: list };
    case "khipu_ledger_set": {
      const change = (args?.change ?? {}) as { state?: string; if_state?: string };
      const r = KHIPU.find((x) => x.id === args?.id);
      if (!r) return { ok: false, error: "That line is no longer on the list." };
      if (change.if_state && change.if_state !== r.state) return { ok: true, late: true, state: change.state };
      if (change.state) {
        r.history.unshift({ ts: new Date().toISOString(), by: "you, in the app", from: r.state, to: change.state });
        r.state = change.state;
        r.updated = new Date().toISOString();
        r.version += 1;
      }
      return { ok: true, state: r.state };
    }
    default:
      return undefined;
  }
};
