// The Accounts place's canned answers (slice A4), shaped as the CLI prints them
// and stateful, so a pause, a stop-at change, a run-ahead grant and each Undo
// play through. Fictional accounts and conversations. Nothing real is touched.
// Switches (set from a dev state in main.tsx):
//   accounts: true         turn these answers on
//   accountsProblem: true  Claude is out of room until the next reset; Claude second needs signing in
//   accountsAhead: true    one conversation is already running ahead on Claude
//   accountsRefuse: true   a pause is refused
//   accountsProblem also keeps one rule-set stop on Claude (A11: "More for this account" ends it)
import type { RouteAccount, RouteView } from "../../src/routing";
import type { UsageReport } from "../../src/PlansCard";
import type { FakeHandler } from "./index";

const MIN = 60_000;
const at = (min: number) => new Date(Date.now() + min * MIN).toISOString();

type Pace = NonNullable<RouteAccount["pace"]>[number];
const win = (window: string, used: number, resetsInMin: number, verdict: string, extra: Partial<Pace> = {}): Pace => ({
  window, cycle_resets_at: at(resetsInMin), trailing_h: window === "five_hour" ? 1 : 24, valid_pct: 100, used_pct: used,
  rate_pct_per_h: 5, plan: { verdict }, share_cap: null, day_cap: null, pace_target: null, headline: "plan", ...extra,
});

type State = {
  accounts: RouteAccount[];
  overrides: Array<Record<string, unknown>>;
  /** Snapshots, newest last, one per change that can be undone. */
  undo: Array<{ id: string; accounts: RouteAccount[]; overrides: Array<Record<string, unknown>> }>;
  n: number;
  problem: boolean;
  holdEnded?: boolean;
};

const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v)) as T;

function seed(problem: boolean, ahead: boolean): State {
  const accounts: RouteAccount[] = [
    {
      key: "claude|", label: "Claude", harness: "claude", state: problem ? "limited" : "open", until: problem ? at(56) : null,
      pace: [win("five_hour", 60, 56, "outpacing", { plan: { verdict: "outpacing", runs_out_at: at(36), before_reset_s: 20 * 60 } }),
        win("weekly", 38, 4 * 24 * 60, "on_pace")],
      cap_statuses: [],
    },
    {
      key: "codex|", label: "Codex", harness: "codex", state: "open",
      pace: [win("weekly", 42, 3 * 24 * 60, "outpacing")],
      cap_statuses: [{ kind: "pace_target", purpose: "job", window: "weekly", pct: 100, slack: 5, verdict: "ahead" }],
    },
    {
      key: "claude|~/.claude-second", label: "Claude second", harness: "claude", state: "open",
      pace: [win("five_hour", 8, 200, "underpacing"), win("weekly", 19, 2 * 24 * 60, "underpacing")],
      cap_statuses: [
        { kind: "share", window: "five_hour", pct: 60 },
        { kind: "share", window: "weekly", pct: 91 },
      ],
    },
    {
      key: "grok|", label: "Grok", harness: "grok", state: "open",
      pace: [win("weekly", 81, 5 * 24 * 60, "on_pace")],
      cap_statuses: [{ kind: "share", window: "weekly", pct: 90 }],
    },
    {
      key: "kimi|", label: "Kimi", harness: "kimi", state: "open",
      pace: [win("weekly", 12, 4 * 24 * 60, "underpacing")], cap_statuses: [],
    },
    {
      key: "glm|", label: "GLM", harness: "glm", state: "paused", until: at(2 * 24 * 60),
      pace: [win("monthly", 26, 20 * 24 * 60, "on_pace")], cap_statuses: [],
    },
    { key: "deepseek|", label: "DeepSeek", harness: "deepseek", state: "open", pace: [], cap_statuses: [] },
    {
      key: "muse|", label: "Muse", harness: "muse", state: "open",
      pace: [win("weekly", 50, 4 * 24 * 60, "on_pace")], cap_statuses: [],
    },
  ];
  const overrides = ahead
    ? [{ id: "ov-1", label: "Claude", who: "The T3 conversation “Khipu app mocks”", until: at(3 * 60), indefinite: false, review_at: null,
        review_due: false, cap: "It stops at the account's saved share cap if it has one, otherwise at its plan limit" }]
    : [];
  return { accounts, overrides, undo: [], n: 0, problem };
}

let state: State | null = null;
const get = (flags: Record<string, unknown>): State => (state ??= seed(!!flags.accountsProblem, !!flags.accountsAhead));

const view = (s: State): RouteView => ({
  record: true,
  version: 7,
  checked_at: at(-3),
  this_mac: "M5",
  accounts: clone(s.accounts),
  priority: {
    entries: s.accounts.map((a) => ({ account: a.key, label: a.label })),
    version: "p1",
  },
  priority_version: "p1",
  holds: s.problem && !s.holdEnded
    ? [{ id: "h1", scope: { kind: "account", account: s.accounts[0]?.key }, scope_label: s.accounts[0]?.label ?? "Claude",
        reason: "owner", text: "No long batch jobs until Friday", until: at(60 * 24 * 3), owner: true, by: "you" }]
    : [],
  rules: [],
});

const usage = (s: State): UsageReport => ({
  generated_at: at(-3),
  accounts: s.accounts.map((a) => {
    const signedOut = s.problem && a.key === "claude|~/.claude-second";
    return {
      key: a.key, harness: a.harness, account: a.label, home: null,
      plan: a.harness === "claude" ? "max" : a.harness === "codex" ? "pro" : a.harness === "grok" ? "ultra" : "coding",
      ok: !signedOut, error: signedOut ? "not_signed_in" : null, last_attempt_at: at(-3), probed_at: at(-3),
      windows: signedOut ? [] : [{ name: "weekly", used_pct: 40, window_mins: 10080, resets_at: at(3 * 24 * 60) }],
    };
  }),
});

const CANDIDATES = [
  { subject: { kind: "thread", id: "thr-aaa", mac: "m5-id" }, title: "Khipu app mocks", mac: "M5", last_active: at(-12) },
  { subject: { kind: "session", id: "ses-bbb", harness: "claude_code", mac: "m5-id" }, title: "Claude Code conversation in khipu", mac: "M5", last_active: at(-90) },
  { subject: { kind: "thread", id: "thr-ccc", mac: "m5-id" }, title: "Needs-you review", mac: "M5", last_active: at(-200) },
];

function snapshot(s: State): string {
  const id = `${(++s.n).toString(16).padStart(2, "0")}c0ffee`.slice(-8);
  s.undo.push({ id, accounts: clone(s.accounts), overrides: clone(s.overrides) });
  return id;
}

export const accountsFakes: FakeHandler = (cmd, args, flags) => {
  if (!flags.accounts) return undefined;
  const s = get(flags);
  const find = (key: unknown) => s.accounts.find((a) => a.key === key || a.label === key);
  switch (cmd) {
    case "khipu_route_view":
      return view(s);
    case "khipu_usage_status":
    case "khipu_usage_refresh":
      return usage(s);
    case "khipu_route_override_view":
      return { ok: true, overrides: clone(s.overrides) };
    case "khipu_route_override_candidates": {
      const a = find(args?.account);
      const list = a && (a.harness === "claude" || a.harness === "codex") ? CANDIDATES : a?.harness === "grok" ? CANDIDATES.slice(0, 1) : [];
      return { ok: true, candidates: list };
    }
    case "khipu_route_override_act": {
      if (args?.verb === "grant") {
        const a = find(args.account);
        const id = snapshot(s);
        const c = CANDIDATES.find((x) => JSON.stringify(x.subject) === JSON.stringify(args.subject));
        s.overrides.push({ id: `ov-${s.n + 1}`, label: a?.label ?? "", who: `The T3 conversation “${c?.title ?? "A conversation"}”`, until: args.until,
          indefinite: false, review_at: null, review_due: false, cap: "It stops at the account's saved share cap if it has one, otherwise at its plan limit" });
        return { ok: true, id: `ov-${s.n + 1}`, account: a?.key, until: args.until, indefinite: false, replaced: [], owner_action: id };
      }
      const id = snapshot(s);
      if (args?.verb === "end") s.overrides = s.overrides.filter((o) => o.id !== args.id);
      return { ok: true, owner_action: id };
    }
    case "khipu_route_pause": {
      if (flags.accountsRefuse) return { ok: false, error: "Khipu can't write the routing record right now: it is read-only on this Mac." };
      const a = find(args?.account);
      const id = snapshot(s);
      if (a) { a.state = "paused"; a.until = (args?.until as string | null) ?? null; }
      return { ok: true, owner_action: id };
    }
    case "khipu_route_hold_end":
      s.holdEnded = true;
      return { ok: true };
    case "khipu_route_clear": {
      const a = find(args?.account);
      if (a && (a.state === "limited" || a.state === "down")) a.state = "open";
      return { ok: true };
    }
    case "khipu_route_lift": {
      const a = find(args?.account);
      const id = snapshot(s);
      if (a) { a.state = "open"; a.until = null; }
      return { ok: true, owner_action: id };
    }
    case "khipu_route_cap": {
      const a = find(args?.account);
      const id = snapshot(s);
      const cap = args?.cap as { kind: string; window?: string; pct?: number };
      if (a) {
        const rest = (a.cap_statuses ?? []).filter((c) => !(c.kind === cap.kind && (c.window || "weekly") === (cap.window || "weekly")));
        a.cap_statuses = args?.action === "set" ? [...rest, { kind: cap.kind, window: cap.window, pct: cap.pct }] : rest;
      }
      return { ok: true, owner_action: id };
    }
    case "khipu_owner_direction_undo": {
      const i = s.undo.findIndex((u) => u.id === args?.id);
      if (i < 0) return { ok: false, error: "That change is no longer there to undo." };
      s.accounts = s.undo[i].accounts;
      s.overrides = s.undo[i].overrides;
      s.undo = s.undo.slice(0, i);
      return { ok: true };
    }
    default:
      return undefined;
  }
};
