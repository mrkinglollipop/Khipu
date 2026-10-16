// Tauri's invoke, faked for the dev preview: canned answers shaped as the
// CLI prints them, from fictional fixtures. `window.__khipuFake` picks the
// variant (9B here or not, what discovery found).
import { CLIENT, TRACKED, discovery, liveRow, status9B } from "../src/__tests__/accountsFixtures";
import type { DiscoverResult } from "../src/accountsApi";

export type Fake = { nineB?: boolean; disc?: DiscoverResult; usage?: unknown; none?: boolean };

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
        return { accounts: [], priority: null };
      case "khipu_account_sign_in":
        return { ok: true, row: liveRow({ account: CLIENT.name ?? "Claude client" }) };
      default:
        return { ok: true };
    }
  })();
  return JSON.stringify(answer) as unknown as T;
}
