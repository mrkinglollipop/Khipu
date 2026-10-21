/* Settings -> Accounts and routing as an overview that folds (settings
   clarity phase 3, docs/plans/2026-10-15-settings-clarity.md section 3; mocks
   docs/mockups/2026-10-15-settings-clarity/routing.html and
   routing-states.html). Pure: what each group's closed line says, and every
   problem on the page, each counted once from one source. The Needs you list,
   the closed lines' tags and Home's attention items read the same sources
   (`brokenRules`, `quietRunners` in routing.ts). */

import { accountClassification, brokenRules, quietRunners, rateWords, readWhen, ruleWords, runnerWords, whenLocal } from "./routing";
import type { PlanFact, RouteProvider, RouteView } from "./routing";
import { checksLine, checksModel } from "./routingChecks";
import { keptWindow, passingGapHeld, planStatus } from "./PlansCard";
import type { UsageAccount, UsageReport } from "./PlansCard";

export type GroupKey = "accounts" | "priority" | "rules" | "holds" | "checks" | "telling" | "resets" | "runners" | "table" | "changes";

/** The groups in page order. A link from elsewhere names one by its key
 *  (`data-settings-group`); the what's-new items use accounts, priority and
 *  rules. `record`: the group needs the routing record. */
export const GROUPS: ReadonlyArray<{ key: GroupKey; name: string; record: boolean }> = [
  { key: "accounts", name: "Accounts", record: true },
  { key: "priority", name: "Priority and pace", record: true },
  { key: "rules", name: "Your note and standing rules", record: true },
  { key: "holds", name: "Holds", record: true },
  { key: "checks", name: "Checks and copies", record: true },
  { key: "telling", name: "Telling conversations", record: false },
  { key: "resets", name: "Checking for resets", record: false },
  { key: "runners", name: "Providers and lanes", record: true },
  { key: "table", name: "Old routing table file", record: false },
  { key: "changes", name: "Recent changes", record: true },
];

export type Tone = "warn" | "err";

/** One problem: where it lives, the kind its group's tag counts it under, and,
 *  when the owner can act on it, the Needs you line and the item it points at
 *  (`data-route-item`). */
export type Problem = {
  key: string;
  group: GroupKey;
  kind: string;
  tone: Tone;
  needs?: string;
  /** What Show opens, for its accessible name ("Show the Claude second disagreement"). */
  show?: string;
  item?: string;
};

export type GroupTag = { text: string; tone: Tone };

/** The settings the closed lines need, from `khipu config --show` and `khipu features`. */
export type OverviewSettings = {
  /** null: not read yet. */
  routing: Record<string, { value: unknown; default: unknown }> | null;
  routingError?: string | null;
  /** routing_push as `khipu features` reports it; null: not read yet. */
  push: { enabled?: boolean } | null;
  pushError?: string | null;
};

export type Overview = {
  lines: Record<GroupKey, string>;
  tags: Record<GroupKey, GroupTag[]>;
  problems: Problem[];
};

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/** The routing record's "out of date after" setting in seconds (0: never),
 *  its default until the settings are read. */
export function staleAfterSeconds(s: OverviewSettings): number {
  const v = s.routing?.routing_stale_after?.value;
  return typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : 12 * 3600;
}


function laneMap(s: OverviewSettings): Record<string, Record<string, string>> {
  const raw = s.routing?.routing_lane_map?.value;
  return raw && typeof raw === "object" && !Array.isArray(raw) ? (raw as Record<string, Record<string, string>>) : {};
}

/** Every lane with no account, once each: a lane Khipu read with no match in
 *  the lane map, a lane matched to a name that is no account Khipu knows, or
 *  a limit that came from a lane not matched (one lane is one problem,
 *  whichever of these says so). */
export function lanesWithoutAccount(view: RouteView, s: OverviewSettings): { runner: string; lane: string }[] {
  const map = laneMap(s);
  const out: { runner: string; lane: string }[] = [];
  const add = (runner: string, lane: string) => {
    if (!out.some((x) => x.runner === runner && x.lane === lane)) out.push({ runner, lane });
  };
  if (s.routing) {
    for (const r of view.sources?.runners ?? []) for (const lane of r.lanes ?? []) if (!map[r.name]?.[lane] && !r.lane_matches?.[lane]?.account) add(r.name, lane);
    // Matched to a name Khipu doesn't know: the lane row says so in warn.
    const known = (t: string) => (view.accounts ?? []).some((a) => a.label === t || a.key === t);
    for (const [runner, lanes] of Object.entries(map)) {
      for (const [lane, target] of Object.entries(lanes ?? {})) if (target && !known(target)) add(runner, lane);
    }
  }
  for (const u of view.unmatched ?? []) add(u.runner, u.lane);
  return out;
}

/** One provider in the Providers and lanes group: the owner's facts (slice
 *  9a) over the built-in ones an account's harness names. */
export type ProviderRow = {
  id: string;
  label: string;
  family: string;
  /** The adapter that reads its plan: a definition's `probe`, or the
   *  built-in's; null when Khipu knows neither. */
  probe: string | null;
  models_allowed?: string[];
  models_never?: string[];
  use?: string;
  /** The account labels Khipu knows on this provider. */
  accounts: string[];
};

/** The built-ins, as the app words them. A provider with a definition takes
 *  its label, family and probe from there instead. */
const BUILTIN_PROVIDERS: Record<string, { label: string; family: string; probe: string }> = {
  claude: { label: "Claude", family: "Anthropic", probe: "claude" },
  codex: { label: "Codex", family: "OpenAI", probe: "codex" },
  grok: { label: "Grok", family: "xAI", probe: "grok" },
  glm: { label: "GLM", family: "Zhipu", probe: "none" },
  muse: { label: "Muse", family: "Meta", probe: "none" },
};

/** The mock's order (docs/mockups/2026-10-21-routing/lanes-light.html);
 *  providers it doesn't name come after, by label. */
const PROVIDER_ORDER = ["claude", "codex", "grok", "glm", "deepseek", "muse", "antigravity"];

/** Every provider Khipu can say something about: one row per id, whether the
 *  owner defined it or only an account's harness names it. */
export function providerRows(view: RouteView): ProviderRow[] {
  const rows: ProviderRow[] = [];
  const add = (id: string, def?: RouteProvider) => {
    const builtin = BUILTIN_PROVIDERS[id];
    const accounts = (view.accounts ?? [])
      .filter((a) => a.harness === id || a.key.startsWith(`${id}:`))
      .map((a) => a.label);
    if (!def && !builtin && !accounts.length) return;
    if (rows.some((r) => r.id === id)) return;
    rows.push({
      id,
      label: def?.label ?? builtin?.label ?? id,
      family: def?.family ?? builtin?.family ?? "",
      probe: def?.probe ?? builtin?.probe ?? null,
      models_allowed: def?.models_allowed,
      models_never: def?.models_never,
      use: def?.use,
      accounts,
    });
  };
  const definitions = view.definitions?.providers ?? {};
  for (const id of Object.keys(definitions)) add(id, definitions[id]);
  for (const a of view.accounts ?? []) if (a.harness) add(a.harness);
  const rank = (r: ProviderRow) => {
    const at = PROVIDER_ORDER.indexOf(r.id);
    return at === -1 ? PROVIDER_ORDER.length : at;
  };
  return rows.sort((a, b) => rank(a) - rank(b) || a.label.localeCompare(b.label));
}

/** Every problem on the page (scope rule 1), each from one source. */
export function routingProblems(
  view: RouteView,
  plans: UsageReport | null | undefined,
  plansError: string | null | undefined,
  s: OverviewSettings,
  now: Date,
): Problem[] {
  const out: Problem[] = [];
  for (const a of view.accounts ?? []) {
    if (a.state === "limited") out.push({ key: `limit:${a.key}`, group: "accounts", kind: "limit", tone: "warn" });
    if (a.state === "down") out.push({ key: `down:${a.key}`, group: "accounts", kind: "down", tone: "err" });
    // Open, but a model has its own limit: the account's dot is warn.
    if (a.state === "open" && (a.models ?? []).some((m) => m.until && new Date(m.until).getTime() > now.getTime())) {
      out.push({ key: `model-limit:${a.key}`, group: "accounts", kind: "model-limit", tone: "warn" });
    }
    // As the account's disagreement panel shows them: its own, and each
    // model's while that model's limit lasts.
    const modelDisagrees = (a.models ?? []).some(
      (m) => m.disagreement && m.until && new Date(m.until).getTime() > now.getTime(),
    );
    if (a.disagreement || modelDisagrees) {
      out.push({ key: `disagree:${a.key}`, group: "accounts", kind: "disagree", tone: "warn",
        needs: `${a.label}: two sources disagree`, show: `the ${a.label} disagreement`, item: `account:${a.key}` });
    }
  }
  for (const row of plans?.accounts ?? []) {
    const st = planStatus(row, now);
    if (!st.code || st.code === "pending") continue;
    // A passing gap with recent figures kept is no problem (the shared rule
    // Home uses); kept figures past the stale setting make it one.
    if (passingGapHeld(row, staleAfterSeconds(s), now)) continue;
    const name = row.account || row.harness || "An account";
    const signIn = st.code === "not_signed_in";
    const routed = (view.accounts ?? []).some((a) => row.key != null && a.key === row.key);
    out.push({
      key: `plan:${row.key ?? name}`,
      group: "accounts",
      kind: signIn ? "signin" : "plan",
      tone: "warn",
      needs: signIn
        ? `${name}: not signed in`
        : st.code === "no_plan_limits"
          ? `${name}: its sign-in reports no plan limits`
          : `${name}: Khipu can't read its plan`,
      show: name,
      item: routed ? `account:${row.key}` : `plan:${row.key ?? name}`,
    });
  }
  if (plansError && !plans) out.push({ key: "plans-read", group: "accounts", kind: "plans-read", tone: "warn" });
  if (view.priority?.pending) {
    out.push({ key: "priority", group: "priority", kind: "priority", tone: "warn",
      needs: "A conversation changed your priority: confirm or undo it", show: "the priority to confirm", item: "priority:pending" });
  }
  const toConfirm = (view.rules ?? []).filter((r) => r.pending || (r.seeded && !r.confirmed));
  for (const r of toConfirm) out.push({ key: `confirm:${r.n}`, group: "rules", kind: "confirm", tone: "warn" });
  if (toConfirm.length) {
    // One Needs you line for all of them, pointing at the first.
    const first = out.find((p) => p.kind === "confirm")!;
    first.needs = toConfirm.length === 1 ? "1 standing rule to confirm" : `${toConfirm.length} standing rules to confirm`;
    first.show = toConfirm.length === 1 ? "the standing rule to confirm" : "the standing rules to confirm";
    first.item = `rule:${toConfirm[0].n}`;
  }
  for (const { rule, checks } of brokenRules(view)) {
    const said = (checks[0]?.text ?? "").replace(/^A standing rule may be broken: /, "").replace(/\.$/, "");
    const r = (view.rules ?? []).find((x) => x.n === rule);
    out.push({ key: `broken:${rule}`, group: "rules", kind: "broken", tone: "warn",
      needs: `Standing rule ${rule} may be broken${said ? `: ${said}` : r ? `: it says ${ruleWords(r.text)}` : ""}`,
      show: `standing rule ${rule}`, item: `rule:${rule}` });
  }
  for (const h of view.holds ?? []) {
    if (h.remind) {
      out.push({ key: `hold:${h.id}`, group: "holds", kind: "remind", tone: "warn",
        needs: `Your hold on ${h.scope_label} is still on: is it still needed?`, show: `the hold on ${h.scope_label}`, item: `hold:${h.id}` });
    }
  }
  for (const r of quietRunners(view)) {
    out.push({ key: `runner:${r.name}`, group: "runners", kind: r.silent && !r.unreadable ? "silent" : "unreadable",
      tone: "err", needs: runnerWords(r, now).replace(/\.$/, ""), show: `the ${r.name} job runner`, item: `runner:${r.name}` });
  }
  for (const name of view.sources?.refused ?? []) {
    out.push({ key: `refused:${name}`, group: "runners", kind: "refused", tone: "err",
      needs: `Khipu refused the ${name} folder: it is inside a private folder`, show: `the ${name} folder`, item: `runner:${name}` });
  }
  for (const { runner, lane } of lanesWithoutAccount(view, s)) {
    const match = (view.sources?.runners ?? []).find((r) => r.name === runner)?.lane_matches?.[lane];
    // A lane whose declared account its launcher contradicts: the lane row
    // says how, so it counts under its own tag, not Needs you.
    if (match?.reason === "launcher evidence contradicts the declaration") {
      out.push({ key: `lane-conflict:${runner}/${lane}`, group: "runners", kind: "conflict", tone: "warn",
        item: `lane:${runner}/${lane}` });
      continue;
    }
    const n = match?.candidates.length ?? 0;
    out.push({ key: `lane:${runner}/${lane}`, group: "runners", kind: "lane", tone: "warn",
      needs: n >= 2
        ? `Lane ${lane} in ${runner} matches ${n === 2 ? "two accounts" : `${n} accounts`}`
        : `Lane ${lane} in ${runner} has no account`,
      show: `lane ${lane}`, item: `lane:${runner}/${lane}` });
  }
  if (view.sources?.table?.error) out.push({ key: "table", group: "table", kind: "table", tone: "warn" });
  // Advisory contradictions need the owner to choose which line holds, so
  // each gets its own Needs you line; rule upkeep is an advisory mark only.
  for (const row of checksModel(view, now).contradictions) {
    out.push({ key: `check:${row.key}`, group: "checks", kind: "contradiction", tone: "warn",
      needs: row.title, show: row.title, item: `check:${row.key}` });
  }
  return out;
}

const TAG_WORDS: Record<string, (n: number) => string> = {
  limit: (n) => (n === 1 ? "1 at its limit" : `${n} at their limit`),
  down: (n) => `${n} down`,
  "model-limit": (n) => (n === 1 ? "1 with a model at its limit" : `${n} with a model at its limit`),
  disagree: (n) => (n === 1 ? "Sources disagree" : `Sources disagree on ${n}`),
  plan: (n) => (n === 1 ? "1 plan can't be read" : `${n} plans can't be read`),
  signin: (n) => `${n} not signed in`,
  "plans-read": () => "Plans couldn't be read",
  priority: () => "To confirm",
  confirm: (n) => `${n} to confirm`,
  broken: (n) => `${n} may be broken`,
  remind: (n) => plural(n, "hold to look at", "holds to look at"),
  silent: (n) => `${n} stopped reporting`,
  unreadable: (n) => `${n} can't be read`,
  refused: (n) => `${n} refused`,
  lane: (n) => plural(n, "lane without an account", "lanes without an account"),
  conflict: (n) => plural(n, "lane conflicts", "lanes conflict"),
  table: () => "Can't read the file",
  contradiction: (n) => plural(n, "contradiction", "contradictions"),
};

/** A group's problem tags: one per kind, counted. */
export function groupTags(problems: Problem[], group: GroupKey): GroupTag[] {
  const kinds: string[] = [];
  for (const p of problems) if (p.group === group && !kinds.includes(p.kind)) kinds.push(p.kind);
  return kinds.map((k) => {
    const of = problems.filter((p) => p.group === group && p.kind === k);
    return { text: (TAG_WORDS[k] ?? ((n: number) => `${n} to look at`))(of.length), tone: of.some((p) => p.tone === "err") ? "err" : "warn" };
  });
}

function accountsLine(view: RouteView): string {
  const accounts = view.accounts ?? [];
  if (!accounts.length) return "No accounts yet";
  const count = (classification: string) => accounts.filter((a) => accountClassification(a) === classification).length;
  const parts: string[] = [];
  const open = count("open");
  const probablyOpen = count("probably_open");
  const limited = count("limited");
  const paused = count("paused");
  const down = count("down");
  const unknown = count("unknown");
  if (open) parts.push(`${open} open`);
  if (probablyOpen) parts.push(`${probablyOpen} probably open`);
  if (limited) parts.push(limited === 1 ? "1 at its limit" : `${limited} at their limit`);
  if (paused) parts.push(`${paused} paused`);
  if (down) parts.push(`${down} down`);
  if (unknown) parts.push(`${unknown} unknown`);
  return `${plural(accounts.length, "account", "accounts")}: ${parts.join(", ")}`;
}

function priorityLine(view: RouteView, s: OverviewSettings): string {
  const entries = view.priority?.entries ?? [];
  if (entries.length === 1) return `${entries[0].label} first`;
  if (entries.length) return `${entries[0].label} first, ${entries.slice(1).map((e) => `then ${e.label}`).join(", ")}`;
  if (!s.routing) return "No priority set";
  return s.routing.routing_pace_order?.value === false ? "No priority set" : "No priority set; accounts listed by pace";
}

function rulesLine(view: RouteView): string {
  const note = view.note;
  const at = note?.at ? new Date(note.at) : null;
  const noteWords = note
    ? at && !Number.isNaN(at.getTime())
      ? `Note from ${at.toLocaleDateString([], { month: "short", day: "numeric" })}`
      : "Your note"
    : "No note";
  return `${noteWords} · ${plural((view.rules ?? []).length, "rule", "rules")}`;
}

function holdsLine(view: RouteView, now: Date): string {
  const holds = view.holds ?? [];
  if (!holds.length) return "None";
  const next = holds
    .map((h) => h.until)
    .filter((u): u is string => !!u && !Number.isNaN(new Date(u).getTime()))
    .sort((a, b) => new Date(a).getTime() - new Date(b).getTime())[0];
  const head = plural(holds.length, "hold", "holds");
  if (!next) return `${head}, each until you clear it`;
  return `${head}, the ${holds.length === 1 ? "hold" : "next"} ends ${whenLocal(next, now)}`;
}

function tellingLine(s: OverviewSettings): string {
  if (s.pushError || s.routingError) return "Couldn't read its settings";
  if (!s.push || !s.routing) return "Reading…";
  if (s.push.enabled !== true) return "Off";
  const on = (k: string) => s.routing?.[k]?.value === true;
  const moments = [
    on("routing_push_start") ? "at the start" : null,
    on("routing_push_prompt") ? "before each prompt" : null,
    on("routing_push_delegation") ? "before handing work on" : null,
  ].filter((x): x is string => !!x);
  if (!moments.length) return "On, but every moment is off";
  const last = moments.pop()!;
  return `On: ${moments.length ? `${moments.join(", ")} and ${last}` : last}`;
}

function resetsLine(s: OverviewSettings): string {
  if (s.routingError) return "Couldn't read its settings";
  if (!s.routing) return "Reading…";
  if (s.routing.routing_reset_check?.value !== true) return "Off";
  const raw = s.routing.routing_reset_check_retries?.value;
  const retries = Array.isArray(raw) ? raw.filter((v) => typeof v === "number" && v > 0).length : 0;
  return `On: up to ${plural(1 + retries, "check", "checks")} after each reset`;
}

function runnersLine(view: RouteView, s: OverviewSettings): string {
  if (s.routingError) return "Couldn't read its settings";
  if (!s.routing) return "Reading…";
  const raw = s.routing.routing_sources?.value;
  const folders = Array.isArray(raw) ? raw.length : 0;
  const map = laneMap(s);
  let lanes = 0;
  const names = new Set([...(view.sources?.runners ?? []).map((r) => r.name), ...Object.keys(map)]);
  for (const name of names) {
    const r = (view.sources?.runners ?? []).find((x) => x.name === name);
    lanes += new Set([...(r?.lanes ?? []), ...Object.keys(map[name] ?? {})]).size;
  }
  if (!folders && !lanes) return "No job runner folders";
  return `${plural(providerRows(view).length, "provider", "providers")} · ${plural(folders, "folder", "folders")}, ${plural(lanes, "lane", "lanes")}`;
}

function tableLine(s: OverviewSettings): string {
  if (s.routingError) return "Couldn't read its settings";
  if (!s.routing) return "Reading…";
  const raw = s.routing.routing_table?.value;
  if (typeof raw !== "string" || !raw) return "No file chosen";
  return `${raw.split("/").pop() || raw}, read only`;
}

function changesLine(view: RouteView, now: Date): string {
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  let n = 0;
  let any = false;
  for (const c of view.changes ?? []) {
    const items = c.items?.length || c.entities?.length || 0;
    if (items) any = true;
    const at = new Date(c.at).getTime();
    if (!Number.isNaN(at) && at >= today) n += items;
  }
  if (!any) return "No changes yet";
  return n ? `${plural(n, "change", "changes")} today` : "No changes today";
}

/** The closed lines and tags for every group. `view` null: the record is not
 *  read yet (or its read failed: `recordError`). */
export function routingOverview(
  view: RouteView | null,
  recordError: string | null | undefined,
  plans: UsageReport | null | undefined,
  plansError: string | null | undefined,
  s: OverviewSettings,
  now: Date,
): Overview {
  const problems = view ? routingProblems(view, plans, plansError, s, now) : [];
  const lines = {} as Record<GroupKey, string>;
  const tags = {} as Record<GroupKey, GroupTag[]>;
  for (const g of GROUPS) tags[g.key] = groupTags(problems, g.key);
  const unread = recordError ? "Not read" : "Reading…";
  lines.accounts = view ? accountsLine(view) : unread;
  lines.priority = view ? priorityLine(view, s) : unread;
  lines.rules = view ? rulesLine(view) : unread;
  lines.holds = view ? holdsLine(view, now) : unread;
  lines.checks = view ? checksLine(view, now) : unread;
  lines.telling = tellingLine(s);
  lines.resets = resetsLine(s);
  lines.runners = view ? runnersLine(view, s) : unread;
  lines.table = tableLine(s);
  lines.changes = view ? changesLine(view, now) : unread;
  for (const g of GROUPS) {
    if (!view && recordError && g.record) tags[g.key] = [{ text: "Couldn't read", tone: "err" }];
  }
  return { lines, tags, problems };
}

/** An account line's week and pace, in two parts: "83% of its week left,
 *  resets Sat 3:00 PM" over "About 1.8% an hour to use it all (a lot left,
 *  little time)", or why there is no pace. */
export function accountWeek(
  fact: PlanFact | undefined,
  now: Date,
  plan?: UsageAccount,
  staleAfterS: number = 12 * 3600,
): { week: string | null; pace: string | null } {
  // A passing gap keeps the plan read's last figures, with Home's words for
  // why they are not from this read.
  if (!fact?.readable && plan && passingGapHeld(plan, staleAfterS, now)) {
    const k = keptWindow(plan, now);
    if (k) {
      const reset = k.resets_at ? `, resets ${whenLocal(k.resets_at, now)}` : "";
      return { week: `${k.left}% of its ${k.label} left${reset}`, pace: planStatus(plan, now).say };
    }
  }
  if (!fact) return { week: null, pace: null };
  if (!fact.readable) {
    const why = fact.why ?? "";
    if (/no window with a reset time/.test(why)) return { week: "Its plan shows no limit that resets", pace: "So there is no pace to work out" };
    if (fact.reset_passed) return { week: "Its reset time passed; figures out of date", pace: "No pace until Khipu reads its plan again" };
    if (/not read yet/.test(why)) return { week: null, pace: "No pace until Khipu reads its plan" };
    return { week: null, pace: "No pace until Khipu can read its plan" };
  }
  if (fact.stale) {
    return {
      week: `${fact.left_pct}% of its ${fact.window} left ${readWhen(fact, now)}, resets ${whenLocal(fact.resets_at, now)}`,
      pace: "These figures may be out of date, so no pace is given",
    };
  }
  const week = `${fact.left_pct}% of its ${fact.window} left, resets ${whenLocal(fact.resets_at, now)}`;
  if (!fact.left_pct || fact.left_pct <= 0) return { week, pace: "Nothing left to use until then" };
  return { week, pace: `About ${rateWords(fact.pace_pct_per_hour ?? 0)}% an hour to use it all (${fact.words})` };
}

const ACCOUNT_TAG: Record<string, string> = {
  disagree: "Sources disagree",
  signin: "Not signed in",
};

/** The tags on one account's line: its problems that point at it (the same
 *  problems the Accounts tags count), never a second count of them. */
export function accountTags(problems: Problem[], key: string): GroupTag[] {
  return problems
    .filter((p) => p.item === `account:${key}` && ACCOUNT_TAG[p.kind])
    .map((p) => ({ text: ACCOUNT_TAG[p.kind], tone: p.tone }));
}
