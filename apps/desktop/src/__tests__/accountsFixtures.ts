// Fictional logins and accounts for phase 9's tests and the dev preview
// (apps/desktop/dev). No real folder or account name appears here.
import type { AccountsStatus9B, Candidate, CliState, DiscoverResult } from "../accountsApi";
import type { UsageAccount } from "../PlansCard";

export const NOW = new Date("2026-10-15T19:40:00Z");

export function cand(over: Partial<Candidate> & Pick<Candidate, "id" | "cli">): Candidate {
  const names: Record<string, string> = { claude: "Claude Code", codex: "Codex", grok: "Grok Build", muse: "Muse" };
  return {
    cli_name: names[over.cli],
    path: null,
    default: false,
    sources: ["name"],
    name: null,
    first_seen: "2026-10-15T19:20:00Z",
    last_checked: null,
    state: "signed_in",
    reason: null,
    plan: null,
    conversations_7d: null,
    needs_owner_check: false,
    offer_on_home: false,
    ...over,
  };
}

export const CLIENT = cand({
  id: "claude:inode:1", cli: "claude", path: "~/.claude-client", name: "Claude client", sources: ["conversation", "name"],
  state: "signed_in", plan: "pro", conversations_7d: 4, offer_on_home: true, last_checked: "2026-10-15T19:30:00Z",
});
export const OLD = cand({
  id: "claude:inode:2", cli: "claude", path: "~/.claude-old", name: "Claude old", state: "not_signed_in", conversations_7d: 0,
  last_checked: "2026-10-15T19:30:00Z",
});
export const CODEX_WORK = cand({
  id: "codex:inode:3", cli: "codex", path: "~/.codex-work", name: "Codex work", state: "not_checked", needs_owner_check: true,
});
export const CODEX_LAB = cand({
  id: "codex:inode:4", cli: "codex", path: "~/.codex-lab", name: "Codex lab", state: "couldnt_check", reason: "timeout",
  sources: ["conversation", "name"], conversations_7d: 1,
});
export const OFFICE = cand({
  id: "claude:inode:5", cli: "claude", path: "~/.claude-office", name: "Claude office", state: "signed_in", plan: "max 20x",
  sources: ["conversation", "name"], conversations_7d: 3, offer_on_home: true,
});
export const GROK_SECOND = cand({
  id: "grok:inode:6", cli: "grok", path: "~/.grok-second", name: "Grok second", state: "signed_in",
  sources: ["conversation", "name"], conversations_7d: 2, offer_on_home: true,
});

export function cli(c: CliState["cli"], state: CliState["state"], extra: Partial<CliState> = {}): CliState {
  const names: Record<string, string> = { claude: "Claude Code", codex: "Codex", grok: "Grok Build", muse: "Muse" };
  return { cli: c, cli_name: names[c], path: state === "not_installed" ? null : `/opt/fake/bin/${c}`, state, version: null, reason: null, ...extra };
}

export const CLIS_ALL: DiscoverResult["clis"] = {
  claude: cli("claude", "installed", { version: "2.1.296" }),
  codex: cli("codex", "installed", { version: "0.162.1" }),
  grok: cli("grok", "installed", { version: "1.0.52" }),
  muse: cli("muse", "installed", { version: "1.4.6" }),
};

export const CLIS_CODEX_NOT: DiscoverResult["clis"] = { ...CLIS_ALL, codex: cli("codex", "not_installed") };

export const CLIS_CODEX_CANT: DiscoverResult["clis"] = {
  ...CLIS_ALL,
  codex: cli("codex", "cant_run", { path: "/home/someone/.nvm/versions/node/v20.11.0/bin/codex", reason: "node_missing" }),
};

export const CLIS_NONE: DiscoverResult["clis"] = {
  claude: cli("claude", "not_installed"),
  codex: cli("codex", "not_installed"),
  grok: cli("grok", "not_installed"),
  muse: cli("muse", "not_installed"),
};

export function discovery(over: Partial<DiscoverResult> = {}): DiscoverResult {
  return {
    ok: true,
    candidates: [CLIENT, OLD, CODEX_WORK],
    last_run: { at: "2026-10-15T19:40:00Z", checked: 3, couldnt_check: 1, timed_out: 1, left_for_later: 0, not_checked: 1 },
    looking: false,
    clis: CLIS_ALL,
    ...over,
  };
}

export const TRACKED = [
  { label: "Claude main", harness: "claude" },
  { label: "Claude work", harness: "claude", home: "~/.claude-work" },
  { label: "Codex", harness: "codex" },
  { label: "Muse", harness: "muse" },
];

export function status9B(over: Partial<AccountsStatus9B> = {}): AccountsStatus9B {
  return {
    ok: true,
    version: "v12",
    none: false,
    auto_track: false,
    value: TRACKED,
    accounts: TRACKED.map((t) => ({ ...t, key: `${t.harness}|${t.home ?? ""}` })),
    user_home: "/home/someone",
    declined: [],
    request: null,
    ...over,
  };
}

export function liveRow(over: Partial<UsageAccount> = {}): UsageAccount {
  return {
    harness: "claude",
    account: "Claude",
    home: "/home/someone/.claude-client",
    plan: "pro",
    ok: true,
    windows: [{ name: "weekly", used_pct: 18, resets_at: "2026-10-20T13:00:00Z", window_mins: 10080 }],
    ...over,
  };
}
