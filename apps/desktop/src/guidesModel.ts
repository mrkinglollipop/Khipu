/* Guides: what each guide is, and the live state the Guides page, the rail
   badge and Home's hint all read. Pure functions on purpose, so one set of
   rules decides "done" for all three surfaces and a test can pin it.

   docs: jobs/2026-10-26/setup-guides-design/guides-scope.md (§1 to §3). */

import type { LivenessPayload } from "./IntegrationsPanel";

export type GuideId =
  | "claude_code"
  | "codex"
  | "cursor"
  | "grok_bot"
  | "t3"
  | "aegis"
  | "other_cloud"
  | "grok_build"
  | "add_account"
  | "routing"
  | "caps"
  | "saving"
  | "forget"
  | "projects"
  | "gateway"
  | "backups"
  | "another_mac"
  | "red";

export type GuideGroup = "agents" | "accounts" | "memory" | "safe";

export const GROUP_TITLES: Record<GuideGroup, string> = {
  agents: "Connect your agents",
  accounts: "Accounts and limits",
  memory: "Your memory",
  safe: "Keep it safe",
};

export type GuideMeta = {
  id: GuideId;
  group: GuideGroup;
  title: string;
  blurb: string;
  /** Extra words the search matches ("grok", "backup", "key"). */
  aliases: string[];
};

export const GUIDES: GuideMeta[] = [
  { id: "claude_code", group: "agents", title: "Claude Code", blurb: "Install in each Claude folder, then restart it.", aliases: ["claude", "hooks", "t3"] },
  { id: "codex", group: "agents", title: "Codex", blurb: "Install, then approve Khipu's hooks inside Codex.", aliases: ["openai", "hooks", "approve"] },
  { id: "cursor", group: "agents", title: "Cursor", blurb: "Install, and add memory rules to each project.", aliases: ["rules", "project"] },
  { id: "grok_bot", group: "agents", title: "Grok Bot (in Cursor's cloud)", blurb: "Reach your memory from the cloud through your gateway.", aliases: ["grok", "cloud", "gateway", "key", "secret", "mcp"] },
  { id: "t3", group: "agents", title: "T3 Code", blurb: "Covered by the Claude Code install. Nothing extra to do.", aliases: ["t3", "claude"] },
  { id: "aegis", group: "agents", title: "Aegis", blurb: "Install, then save the gateway key so search works.", aliases: ["gateway", "key", "token"] },
  { id: "other_cloud", group: "agents", title: "Other cloud chat apps", blurb: "Any app that accepts a remote memory server (MCP) can use the gateway.", aliases: ["mcp", "gateway", "chat", "remote", "key"] },
  { id: "grok_build", group: "agents", title: "Grok Build", blurb: "Not supported yet.", aliases: ["grok", "cli"] },
  { id: "add_account", group: "accounts", title: "Add an AI account", blurb: "Sign in, or paste a key, so Khipu can read how much you've used.", aliases: ["account", "plan", "usage", "key", "sign in"] },
  { id: "routing", group: "accounts", title: "Which account takes which work", blurb: "Your priority, the words for each account, and rules.", aliases: ["priority", "rules", "routing"] },
  { id: "caps", group: "accounts", title: "Caps, pauses and exceptions", blurb: "Keep an account from running out, or allow something for a few days.", aliases: ["cap", "pause", "hold", "limit"] },
  { id: "saving", group: "memory", title: "How saving and recall work", blurb: "When a session is saved, and what your agent is shown.", aliases: ["capture", "recall", "save"] },
  { id: "forget", group: "memory", title: "Forget something", blurb: "Hide a saved session from every agent.", aliases: ["delete", "remove", "privacy"] },
  { id: "projects", group: "memory", title: "Projects, Owed and Work", blurb: "How sessions group into projects, and what 'owed' means.", aliases: ["owed", "work", "commitments"] },
  { id: "backups", group: "safe", title: "Backups and copies on this Mac", blurb: "What's copied where, and how to restore.", aliases: ["backup", "restore", "copy"] },
  { id: "another_mac", group: "safe", title: "Add another Mac", blurb: "Save a join kit here, open it on the other Mac.", aliases: ["mac", "join", "kit", "second"] },
  { id: "gateway", group: "safe", title: "Run a gateway", blurb: "The one part of Khipu that cloud agents reach over the internet.", aliases: ["server", "https", "address", "cloud"] },
  { id: "red", group: "safe", title: "When something turns red", blurb: "What each health check means and how to fix it.", aliases: ["health", "problem", "error", "red"] },
];

export function guideMeta(id: GuideId): GuideMeta {
  const g = GUIDES.find((x) => x.id === id);
  if (!g) throw new Error(`unknown guide ${id}`);
  return g;
}

/** Title and aliases only match; an empty query shows everything. */
export function filterGuides(query: string): GuideMeta[] {
  const q = query.trim().toLowerCase();
  if (!q) return GUIDES;
  return GUIDES.filter((g) => [g.title, g.blurb, ...g.aliases].join(" ").toLowerCase().includes(q));
}

// ---------------------------------------------------------------------------
// What is remembered on this install (a hidden hint, a step acknowledged).
// Stored as an app setting in the data dir (survives a webview reset), not
// in localStorage. parsePrefs / prefsJson are pure so tests and the CLI
// writer share one shape.
// ---------------------------------------------------------------------------

/** Legacy webview key; read once to migrate into the app setting. */
export const LEGACY_STORE_KEY = "khipu.guides.v1";

export type GrokCheck = {
  ok: boolean;
  at: number;
  error?: string;
  status?: number | null;
  /** Address that was saved when this check ran; stale when it no longer matches. */
  address?: string;
};

export type GuidesPrefs = {
  /** The Home hint was closed with ×. */
  hintHidden: boolean;
  /** "Hide" on Start here: gone from Guides and from Home. */
  startHidden: boolean;
  /** "You're set up" has been shown and closed. */
  setUpSeen: boolean;
  /** Highest Grok Bot step the user said they did outside Khipu (0, 3 or 4). */
  grokAck: 0 | 3 | 4;
  /** The last "Check the gateway" answer for Grok Bot. */
  grokCheck: GrokCheck | null;
};

export const DEFAULT_PREFS: GuidesPrefs = {
  hintHidden: false,
  startHidden: false,
  setUpSeen: false,
  grokAck: 0,
  grokCheck: null,
};

export function parsePrefs(raw: unknown): GuidesPrefs {
  try {
    const p = (typeof raw === "string" ? JSON.parse(raw) : raw) as Partial<GuidesPrefs> | null;
    if (!p || typeof p !== "object") return DEFAULT_PREFS;
    const chk = p.grokCheck;
    return {
      hintHidden: p.hintHidden === true,
      startHidden: p.startHidden === true,
      setUpSeen: p.setUpSeen === true,
      grokAck: p.grokAck === 3 || p.grokAck === 4 ? p.grokAck : 0,
      grokCheck:
        chk && typeof chk.ok === "boolean" && typeof chk.at === "number"
          ? {
              ok: chk.ok,
              at: chk.at,
              error: typeof chk.error === "string" ? chk.error : undefined,
              status: typeof chk.status === "number" ? chk.status : null,
              address: typeof chk.address === "string" ? chk.address : undefined,
            }
          : null,
    };
  } catch {
    return DEFAULT_PREFS;
  }
}

export function prefsJson(prefs: GuidesPrefs): string {
  return JSON.stringify(prefs);
}

/** @deprecated Prefer the app-setting load in useGuideSignals; kept for tests. */
export function readPrefs(): GuidesPrefs {
  try {
    return parsePrefs(window.localStorage.getItem(LEGACY_STORE_KEY));
  } catch {
    return DEFAULT_PREFS;
  }
}

/** @deprecated Prefer the app-setting save in useGuideSignals; kept for tests. */
export function writePrefs(next: GuidesPrefs): void {
  try {
    window.localStorage.setItem(LEGACY_STORE_KEY, prefsJson(next));
  } catch {
    /* the state simply isn't remembered */
  }
}

/** A gateway check only counts while it still matches the saved address. */
export function liveGrokCheck(check: GuidesPrefs["grokCheck"], gatewayUrl: string): GuidesPrefs["grokCheck"] {
  if (!check) return null;
  if (typeof check.address !== "string" || check.address !== gatewayUrl.trim()) return null;
  return check;
}

// ---------------------------------------------------------------------------
// Grok Bot: five steps, three of which Khipu can observe.
// ---------------------------------------------------------------------------

export type GrokBotSignals = {
  /** The saved or in-force gateway address ("" when none). */
  gatewayUrl: string;
  /** A gateway key is saved on this Mac; null while that is still being read. */
  keySaved: boolean | null;
  check: GuidesPrefs["grokCheck"];
  ack: GuidesPrefs["grokAck"];
};

export type GrokBotStep = {
  n: 1 | 2 | 3 | 4 | 5;
  /** Khipu saw it: steps 1, 2 and 5 only. Never set by a click. */
  done: boolean;
  /** The user said they did it in Cursor (steps 3 and 4). It moves focus on
   *  and counts toward progress, but never draws a check mark. */
  acknowledged: boolean;
};

export type GrokBotProgress = {
  steps: GrokBotStep[];
  /** Steps passed, by evidence or by the user's word. */
  passed: number;
  /** The step the guide opens at. */
  current: 1 | 2 | 3 | 4 | 5;
  /** Anything done yet. */
  started: boolean;
  finished: boolean;
};

export function isHttpsAddress(url: string): boolean {
  return /^https:\/\/[^\s/]+/i.test(url.trim());
}

export function grokBotProgress(s: GrokBotSignals): GrokBotProgress {
  const check = liveGrokCheck(s.check, s.gatewayUrl);
  const s1 = isHttpsAddress(s.gatewayUrl);
  const s2 = s.keySaved === true;
  const s5 = check?.ok === true;
  const steps: GrokBotStep[] = [
    { n: 1, done: s1, acknowledged: false },
    { n: 2, done: s2, acknowledged: false },
    { n: 3, done: false, acknowledged: s.ack >= 3 },
    { n: 4, done: false, acknowledged: s.ack >= 4 },
    { n: 5, done: s5, acknowledged: false },
  ];
  const passed = steps.filter((x) => x.done || x.acknowledged).length;
  const firstOpen = steps.find((x) => !x.done && !x.acknowledged);
  // An https address (and even a key) is shared with Aegis / Another Mac;
  // Grok Bot setup has started only once the user ack'd a Cursor step or ran
  // a gateway check against the current address.
  const started = s.ack > 0 || check != null;
  return {
    steps,
    passed,
    current: (firstOpen?.n ?? 5) as GrokBotProgress["current"],
    started,
    finished: passed === 5,
  };
}

// ---------------------------------------------------------------------------
// Start here: four items ticked from live state.
// ---------------------------------------------------------------------------

export type StartSignals = {
  liveness: LivenessPayload | null;
  /** Grok Bot's gateway check passed (step 5 green). */
  gatewayCheckOk: boolean;
  /** Episodes in the hub, null while unknown. */
  episodes: number | null;
  /** Accounts that returned a usage reading. */
  accountsRead: number;
  /** The search log holds a call from an agent. */
  agentAsked: boolean;
};

export type StartItemId = "connect" | "session" | "accounts" | "recall";

export type StartItem = { id: StartItemId; label: string; done: boolean; why: string };

export function startItems(s: StartSignals): StartItem[] {
  const recording = Object.values(s.liveness?.harnesses ?? {}).some(
    (h) => h.ok !== false && h.seen === true && (h.captures ?? 0) > 0,
  );
  return [
    { id: "connect", label: "Connect your first agent", done: recording || s.gatewayCheckOk, why: "Install Khipu in an agent you use, or connect Grok Bot." },
    { id: "session", label: "See your first saved session", done: (s.episodes ?? 0) > 0, why: "Work in a connected agent for a few messages and Khipu saves the session." },
    { id: "accounts", label: "Add the AI accounts you pay for", done: s.accountsRead > 0, why: "Khipu can then show how much of each plan is left." },
    { id: "recall", label: "Ask your agent what it remembers", done: s.agentAsked, why: "Ask a connected agent what it remembers about your project." },
  ];
}

export function startProgress(items: StartItem[]): { done: number; total: number; next: StartItem | null } {
  const done = items.filter((i) => i.done).length;
  return { done, total: items.length, next: items.find((i) => !i.done) ?? null };
}

// ---------------------------------------------------------------------------
// Live state on each guide row, and the rail badge.
// ---------------------------------------------------------------------------

export type RowTone = "ok" | "warn" | "err" | "neutral";
export type GuideState = { tone: RowTone; label: string };

export function guideStates(
  s: StartSignals & { grok: GrokBotProgress; problems: number },
): Record<GuideId, GuideState> {
  const rec = (id: string): GuideState => {
    const lv = s.liveness?.harnesses?.[id];
    if (lv?.ok === false) return { tone: "err", label: "Stopped recording" };
    if (lv?.seen && (lv.captures ?? 0) > 0) return { tone: "ok", label: "Recording" };
    if (lv?.seen) return { tone: "warn", label: "Nothing saved yet" };
    return { tone: "neutral", label: "Not set up" };
  };
  const guide: GuideState = { tone: "neutral", label: "Guide" };
  const claude = rec("claude_code");
  return {
    claude_code: claude,
    codex: rec("codex"),
    cursor: rec("cursor"),
    aegis: rec("aegis"),
    grok_bot: s.grok.finished
      ? { tone: "ok", label: "Connected" }
      : s.grok.started
        ? { tone: "warn", label: `${s.grok.passed} of 5 steps` }
        : { tone: "neutral", label: "Not set up" },
    t3: { tone: "ok", label: "Covered" },
    other_cloud: guide,
    grok_build: { tone: "neutral", label: "Not supported yet" },
    add_account: s.accountsRead > 0
      ? { tone: "ok", label: s.accountsRead === 1 ? "1 account" : `${s.accountsRead} accounts` }
      : { tone: "neutral", label: "No accounts yet" },
    routing: guide,
    caps: guide,
    saving: guide,
    forget: guide,
    projects: guide,
    backups: guide,
    another_mac: guide,
    gateway: guide,
    red: s.problems > 0
      ? { tone: "err", label: s.problems === 1 ? "1 problem" : `${s.problems} problems` }
      : { tone: "ok", label: "No problems" },
  };
}

/** The rail badge: setups started and not finished, plus health problems.
 *  Never a count of guides merely unread. */
export function railBadge(grok: GrokBotProgress, problems: number): number {
  return (grok.started && !grok.finished ? 1 : 0) + Math.max(0, problems);
}

/** What Home's hint says: the next unfinished item, "You're set up" once, or
 *  nothing at all. */
export type HintState =
  | { kind: "hidden" }
  | { kind: "next"; item: StartItem; done: number; total: number }
  | { kind: "all-set" };

export function hintState(items: StartItem[], prefs: Pick<GuidesPrefs, "hintHidden" | "startHidden" | "setUpSeen">): HintState {
  if (prefs.hintHidden || prefs.startHidden) return { kind: "hidden" };
  const { done, total, next } = startProgress(items);
  if (next) return { kind: "next", item: next, done, total };
  return prefs.setUpSeen ? { kind: "hidden" } : { kind: "all-set" };
}
