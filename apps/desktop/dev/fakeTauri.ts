// Tauri's invoke, faked for the dev preview: canned answers shaped as the
// CLI prints them, from fictional fixtures. `window.__khipuFake` picks the
// variant (9B here or not, what discovery found).
import { CLIENT, TRACKED, discovery, liveRow, status9B } from "../src/__tests__/accountsFixtures";
import type { DiscoverResult } from "../src/accountsApi";

export type Fake = { nineB?: boolean; disc?: DiscoverResult; usage?: unknown; none?: boolean; route15b?: boolean };

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
        routing_conversation_hold: setting(86400), routing_conversation_hold_max: setting(604800),
        routing_owner_hold_reminder: setting(7776000), routing_hold_chars: setting(300),
        routing_proposal_text_between_macs: setting(true),
        routing_push_start: setting(true), routing_push_prompt: setting(true), routing_push_delegation: setting(true),
      },
    },
  };
}

function fake(): Fake {
  return ((window as unknown as { __khipuFake?: Fake }).__khipuFake ?? {}) as Fake;
}

export async function invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  const f = fake();
  await new Promise((r) => setTimeout(r, 200));
  const answer = (() => {
    switch (cmd) {
      case "khipu_accounts_discover":
      case "khipu_accounts_check":
        return f.disc ?? discovery();
      case "khipu_accounts_status":
        if (!f.nineB) throw new Error("Command khipu_accounts_status not found");
        return status9B({ none: !!f.none });
      case "khipu_accounts_try":
        return { ok: true, default: false, row: liveRow({ account: String(args?.harness ?? "claude") }) };
      case "khipu_config_show":
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
        return f.usage ?? { accounts: [{ harness: "claude", account: "Claude Code", home: null, ok: true, plan: "max", windows: [] }] };
      case "khipu_features_show":
        return { features: { routing_push: { enabled: true, source: "default" } } };
      case "khipu_route_view":
        if (f.route15b) return route15b().view;
        return { accounts: [], priority: null };
      case "khipu_account_sign_in":
        return { ok: true, row: liveRow({ account: CLIENT.name ?? "Claude client" }) };
      default:
        return { ok: true };
    }
  })();
  return JSON.stringify(answer) as unknown as T;
}
