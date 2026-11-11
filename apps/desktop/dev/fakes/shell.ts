// What the whole window needs to open in the dev preview (slice A1): setup
// done or not, a database that answers, and two open questions so Today's
// count shows. Fictional. Switches (set on window.__khipuFake by a dev state):
//   welcome: true  the first-run Welcome flow (no database configured)
//   quiet: true    nothing needs you (no count on Today)
import { raw } from "./raw";
import type { FakeHandler } from "./index";

const ago = (min: number) => new Date(Date.now() - min * 60000).toISOString();

const NEEDS_YOU = [
  { id: 1, text: "Decide whether the Khipu app mocks conversation may run ahead on Claude until 6 pm.", project: "khipu", owner: "user", kind: "ask", status: "open", opened_at: ago(12), priority: 2 },
  { id: 2, text: "Delete 412 leftover summary drafts to free 3.1 GB.", project: "khipu", owner: "user", kind: "ask", status: "open", opened_at: ago(60 * 20), priority: 1 },
];

export const shellFakes: FakeHandler = (cmd, args, flags) => {
  switch (cmd) {
    case "dsn_configured":
      return raw(!flags.welcome);
    case "khipu_db_status":
      return { ok: true };
    case "run_khipu": {
      const a = (args?.args as string[] | undefined) ?? [];
      if (a[0] === "owed") {
        const open = a.includes("--status=open") && !flags.quiet;
        const owner = a.find((x) => x.startsWith("--owner="))?.slice(8);
        const rows = open ? NEEDS_YOU.filter((r) => owner === "all" || owner === r.owner) : [];
        return { results: rows, total: rows.length, next_cursor: null };
      }
      return undefined;
    }
    default:
      return undefined;
  }
};
