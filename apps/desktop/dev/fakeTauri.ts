// Tauri's invoke, faked for the dev preview: canned answers shaped as the
// CLI prints them, from fictional fixtures. `window.__khipuFake` picks the
// variant (9B here or not, what discovery found).
import { CLIENT, TRACKED, discovery, liveRow, status9B } from "../src/__tests__/accountsFixtures";
import type { DiscoverResult } from "../src/accountsApi";
import { DELIVERY_VIEW } from "./deliveryFake";
import { fakeAnswer, isRaw, RAW } from "./fakes";
import { config as fixtureRoutingConfig } from "../src/__tests__/routingSettingsFixtures";

export type Fake = { projects?: unknown; nineB?: boolean; disc?: DiscoverResult; usage?: unknown; none?: boolean; route15a?: boolean; route15b?: boolean; routeNeeds?: boolean; worst?: boolean; routeReplication?: boolean; routeMacs?: "first" | "settled" | "off"; overrides?: boolean; welcome?: boolean; quiet?: boolean; accounts?: boolean; accountsProblem?: boolean; accountsAhead?: boolean; accountsRefuse?: boolean; healthProblem?: boolean; healthUnknown?: boolean; healthNone?: boolean; running?: boolean; runningSilent?: boolean; runningNone?: boolean; runningStale?: boolean; runningFail?: boolean };

/** Values used by every routing-settings preview. They match the CLI's named
 * settings so every Change control is enabled with a real-looking value. */
function routingFixtureSettings() {
  const setting = (value: unknown, kind = "count") => ({ value, default: value, kind, source: "default" });
  return {
    ...fixtureRoutingConfig().routing,
    routing_dispatch_scripts: setting(["claude -p", "codex exec"], "commands"),
    routing_summary_chars: setting(1500), routing_prompt_chars: setting(400), routing_child_chars: setting(4000),
    routing_line_chars: setting(200), routing_rule_chars: setting(600), routing_rules_max: setting(40),
    routing_note_chars: setting(1500), routing_rules_chars: setting(2000), routing_prompt_budget_ms: setting(50),
    routing_slow_read_pct: setting(1, "number"), routing_start_budget_ms: setting(1000),
    routing_resend_after_versions: setting(20), routing_resend_after: setting(6 * 3600, "duration"),
    routing_stale_after: setting(12 * 3600, "duration"), routing_report_default_limit: setting(2 * 3600, "duration"),
    routing_priority_unconfirmed_warn_after: setting(86400, "duration"),
    routing_disagreement_warn_after: setting(3600, "duration"), routing_not_refreshed_after: setting(5 * 60, "duration"),
    routing_ledger_keep_days: setting(7), routing_poll_interval: setting(30, "duration"), routing_runner_silent_intervals: setting(5),
    routing_runner_tail_bytes: setting(512 * 1024), routing_runner_limited_lines: setting(200),
  };
}

/** Fictional routing record for the Proposals and holds group (slice 15b),
 *  shaped as `khipu route --json` prints it. */
function route15b() {
  const setting = (value: unknown) => ({ value, default: value, kind: "x", source: "default" });
  return {
    view: {
      record: true, version: 12, checked_at: "2026-10-21T18:40:00Z",
      accounts: [
        { key: "codex|", label: "Codex", harness: "codex", state: "open", models: [], witness: null, disagreement: null, last_seen: null },
        { key: "grok|", label: "Grok", harness: "grok", state: "open", models: [], witness: null, disagreement: null, last_seen: null },
      ],
      holds: [
        { id: "h-grok", scope_label: "Grok", reason: "keep", text: "Keep Grok for Grok Bot this week", until: "2026-10-23T22:00:00Z", owner: true, by: "you", at: "2026-10-20T13:14:00Z" },
        { id: "h-second", scope_label: "Claude second", reason: "machine_busy", text: "The Mac is busy", until: "2026-10-22T02:40:00Z", owner: false, by: "a Claude Code conversation in project-a", at: "2026-10-21T16:00:00Z" },
      ],
      rules: [
        { n: 1, entity: "rule-a", text: "Grok, GLM and DeepSeek never build or fix privacy, safety, sign-in or money code. Use at most 76% of the xAI week.", pending: true, pending_kind: "changed", pending_by: "a Claude Code conversation in project-a", prior_text: "Grok, GLM and DeepSeek never build or fix privacy, safety, sign-in or money code. Use at most 80% of the xAI week." },
      ],
      priority: {
        entries: [{ account: "codex|", label: "Codex", role: "Paced: new launches only while its week is at most 15 points ahead of an even pace." }],
        pending: true, pending_by: "a Codex conversation in project-b", owner_request: "loosen Codex's pace",
        replaced: { entries: [{ account: "codex|", label: "Codex", role: "Paced: new launches only while its week is at most 10 points ahead of an even pace." }], until: null },
        version: "p3",
      },
      priority_version: "p3",
      proposals: {
        "prop-rule": { id: "prop-rule", entity: "rule-a", kind: "rule", n: 1, status: "live", at: "2026-10-21T18:15:00Z", author: { kind: "conversation", harness: "claude_code", project: "project-a" } },
        "prop-prio": { id: "prop-prio", entity: "priority", kind: "priority", status: "live", at: "2026-10-21T17:40:00Z", author: { kind: "conversation", harness: "codex", project: "project-b" } },
        "prop-crossed": { id: "prop-crossed", entity: "rule-b", kind: "rule", n: 6, status: "crossed", at: "2026-10-21T15:00:00Z", author: { kind: "conversation", harness: "grok_build", project: "project-d" } },
        "prop-done": { id: "prop-done", entity: "rule-c", kind: "rule", n: 9, status: "dismissed", at: "2026-10-20T12:00:00Z", ended_at: "2026-10-21T09:00:00Z", author: { kind: "conversation", harness: "codex", project: "project-b" } },
      },
      note: { text: "We don't need cursor. Just grok.", at: "2026-10-20T14:00:00Z" },
      changes: [],
    },
    config: {
      routing: {
        ...routingFixtureSettings(),
        routing_conversation_hold: setting(86400), routing_conversation_hold_max: setting(604800),
        routing_owner_hold_reminder: setting(7776000), routing_hold_chars: setting(300),
        routing_proposal_text_between_macs: setting(true),
        routing_replication_live: setting(false),
        routing_push_start: setting(true), routing_push_prompt: setting(true), routing_push_delegation: setting(true),
      },
    },
  };
}

/** Every kind of Needs you item at once (settings reorg slice A), with long
 *  words where the owner's can be long. Fictional. */
function routeNeedsWorst() {
  const base = routeNeeds();
  const long = "Reviews can use the second Claude account only after the first limit, and only for repositories whose owners have said in writing that a second reviewer is fine, and never for anything touching money, sign-in or privacy code, whichever account a job runner reports first";
  const path = "/Users/someone/Library/Application Support/SomeTool/profiles/a-very-long-profile-name-with-no-spaces-at-all-0123456789abcdef0123456789abcdef/settings.json";
  const entries = Array.from({ length: 20 }, (_, i) => ({ account: `a${i}|`, label: `Account number ${i + 1}`,
    role: i % 3 === 0 ? "Paced: new launches only while its week is at most 15 points ahead of an even pace, and never for review work on the money code" : "builds" }));
  const v = base.view as unknown as { accounts: unknown[]; rules: Record<string, unknown>[]; priority: Record<string, unknown>; holds: Record<string, unknown>[]; sources: { runners: Record<string, unknown>[] }; unmatched: Record<string, unknown>[] };
  v.priority.entries = [entries[7], entries[0], entries[1], ...entries.slice(2, 7), ...entries.slice(8, 18), { account: "new|", label: "A brand new account", role: "second opinions" }];
  v.priority.replaced = { entries, until: null };
  v.priority.owner_request = `${long}. Also: ${path}`;
  v.rules[0].text = `${long}. See ${path}.`;
  v.rules[0].prior_text = long;
  v.rules[2].text = `${long}, copied from the old file.`;
  v.holds[0].text = `${long}.`;
  const delivery = base.view.delivery_rows;
  delivery[0].label = "Review settings for a-project-with-a-very-long-name-and-no-spaces-0123456789";
  delivery[1].mac = "MacBook Pro in the upstairs office";
  const runner = "A-project-with-a-very-long-name-and-no-spaces-to-break-it-at-all-0123456789";
  v.sources.runners[0].name = runner;
  v.unmatched[0] = { runner, lane: "opus-batch-with-a-very-long-lane-name-0123456789" };
  const routing = base.config.routing as Record<string, unknown>;
  routing.routing_lane_map = { value: { [runner]: { "opus-batch": "claude|", "sonnet-batch": "codex|", "gone-lane": "old|~/.gone" } }, default: {}, kind: "lane_map", source: "file", pending: true,
    request: { request: "send them over", by_words: "a Grok Build conversation", at: new Date().toISOString(), prior: { [runner]: { "sonnet-batch": "claude|" } } } };
  routing.routing_rules_chars = { value: 0, default: 2000, kind: "count", source: "file", pending: true,
    request: { request: long, by: { kind: "conversation", harness: "claude_code", project: "a-project-with-a-very-long-name-and-no-spaces-to-break-it-at-all" }, at: new Date().toISOString(), prior: 2000 } };
  routing.routing_table = { value: path, default: null, kind: "path", source: "file", pending: true, request: { by_words: "a Codex conversation", at: new Date().toISOString(), prior: null } };
  routing.routing_usage_login_cursor = { value: false, default: false, kind: "switch", source: "default", pending: true,
    request: { request: "read cursor's usage", by_words: "a Cursor conversation", at: new Date().toISOString(), prior: null, asked: true, asked_set: true } };
  routing.routing_usage_login_grok = { value: true, default: true, kind: "switch", source: "default", pending: true,
    request: { by_words: "a Codex conversation", at: new Date().toISOString(), prior: null } };
  return base;
}

function routeNeeds() {
  const ago = (min: number) => new Date(Date.now() - min * 60000).toISOString();
  const setting = (value: unknown, kind = "x") => ({ value, default: value, kind, source: "default" });
  return {
    view: {
      record: true, version: 12, checked_at: ago(1),
      delivery_rows: [
        { label: "Review the settings pages", harness: "codex", component: "main", mac: "This Mac", last_prompt_at: ago(5), status: "green", owed: 0 },
        { label: "Fix the nightly build", harness: "claude_code", component: "main", mac: "MacBook Pro", last_prompt_at: ago(18), status: "red", owed: 3 },
        { label: "Documentation helper", harness: "claude_code", component: "agent", mac: "This Mac", last_prompt_at: null, status: "unknown", missing: ["delivery time"] },
      ],
      accounts: [
        { key: "codex|", label: "Codex", harness: "codex", state: "open", models: [], witness: null, disagreement: null, last_seen: null },
        { key: "claude|", label: "Claude", harness: "claude", state: "open", models: [], witness: null, disagreement: null, last_seen: null },
        { key: "claude|~/.second", label: "Claude second", harness: "claude", state: "limited", until: new Date(Date.now() + 3 * 3600000).toISOString(), models: [],
          witness: { id: "w1", source: "probe", by: "Khipu usage probe", at: ago(20) },
          disagreement: { id: "d1", state: "open", source: "conversation", by: "a Claude Code conversation in project-b", at: ago(9) }, last_seen: null },
      ],
      holds: [
        { id: "h-grok", scope_label: "Grok", reason: "keep", text: "Keep Grok for reviews only.", until: null, owner: true, by: "you", at: ago(60 * 24 * 92), remind: true },
      ],
      rules: [
        { n: 1, entity: "rule-a", text: "Reviews can use Claude second after the first limit, and only for the repositories whose owners have said in writing that a second reviewer is fine.", pending: true, pending_kind: "changed",
          pending_by: "a Claude Code conversation in project-b", prior_text: "Reviews stay on Claude.", revision: "r1", at: ago(40) },
        { n: 2, entity: "rule-b", text: "Docs may go to Grok.", pending: true, pending_kind: "added", pending_by: "a Codex conversation in project-a", revision: "r2", at: ago(90) },
        { n: 3, entity: "rule-c", text: "Never a -fast tier.", seeded: true, confirmed: false, revision: "r3" },
        { n: 4, entity: "rule-d", text: "Nothing goes through Cursor.", seeded: true, confirmed: false, revision: "r4" },
      ],
      priority: {
        entries: [
          { account: "codex|", label: "Codex", role: "builds" },
          { account: "claude|", label: "Claude", role: "design and review" },
          { account: "grok|", label: "Grok", role: "second opinions" },
        ],
        pending: true, pending_by: "a Codex conversation", pending_author: { kind: "conversation", harness: "codex", project: "project-c" },
        owner_request: "put codex first", at: ago(70),
        replaced: { entries: [{ account: "claude|", label: "Claude", role: "design and review" }, { account: "codex|", label: "Codex", role: "builds" }], until: null },
        version: "p3",
      },
      priority_version: "p3",
      proposals: {
        "prop-prio": { id: "prop-prio", entity: "priority", kind: "priority", status: "live", at: ago(70), author: { kind: "conversation", harness: "codex", project: "project-c" } },
        "prop-rule": { id: "prop-rule", entity: "rule-a", kind: "rule", n: 1, status: "live", at: ago(40), author: { kind: "conversation", harness: "claude_code", project: "project-b" } },
        "prop-new": { id: "prop-new", entity: "rule-b", kind: "rule", n: 2, status: "live", at: ago(90), author: { kind: "conversation", harness: "codex", project: "project-a" } },
      },
      sources: {
        runners: [{ name: "Project B", silent: true, heartbeat_at: ago(95), lanes: ["opus-batch"] }],
      },
      unmatched: [{ runner: "Project B", lane: "opus-batch" }],
      changes: [
        { version: 11, at: ago(20), entities: ["account:claude|"], items: [
          { entity: "account:claude|", now: { label: "Claude", state: "limited", until: new Date(Date.now() + 3600000).toISOString(), basis: "cli" },
            was: { label: "Claude", state: "open" }, source: "probe", by: "Khipu usage probe" },
        ] },
        { version: 10, at: ago(50), entities: ["rule:6"], items: [
          { entity: "rule:6", n: 6, now: null, source: null, via: "app", was: { seeded: false,
            text: "Keep documentation reviews on the account assigned to this project, including long-project-name-with-no-spaces-012345678901234567890123456789." }, by: "you" },
        ] },
      ],
    },
    config: {
      routing: {
        ...routingFixtureSettings(),
        routing_push_start: setting(true, "switch"),
        routing_push_prompt: { value: false, default: true, kind: "switch", source: "file", words: "Tell conversations before each prompt", pending: true,
          request: { request: "stop the per-prompt updates", by: { kind: "conversation", harness: "codex", project: "project-a" }, by_words: "a Codex conversation", at: ago(30), prior: true } },
        routing_line_chars: { value: 0, default: 200, kind: "count", source: "file", words: "Longest line, rules and note aside (0: no limit)", pending: true,
          request: { by_words: "a Claude Code conversation", at: ago(15), prior: null } },
        routing_lane_map: { value: { "Project B": { "opus-batch": "claude|" } }, default: {}, kind: "lane_map", source: "file", words: "Which account each job lane uses", pending: true,
          request: { request: "send opus-batch to Claude", by_words: "a Grok Build conversation", at: ago(12), prior: {} } },
        routing_replication_live: { value: false, default: false, kind: "switch", source: "file", words: "Copy routing between your Macs through the hub", pending: true,
          request: { request: "turn on copying", by_words: "a Codex conversation", at: ago(5), prior: null } },
        routing_delivery_live_after: setting(43200),
      },
    },
    usage: { accounts: [
      { harness: "codex", account: "Codex", home: null, key: "codex|", ok: true, plan: "plus", windows: [] },
      { harness: "codex", account: "Codex second", home: "~/.codex-second", key: "codex|~/.codex-second", ok: false, error: "not_signed_in", windows: [] },
    ] },
  };
}

/** Fictional routing record with the Mac-to-Mac replication switch on. */
function routeReplication() {
  const base = route15b();
  return {
    view: base.view,
    config: {
      routing: {
        ...routingFixtureSettings(),
        ...base.config.routing,
        routing_replication_live: { value: true, default: false, kind: "switch", source: "file" },
      },
    },
  };
}

/** Fictional route/config reads for the delivery Settings preview. Doctor
 * evidence remains a component prop because production doctor data is read
 * by App, not by the route command. */
function route15a() {
  const setting = (value: unknown) => ({ value, default: value, kind: "x", source: "default" });
  return {
    view: DELIVERY_VIEW,
    config: {
      routing: {
        ...routingFixtureSettings(),
        routing_push_start: setting(true), routing_push_prompt: setting(true), routing_push_delegation: setting(true),
        routing_delivery_live_after: setting(43200),
      },
    },
  };
}

/** Your Macs, three states, with fictional Macs (Studio and Laptop): first
 * switch-on with differences to choose, everything settled, and the copy off. */
function routeMacs(kind: "first" | "settled" | "off") {
  const base = routeReplication();
  const on = kind !== "off";
  return {
    view: base.view,
    config: {
      routing: { ...base.config.routing, routing_replication_live: { value: on, default: false, kind: "switch", source: "file" } },
      settings_sync: {
        enabled: on,
        differences: kind === "first" ? [
          { key: "routing_summary_chars", name: "Longest summary", local: 700, remote: 900, remote_snapshot: true },
          { key: "routing_stale_after", name: "Out-of-date after", local: 21600, remote: 43200, remote_snapshot: true },
          { key: "routing_resend_after", name: "Resend a changed fact after", local: 7200, remote: 3600, remote_snapshot: true },
        ] : [],
        waiting: kind === "first" ? [
          { id: "rm-req-0a1b2c3d4e5f", request: "req-0a1b2c3d4e5f", mac: "Studio", state: "pending", answerable: true,
            ask: "A conversation on Studio asked to pause Codex for 6 hours, and it is held until you answer." },
        ] : [],
        requests: kind === "settled" ? [
          { request: "req-1", mac: "Studio", state: "applied", words: "Applied on Studio." },
          { request: "req-2", mac: "Studio", state: "stale", stale: true, words: "That was already settled on Studio before your answer arrived, so your answer changed nothing." },
        ] : [],
      },
    },
  };
}

// Conversation overrides and the tidy-up walk, fictional: enough state to click through
// (end early, undo, say yes, leave, start over) in the dev preview.
const overrideState = {
  rows: [
    { id: "ov-1", label: "Claude second", who: "The T3 conversation \"Healthy You retrospective\"", until: "2026-11-04T19:40:00Z",
      indefinite: false, review_at: null, review_due: false, cap: "It stops when the account reaches 60% of its week" },
    { id: "ov-2", label: "Grok", who: "Every conversation in the folder khipu-slices", until: null, indefinite: true,
      review_at: "2026-11-04T09:00:00Z", review_due: true, cap: "It stops at the account's saved share cap if it has one, otherwise at its plan limit" },
  ] as Array<Record<string, unknown>>,
  items: [
    { code: "duplicate", key: "dup:codex|:x1", words: "Codex has \"re-check of PRs #3 and #11\" twice (ends Nov 5 14:00Z and Nov 5 15:00Z).", yes: "End the second one and keep the first.", settle: { confirm: { action: "route.priority.amend", args: { account: "codex|", end_exception: "x1", expect: "p4" } } } },
    { code: "near_duplicate", key: "dup:codex|:x2", words: "Codex has \"re-check of PRs #3 and #11\" twice, worded a little differently (ends Nov 5 14:00Z and Nov 5 16:00Z).", yes: "End the second one and keep the first.", settle: { confirm: { action: "route.priority.amend", args: { account: "codex|", end_exception: "x2", expect: "p4" } } } },
    { code: "twin", key: "twin:x1:x5", words: "The same instruction \"re-check of PRs #3 and #11\" is on both Codex and Claude.", yes: "Keep both; stop asking about this pair.", settle: { confirm: null } },
  ] as Array<Record<string, unknown>>,
  left: [] as string[],
  undone: null as null | Array<Record<string, unknown>>,
};

function walkAnswer() {
  const pending = overrideState.items;
  const fresh = pending.filter((i) => !overrideState.left.includes(i.key as string));
  const later = pending.filter((i) => overrideState.left.includes(i.key as string));
  const ordered = [...fresh, ...later];
  const item = ordered[0] ? { ...ordered[0], number: 1, of: ordered.length, came_back: overrideState.left.includes(ordered[0].key as string) } : null;
  return { ok: true, total: ordered.length, left_for_later: later.length, item, soon: [] };
}

function overridesCommand(cmd: string, args?: Record<string, unknown>): unknown | undefined {
  switch (cmd) {
    case "khipu_route_override_view":
      return { ok: true, overrides: overrideState.rows };
    case "khipu_route_override_act": {
      overrideState.undone = overrideState.rows.slice();
      overrideState.rows = overrideState.rows.filter((r) => r.id !== args?.id);
      return { ok: true, owner_action: "act-1" };
    }
    case "khipu_owner_direction_undo":
      if (overrideState.undone) overrideState.rows = overrideState.undone;
      return { ok: true };
    case "khipu_route_cleanup_walk":
      if (args?.restart) overrideState.left = [];
      if (args?.leave) overrideState.left.push(args.leave as string);
      if (args?.keep) overrideState.items = overrideState.items.filter((i) => i.key !== args.keep);
      return walkAnswer();
    case "khipu_route_exception_end":
      overrideState.items = overrideState.items.filter((i) => ((i.settle as { confirm: { args: { end_exception: string } } | null }).confirm?.args.end_exception) !== args?.id);
      return { ok: true };
    default:
      return undefined;
  }
}

function fake(): Fake {
  return ((window as unknown as { __khipuFake?: Fake }).__khipuFake ?? {}) as Fake;
}

export async function invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  const f = fake();
  await new Promise((r) => setTimeout(r, 200));
  const answer = (() => {
    const shared = fakeAnswer(cmd, args, f as Record<string, unknown>);
    if (shared !== undefined) return shared;
    if (f.overrides) {
      const mine = overridesCommand(cmd, args);
      if (mine !== undefined) return mine;
    }
    switch (cmd) {
      case "khipu_accounts_discover":
      case "khipu_accounts_check":
        return f.disc ?? discovery();
      case "khipu_accounts_status":
        if (!f.nineB) throw new Error("Command khipu_accounts_status not found");
        if (f.routeNeeds) {
          return status9B({ accounts: TRACKED, request: { request: "track my Muse account", at: new Date(Date.now() - 8 * 60000).toISOString(),
            by: { kind: "conversation", harness: "codex", project: "project-a" }, prior: TRACKED.slice(0, -1) } } as never);
        }
        return status9B({ none: !!f.none });
      case "khipu_accounts_try":
        return { ok: true, default: false, row: liveRow({ account: String(args?.harness ?? "claude") }) };
      case "khipu_config_show":
        if (f.routeNeeds) return (f.worst ? routeNeedsWorst() : routeNeeds()).config;
        if (f.routeMacs) return routeMacs(f.routeMacs).config;
        if (f.routeReplication) return routeReplication().config;
        if (f.route15a) return route15a().config;
        if (f.route15b) return route15b().config;
        return {
          usage_accounts: { source: "default", accounts: TRACKED, user_home: "/home/someone" },
          cli_settings: { node_folder: { value: null, usable: false } },
          offline: {
            hub_statement_timeout: { value: 30, default: 30, kind: "duration", source: "default", words: "Longest an owed or decision read or write waits on the hub before it uses this Mac's copy (0: no limit)" },
            hub_dead_connection_after: { value: 60, default: 60, kind: "duration", source: "default", words: "How soon a hub connection that went silent (a network change) is given up (0: wait for the system)" },
            app_cli_timeout: { value: 45, default: 60, kind: "duration", source: "file", words: "Longest the app waits for one Khipu answer before it shows the hub as not reachable (0: no limit)" },
          },
        };
      case "khipu_usage_status":
      case "khipu_usage_refresh":
        if (f.routeNeeds) return routeNeeds().usage;
        return f.usage ?? { accounts: [{ harness: "claude", account: "Claude Code", home: null, ok: true, plan: "max", windows: [] }] };
      case "khipu_features_show":
        return { features: { routing_push: { enabled: true, source: "default" } } };
      case "khipu_projects_list":
        return { ok: true, ...(f.projects as object) };
      case "khipu_route_view":
        if (f.routeNeeds) return (f.worst ? routeNeedsWorst() : routeNeeds()).view;
        if (f.routeMacs) return routeMacs(f.routeMacs).view;
        if (f.routeReplication) return routeReplication().view;
        if (f.route15a) return route15a().view;
        if (f.route15b) return route15b().view;
        return { accounts: [], priority: null };
      case "khipu_account_sign_in":
        return { ok: true, row: liveRow({ account: CLIENT.name ?? "Claude client" }) };
      default:
        return { ok: true };
    }
  })();
  if (isRaw(answer)) return answer[RAW] as T;
  return JSON.stringify(answer) as unknown as T;
}
