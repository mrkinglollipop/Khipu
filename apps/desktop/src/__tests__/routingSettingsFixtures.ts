import type { RouteView, RouteRunner, RouteAccount } from "../routing";

export function acct(label: string, harness: string, extra: Partial<RouteAccount>): RouteAccount {
  return {
    key: `${harness}|${label === "Claude second" ? "/logins/claude-second" : ""}`,
    label,
    harness,
    state: "unknown",
    checkable: harness !== "muse",
    models: [],
    witness: null,
    disagreement: null,
    last_seen: null,
    ...extra,
  };
}

export function view(): RouteView {
  return {
    record: true,
    version: 12,
    checked_at: "2026-10-14T15:40:00Z",
    accounts: [
      acct("Claude main", "claude", {
        state: "limited", until: "2026-10-14T17:52:00Z", basis: "cli",
        witness: { source: "probe", written_by: { kind: "probe" }, at: "2026-10-14T15:36:00Z" },
      }),
      acct("Claude second", "claude", {
        state: "limited", until: "2026-10-14T18:52:00Z", basis: "cli",
        witness: { id: "r-runner", source: "runner", written_by: { kind: "runner", name: "Project A" }, at: "2026-10-14T15:13:00Z", message_time: true },
        disagreement: { id: "e-open", state: "open", source: "report", by: "a Claude Code conversation in project-b", at: "2026-10-14T15:31:00Z" },
      }),
      acct("Codex", "codex", {
        state: "open",
        witness: { source: "probe", written_by: { kind: "probe" }, at: "2026-10-14T15:36:00Z" },
      }),
      acct("Grok Build", "grok", {
        state: "paused", until: "2026-10-16T13:00:00Z", basis: "owner",
        witness: { source: "owner", written_by: { kind: "owner" }, at: "2026-10-13T22:05:00Z" },
      }),
    ],
    holds: [
      { id: "h1", scope_label: "this Mac", reason: "machine_busy", text: "No new launches while the Mac is busy (load over 75)",
        until: "2026-10-15T13:40:00Z", owner: false, by: "a Claude Code conversation in project-a", at: "2026-10-14T13:40:00Z" },
    ],
    note: { text: "We don't need cursor. Just grok.", at: "2026-10-13T14:00:00Z" },
    rules: [
      { n: 3, revision: "shown-rule-3", text: "Never a -fast tier.", seeded: true, confirmed: false, source: "from your routing table", date: "2026-10-13" },
      { n: 7, text: "Khipu's routing record is guidance, not a gate.", seeded: true, confirmed: true, source: "from your routing table", date: "2026-10-13" },
    ],
    watched_rules: [3],
    unmatched: [],
    sources: {
      runners: [
        { name: "Project A", path: "/runners/a", heartbeat_at: "2026-10-14T15:39:40Z", interval: 20, silent: false, unreadable: false,
          lanes: ["codex", "main", "secondary"], unmatched_lanes: [], files: {} },
        { name: "Project B", path: "/runners/b", heartbeat_at: "2026-10-14T14:02:00Z", interval: 30, silent: true, unreadable: false,
          lanes: ["opus-batch", "secondary"], unmatched_lanes: ["opus-batch"],
          files: { heartbeat: { error: null, good_at: "2026-10-14T14:02:00Z" } } },
      ],
      table: { stamped_at: "2026-10-14T13:20:00Z", changed_at: "2026-10-14T13:20:00Z" },
    },
    old_switches: { env: false, file: false },
    changes: [
      { version: 11, at: "2026-10-13T22:05:00Z", entities: ["account:grok|"], items: [
        // Owner items are `khipu route --json` output, captured in a temp
        // HOME after the app's own argv (fix round 1, F1).
        { entity: "account:grok|", now: { basis: "owner", disagreement: null, label: "Grok Build", last_seen: null, models: [], reason: null, scope: "all", state: "paused", until: "2026-10-16T13:00:00Z" },
          source: "owner", via: "app", was: { basis: null, disagreement: null, label: "Grok Build", last_seen: null, models: [], reason: null, scope: "all", state: "unknown", until: null }, by: "you" },
      ] },
      { version: 12, at: "2026-10-14T15:36:00Z", entities: ["account:claude|", "rule:6"], items: [
        { entity: "account:claude|", now: { label: "Claude main", state: "limited", until: "2026-10-14T17:52:00Z", basis: "cli" },
          was: { label: "Claude main", state: "open" }, source: "probe", by: "Khipu usage probe" },
        { entity: "rule:6", n: 6, now: null, source: null, via: "app", was: { seeded: false, text: "Never send keys to Codex." }, by: "you" },
      ] },
    ],
    rule_checks: [],
    warn_after: { disagreement: 1800, not_refreshed: 300 },
    report_default_limit: 1800,
  };
}

export function setting(value: unknown, def: unknown = value) {
  return { value, default: def, kind: "x", source: "default" };
}

export function config(extra: Record<string, unknown> = {}) {
  return {
    routing: {
      routing_stale_after: setting(43200),
      routing_conversation_hold: setting(86400),
      routing_conversation_hold_max: setting(604800),
      routing_owner_hold_reminder: setting(7776000),
      routing_disagreement_warn_after: setting(1800),
      routing_summary_chars: setting(1500),
      routing_prompt_chars: setting(400),
      routing_hold_chars: setting(300),
      routing_line_chars: setting(200),
      routing_prompt_budget_ms: setting(50),
      routing_resend_after_versions: setting(20),
      routing_resend_after: setting(21600),
      routing_not_refreshed_after: setting(300),
      routing_push_start: setting(true),
      routing_push_prompt: setting(true),
      routing_push_delegation: setting(true),
      routing_reset_check: setting(true),
      routing_reset_check_wait: setting(120),
      routing_reset_check_retries: setting([600, 1800]),
      routing_busy_load: setting(75),
      routing_busy_wait: setting(300),
      routing_busy_tries: setting(3),
      routing_sources: setting([
        { name: "Project A", kind: "lanes", path: "/runners/a" },
        { name: "Project B", kind: "lanes", path: "/runners/b" },
      ], []),
      routing_lane_map: setting({ "Project A": { codex: "Codex", main: "Claude main", secondary: "Claude second" },
        "Project B": { secondary: "Claude second" } }, {}),
      routing_table: setting("/notes/routing/ROUTING-NOW.md", null),
      ...extra,
    },
  };
}

export function lanesView(): RouteView {
  const v = view();
  v.accounts = [...v.accounts!, acct("Muse", "muse", {}), { ...acct("GLM", "glm", {}), key: "glm:main" }];
  v.definitions = {
    providers: {
      glm: { id: "glm", label: "GLM", family: "Zhipu", login: "none", probe: "none", lane_names: ["glm"],
        models_allowed: ["glm-5.3", "glm-5.3-flash"] },
      muse: { id: "muse", label: "Muse", family: "Meta", login: "none", probe: "none", lane_names: ["muse"],
        models_allowed: ["muse-wonder-full-1.0"], use: "on_request" },
    },
  };
  const match = (account: string | null, matched_by: string | null, extra: Partial<NonNullable<RouteRunner["lane_matches"]>[string]> = {}) =>
    ({ account, matched_by, candidates: [] as string[], identity_state: null, reason: null, field_to_add: null, ...extra });
  v.sources!.runners = [
    { name: "project-a", path: "/runners/a", heartbeat_at: "2026-10-14T15:39:40Z", interval: 20, silent: false, unreadable: false,
      lanes: ["claude", "grok-review", "review-2"], unmatched_lanes: [], files: {},
      lane_matches: {
        claude: match(null, null, { candidates: ["Claude main", "Claude second"], field_to_add: "budget_account" }),
        "grok-review": match("Grok Build", null),
        "review-2": match(null, null, { field_to_add: "budget_account" }),
      } },
    { name: "project-c", path: "/runners/c", heartbeat_at: "2026-10-14T15:39:00Z", interval: 20, silent: false, unreadable: false,
      lanes: ["codex", "glm", "main-opus", "main-sonnet", "secondary-haiku"], unmatched_lanes: [], files: {},
      lane_matches: {
        codex: match("Codex", "name"),
        glm: match("GLM", "name"),
        "main-opus": match(null, null, { candidates: ["Claude main", "Claude second"],
          reason: "launcher evidence contradicts the declaration", field_to_add: "budget_account" }),
        "main-sonnet": match("Claude main", "declaration", { identity_state: "confirmed" }),
        "secondary-haiku": match("Claude second", "declaration", { identity_state: "claimed" }),
      } },
  ];
  return v;
}

export function lanesConfig() {
  return config({
    routing_sources: setting([
      { name: "project-a", kind: "lanes", path: "/runners/a" },
      { name: "project-c", kind: "lanes", path: "/runners/c" },
    ], []),
    routing_lane_map: setting({ "project-a": { "grok-review": "Grok Build" } }, {}),
  });
}


const TIERS = {
  scope: "opus", mock: "opus", ruling: "opus", "hard-security": "opus", "failed-fix": "opus",
  build: "sonnet", "fix-round": "sonnet", tests: "sonnet", docs: "sonnet", synthesis: "sonnet",
  mechanical: "haiku",
};

export function checksView(): RouteView {
  const v = view();
  v.priority = {
    entries: [
      { account: "codex|", label: "Codex", role: "Builds and tests." },
      { account: "grok|", label: "Grok Build", role: "Reviews." },
      { account: "antigravity|", label: "Antigravity", in_settings: false },
    ],
    confirmed: true,
    version: "p1",
  };
  v.rules = [
    { n: 3, entity: "rule-3", text: "Grok Build handles fast models.", date: "2026-10-13" },
    { n: 6, entity: "rule-6", text: "Use Grok Build for fast models.", date: "2026-10-13" },
    { n: 9, entity: "rule-9", text: "Use Grok Build until Friday.", date: "2026-10-13" },
    { n: 7, entity: "rule-7", text: "Khipu's record is guidance.", pending: true, at: "2026-10-13T04:40:00Z" },
  ];
  v.contradictions = [
    { kind: "account_state", account: "grok:main", source: "priority", state: "paused", entities: ["priority-1"],
      text: "The priority names Grok Build, which is paused.", advisory: true },
    { kind: "account_state", account: "antigravity:main", source: "priority", state: "untracked", entities: ["priority-2"],
      text: "The priority names Antigravity, which is untracked.", advisory: true },
  ];
  v.rule_hygiene = [
    { kind: "move_to_role", account: "grok:main", entities: ["rule-9"], advisory: true,
      text: "Move this into the account's priority role with an until." },
    { kind: "overlap", account: "grok:main", entities: ["rule-3", "rule-6"], advisory: true,
      text: "Two standing rules name the same account and topic." },
    { kind: "long_pending", entities: ["rule-7"], advisory: true,
      text: "This edit has been waiting on the owner for more than 24 hours.",
      owner_direction: { confirm: "Confirm rule 7", drop: "Undo the pending edit of rule 7" } },
  ];
  v.tier_table = { confirmed: true, tiers: TIERS };
  return v;
}
