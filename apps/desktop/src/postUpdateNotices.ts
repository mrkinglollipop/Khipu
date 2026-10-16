/** What's new after an update (settings clarity phase 8,
 * `docs/plans/2026-10-15-settings-clarity.md` section 5). Each version that
 * changed something a user should know about lists one or more items; most
 * versions add nothing here. On the first launch after an update the app
 * shows every item since the version this Mac last showed, once as a dialog
 * and once as a Home card that stays until it is closed there. Settings ->
 * Updates lists every version's items so they can be found again any time.
 */

/** Where an item's Show me button goes. `group` names a fold inside the
 * Settings section: the element carrying `data-settings-group="<group>"` is
 * opened and scrolled to when it exists. */
export type NoticeTarget =
  | { kind: "home" }
  | { kind: "harnesses" }
  | {
      kind: "settings";
      section:
        | "database"
        | "capture"
        | "routing"
        | "features"
        | "index"
        | "data"
        | "another-mac"
        | "components"
        | "updates"
        | "advanced";
      group?: string;
    }
  | { kind: "setup"; step: string };

/** A feature or place that only some builds have. An item whose `requires`
 * is missing from the build is left out everywhere; an item whose target is
 * missing shows no button. Add a capability to BUILD_CAPABILITIES in the
 * change that ships it.
 * - routing-overview: Settings "Accounts and routing", its folding groups and
 *   their `data-settings-group` anchors (settings clarity phase 3).
 * - plans-per-account: Plans as one block per account, pause shown (phase 2).
 * - rule-results: "Checked by Khipu" with the check's result (phase 5).
 * - capture-models-cards: Capture & models as three cards (phase 6).
 * - setup-accounts-step: setup's "Your accounts" step (phase 7). */
export type Capability =
  | "routing-overview"
  | "plans-per-account"
  | "rule-results"
  | "capture-models-cards"
  | "setup-accounts-step";

export const ALL_CAPABILITIES: ReadonlySet<Capability> = new Set<Capability>([
  "routing-overview",
  "plans-per-account",
  "rule-results",
  "capture-models-cards",
  "setup-accounts-step",
]);

/** What this build has. */
export const BUILD_CAPABILITIES: ReadonlySet<Capability> = new Set<Capability>([
  "routing-overview",
  "plans-per-account",
  "capture-models-cards",
  "setup-accounts-step",
  "rule-results",
]);

/** Setup steps every build has; any other step needs a capability. */
const BASE_SETUP_STEPS = ["welcome", "database", "model", "graph", "agents", "finish"];
const SETUP_STEP_CAPABILITY: Record<string, Capability> = { accounts: "setup-accounts-step" };

export function targetAvailable(target: NoticeTarget, caps: ReadonlySet<Capability>): boolean {
  if (target.kind === "settings") return !target.group || caps.has("routing-overview");
  if (target.kind === "setup") {
    if (BASE_SETUP_STEPS.includes(target.step)) return true;
    const cap = SETUP_STEP_CAPABILITY[target.step];
    return cap != null && caps.has(cap);
  }
  return true;
}

export type NoticeItem = {
  /** The feature this item describes; left out of builds without it. */
  requires?: Capability;
  title: string;
  /** One line in the dialog. */
  text: string;
  /** A shorter line for the Home card and Settings -> Updates; `text` when absent. */
  short?: string;
  target: NoticeTarget;
  /** "routing": when this Mac has no routing record or no account yet, the
   * button reads "Set up accounts" and opens setup at its Accounts step. */
  setUp?: "routing";
};

export type VersionNotice = {
  /** The app version these items arrived in. */
  version: string;
  items: NoticeItem[];
};

export const ACCOUNTS_SETUP: NoticeTarget = { kind: "setup", step: "accounts" };

export const POST_UPDATE_NOTICES: VersionNotice[] = [
  {
    version: "0.3.16",
    items: [
      {
        title: "Cursor users: re-install the Khipu pack once",
        text: "Cursor's rule file for each project refreshes only when you run Install again on the Harnesses screen.",
        short: "Run Install again once per project on the Harnesses screen.",
        target: { kind: "harnesses" },
      },
    ],
  },
  {
    version: "0.4.0",
    items: [
      {
        title: "Khipu has a new layout",
        text: "Six screens, one per job; health checks live on Home and nothing you set up has moved.",
        short: "Six screens, one per job; health checks live on Home.",
        target: { kind: "home" },
      },
    ],
  },
  {
    version: "0.4.2",
    items: [
      {
        title: "Setup and health checks were rebuilt",
        text: "Home runs the full health report on every launch, and every red row says what to do.",
        short: "Every red row on Home says what broke and what to do.",
        target: { kind: "home" },
      },
    ],
  },
  {
    version: "0.4.4",
    items: [
      {
        title: "Right now and Capture now",
        text: "Home shows the turns waiting for capture, and Capture now saves the current conversation.",
        short: "Turns waiting for capture, and a Capture now button.",
        target: { kind: "home" },
      },
    ],
  },
  {
    version: "0.4.5",
    items: [
      {
        title: "Recall tells current decisions from reversed ones",
        text: "A decision that was later reversed is marked, and forgetting a conversation removes what came from it.",
        short: "A reversed decision is marked as reversed.",
        target: { kind: "home" },
      },
    ],
  },
  {
    version: "0.4.7",
    items: [
      {
        title: "Optional features now have switches in Settings",
        text: "Each recall feature has a switch that says what it does and what it costs.",
        short: "Each feature has a switch with what it does and costs.",
        target: { kind: "settings", section: "features" },
      },
    ],
  },
  {
    version: "0.4.8",
    items: [
      {
        title: "Embeddings has its own screen in Settings",
        text: "Add a model, switch to one, or redo some of it, with an estimate first.",
        short: "Add a model, switch to one, or redo some of it.",
        target: { kind: "settings", section: "index" },
      },
    ],
  },
  {
    // The first version after 0.4.15, so these show whatever the release is
    // numbered.
    version: "0.4.16",
    items: [
      {
        requires: "routing-overview",
        title: "Accounts and routing",
        text: "Khipu tells your conversations which accounts have room.",
        target: { kind: "settings", section: "routing", group: "accounts" },
        setUp: "routing",
      },
      {
        title: "Priority and pace",
        text: "Say which account your agents try first; Khipu works out how fast each week is used.",
        short: "Say which account your agents try first.",
        target: { kind: "settings", section: "routing", group: "priority" },
      },
      {
        requires: "plans-per-account",
        title: "Plans",
        text: "Plans now names each account and says when you paused one.",
        short: "Plans names each account and says when you paused one.",
        target: { kind: "home" },
      },
      {
        requires: "capture-models-cards",
        title: "Capture & models",
        text: "Capture & models is shorter: summaries, keys and commitments each have one place.",
        short: "Shorter, with keys and commitments in one place each.",
        target: { kind: "settings", section: "capture" },
      },
      {
        requires: "rule-results",
        title: "Standing rules",
        text: "Each standing rule says whether it is checked by Khipu and what the check found.",
        short: "Each says whether it is checked by Khipu and what the check found.",
        target: { kind: "settings", section: "routing", group: "rules" },
      },
      {
        requires: "setup-accounts-step",
        title: "Setup",
        text: "Setup has a new Your accounts step.",
        short: "A new Your accounts step.",
        target: ACCOUNTS_SETUP,
      },
      {
        title: "Updates",
        text: "Settings → Updates lists everything new, by version.",
        short: "This list.",
        target: { kind: "settings", section: "updates" },
      },
      {
        requires: "plans-per-account",
        title: "Home",
        text: "Plan rows on Home say why a figure is missing.",
        short: "Plan rows say why a figure is missing.",
        target: { kind: "home" },
      },
    ],
  },
];

/** The dialog shows at most this many items; the rest are named by count. */
export const DIALOG_MAX_ITEMS = 5;
/** The Home card shows this many items with Show me. */
export const CARD_MAX_ITEMS = 3;

const STORAGE_KEY = "khipu.lastNoticedVersion";
/** The Home card's own state, kept apart from the dialog's so closing one
 * never closes or brings back the other. */
const CARD_KEY = "khipu.whatsNew.card";

/** localStorage may be unavailable (private mode, disabled site data); every
 * access here is best-effort and never throws. */
export function readLastNoticedVersion(): string {
  try {
    return window.localStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function writeLastNoticedVersion(version: string): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, version);
  } catch {
    // best effort: a missed write just means the dialog may show again
  }
}

/** The Home card: every item after `since` up to and including `version`. */
export type CardState = { since: string; version: string };

export function readCardState(): CardState | null {
  try {
    const raw = window.localStorage.getItem(CARD_KEY);
    if (!raw) return null;
    const v = JSON.parse(raw) as Partial<CardState>;
    return typeof v.since === "string" && typeof v.version === "string"
      ? { since: v.since, version: v.version }
      : null;
  } catch {
    return null;
  }
}

export function writeCardState(card: CardState | null): void {
  try {
    if (card) window.localStorage.setItem(CARD_KEY, JSON.stringify(card));
    else window.localStorage.removeItem(CARD_KEY);
  } catch {
    // best effort
  }
}

function parseVersion(v: string): number[] {
  return v.split(".").map((part) => parseInt(part, 10) || 0);
}

/** True when `a` is a strictly newer version than `b`. Plain numeric
 * dot-segment comparison: Khipu versions are always `MAJOR.MINOR.PATCH`. */
export function versionGreaterThan(a: string, b: string): boolean {
  const pa = parseVersion(a);
  const pb = parseVersion(b);
  const len = Math.max(pa.length, pb.length);
  for (let i = 0; i < len; i++) {
    const x = pa[i] ?? 0;
    const y = pb[i] ?? 0;
    if (x !== y) return x > y;
  }
  return false;
}

/** Every version's notice in (since, upTo], newest first. An empty `since`
 * means from the beginning. */
export function noticesBetween(
  since: string,
  upTo: string,
  caps: ReadonlySet<Capability> = BUILD_CAPABILITIES,
  notices: VersionNotice[] = POST_UPDATE_NOTICES,
): VersionNotice[] {
  return notices
    .map((n) => ({ ...n, items: n.items.filter((i) => !i.requires || caps.has(i.requires)) }))
    .filter(
      (n) =>
        n.items.length > 0 &&
        (!since || versionGreaterThan(n.version, since)) &&
        !versionGreaterThan(n.version, upTo),
    )
    .sort((a, b) => (versionGreaterThan(a.version, b.version) ? -1 : versionGreaterThan(b.version, a.version) ? 1 : 0));
}

/** The items in (since, upTo], newest version first, in each version's order. */
export function itemsBetween(
  since: string,
  upTo: string,
  caps: ReadonlySet<Capability> = BUILD_CAPABILITIES,
  notices: VersionNotice[] = POST_UPDATE_NOTICES,
): NoticeItem[] {
  return noticesBetween(since, upTo, caps, notices).flatMap((n) => n.items);
}

export type LaunchPlan = {
  /** The dialog to show now, or null. */
  dialog: { since: string; version: string; items: NoticeItem[] } | null;
  /** The Home card state to keep. */
  card: CardState | null;
};

/** What this launch shows. `storedVersion` is `readLastNoticedVersion()`'s
 * return and `card` is `readCardState()`'s.
 *
 * An EMPTY `storedVersion` is a fresh install, not an upgrade from version
 * zero: setup covers what the app does, so nothing shows (audit 2026-09-04).
 * A card still open from an earlier update keeps its older start, so nothing
 * it listed drops off before it is closed. */
export function planLaunch(
  storedVersion: string,
  currentVersion: string,
  card: CardState | null,
  caps: ReadonlySet<Capability> = BUILD_CAPABILITIES,
  notices: VersionNotice[] = POST_UPDATE_NOTICES,
): LaunchPlan {
  if (!storedVersion || !currentVersion || !versionGreaterThan(currentVersion, storedVersion)) {
    return { dialog: null, card };
  }
  const items = itemsBetween(storedVersion, currentVersion, caps, notices);
  if (items.length === 0) return { dialog: null, card };
  const since = card && versionGreaterThan(storedVersion, card.since) ? card.since : storedVersion;
  return {
    dialog: { since: storedVersion, version: currentVersion, items },
    card: { since, version: currentVersion },
  };
}

/** True when the routing item should offer setup instead of Show me: the
 * routing record was read and this Mac has no record or no account in it.
 * Before the first read it is not known, so Show me stays. */
export function routingNeedsSetup(view: { record?: boolean; accounts?: unknown[] } | null | undefined): boolean {
  if (!view) return false;
  return view.record === false || !view.accounts || view.accounts.length === 0;
}

/** The button for one item: its words and where it goes. */
export function itemAction(
  item: NoticeItem,
  needsRoutingSetup: boolean,
  caps: ReadonlySet<Capability> = BUILD_CAPABILITIES,
): { label: string; target: NoticeTarget } | null {
  if (item.setUp === "routing" && needsRoutingSetup) {
    // Set up accounts or nothing: Show me would land on an empty routing page.
    return targetAvailable(ACCOUNTS_SETUP, caps) ? { label: "Set up accounts", target: ACCOUNTS_SETUP } : null;
  }
  return targetAvailable(item.target, caps) ? { label: "Show me", target: item.target } : null;
}

/** The Home card as this version shows it: items after `since` up to the
 * older of the card's version and the running one, so a downgrade never
 * shows news from a newer build. Null when nothing is left to show. */
export function cardView(
  card: CardState,
  currentVersion: string,
  caps: ReadonlySet<Capability> = BUILD_CAPABILITIES,
): { since: string; version: string; items: NoticeItem[] } | null {
  if (!/^\d/.test(currentVersion)) return null;
  const upTo = versionGreaterThan(card.version, currentVersion) ? currentVersion : card.version;
  const items = itemsBetween(card.since, upTo, caps);
  return items.length > 0 ? { since: card.since, version: upTo, items } : null;
}

/** Raise the shown-version watermark, never lower it: after a downgrade and a
 * re-upgrade, news already dismissed stays dismissed. */
export function raiseLastNoticedVersion(version: string): void {
  const stored = readLastNoticedVersion();
  if (!stored || versionGreaterThan(version, stored)) writeLastNoticedVersion(version);
}

/** One launch: plan from what this Mac stored, keep the card, raise the
 * watermark. */
export function launchWhatsNew(currentVersion: string, caps: ReadonlySet<Capability> = BUILD_CAPABILITIES): LaunchPlan {
  const plan = planLaunch(readLastNoticedVersion(), currentVersion, readCardState(), caps);
  writeCardState(plan.card);
  raiseLastNoticedVersion(currentVersion);
  return plan;
}

/** Finishing setup covers what's new, so it records the running version.
 * Does nothing while the version is still unknown ("…" or "unknown"). */
export function recordVersionOnSetupFinish(appVersion: string): void {
  if (/^\d+\.\d+\.\d+/.test(appVersion)) raiseLastNoticedVersion(appVersion);
}
