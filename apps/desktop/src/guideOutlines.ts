/* Outline pages for every guide except Grok Bot (guides-scope.md §2). Each
   says what the thing is, the steps in order, and where the real control
   lives. The wording is the scope's outline; the full step-by-step copy for
   each is a later slice. */

import type { GuideId } from "./guidesModel";

export type GuideTarget =
  | { kind: "harnesses" }
  | { kind: "settings"; section: string }
  | { kind: "guide"; id: GuideId };

export type GuideOutline = {
  lede: string;
  steps: Array<{ title: string; text: string }>;
  opens?: Array<{ label: string; target: GuideTarget }>;
  /** A plain statement shown in a callout, for what is honestly missing. */
  note?: string;
};

export const OUTLINES: Partial<Record<GuideId, GuideOutline>> = {
  claude_code: {
    lede: "Khipu saves your Claude Code sessions and shows Claude what it remembered. T3 Code and extra Claude folders are covered here too.",
    steps: [
      { title: "Install in each Claude folder Khipu found", text: "On the Harnesses page, the Claude Code card lists every folder. Install each one you use." },
      { title: "Restart Claude Code", text: "It loads Khipu's hooks when it starts." },
      { title: "Start a session and send two messages", text: "Khipu saves a session after five messages or twenty minutes." },
      { title: "Watch the card turn green", text: "It turns green by itself once a session has run the hook." },
    ],
    opens: [{ label: "Open the Claude Code card", target: { kind: "harnesses" } }],
  },
  codex: {
    lede: "Khipu saves your Codex sessions. Codex keeps new hooks switched off until you review them inside Codex.",
    steps: [
      { title: "Install", text: "On the Harnesses page, use Install on the Codex card." },
      { title: "Approve Khipu's hooks inside Codex", text: "Open Codex. It asks you to review new hooks. Approve Khipu's." },
      { title: "Run a session", text: "Send a few messages." },
      { title: "Watch the card turn green", text: "It turns green by itself once the hook has run." },
    ],
    opens: [{ label: "Open the Codex card", target: { kind: "harnesses" } }],
  },
  cursor: {
    lede: "Khipu saves your Cursor sessions. Cursor has no per-message recall, which is why each project needs a memory rule.",
    steps: [
      { title: "Install", text: "On the Harnesses page, use Install on the Cursor card. This adds the global hooks." },
      { title: "Add memory rules to each project you use", text: "Today this step needs a terminal. A folder picker for it is coming." },
      { title: "Restart Cursor", text: "It loads the hooks when it starts." },
      { title: "Watch the card turn green", text: "It turns green by itself once a session has run the hook." },
    ],
    opens: [{ label: "Open the Cursor card", target: { kind: "harnesses" } }],
  },
  t3: {
    lede: "Nothing to do. The Claude Code install covers T3 Code. Khipu reads T3's settings and thread records and never changes them.",
    steps: [
      { title: "Install Khipu for Claude Code", text: "T3 sessions are saved through it." },
      { title: "Search by thread", text: "In Recall, filter to T3 captures to find a thread's sessions." },
    ],
    opens: [{ label: "Open the T3 Code card", target: { kind: "harnesses" } }],
  },
  aegis: {
    lede: "Aegis saves through a queue that this Mac drains, and it reaches your gateway with a key.",
    steps: [
      { title: "Install", text: "On the Harnesses page, use Install on the Aegis card." },
      { title: "Save the gateway key", text: "Paste it into the Gateway key card on the Harnesses page. It is never shown again." },
      { title: "Verify", text: "Use Verify on the card to run a real round trip." },
    ],
    opens: [{ label: "Open the Aegis card", target: { kind: "harnesses" } }],
  },
  other_cloud: {
    lede: "Any chat app that accepts a remote memory server (MCP) with a bearer header can reach your Khipu gateway the same way Grok Bot does.",
    steps: [
      { title: "Have a gateway address", text: "As in step 1 of the Grok Bot guide." },
      { title: "Copy your gateway key", text: "As in step 2 of the Grok Bot guide." },
      { title: "Add a remote memory server in that app's settings", text: "Use your gateway address followed by /mcp, with the header Authorization: Bearer and your key." },
      { title: "Check the gateway", text: "As in step 5 of the Grok Bot guide." },
    ],
    opens: [{ label: "Open the Grok Bot guide", target: { kind: "guide", id: "grok_bot" } }],
    note: "Which apps accept a static bearer header is not verified yet. Apps that require OAuth for custom connectors cannot use the gateway as built.",
  },
  grok_build: {
    lede: "Grok Build has no Khipu connection of its own yet.",
    steps: [
      { title: "If you also use Aegis", text: "Grok Build sessions are saved under Aegis." },
    ],
    note: "Not supported yet.",
  },
  add_account: {
    lede: "Khipu reads how much of each AI plan you've used so it can tell your agents which accounts have room.",
    steps: [
      { title: "Choose the account type", text: "Plans you sign in to, and services you pay for by key, are added differently." },
      { title: "Sign in or paste a key", text: "Khipu reads usage; it never starts or stops your work." },
      { title: "See a reading", text: "Success looks like 'Khipu read 72% left'." },
    ],
    opens: [{ label: "Open Accounts and routing", target: { kind: "settings", section: "routing" } }],
  },
  routing: {
    lede: "Khipu tells your agents which accounts have room and what you prefer. It never starts, stops or moves work.",
    steps: [
      { title: "Set your priority", text: "The order you'd like accounts used in." },
      { title: "Say what each account is for", text: "Your own words for each account." },
      { title: "Add rules and a note", text: "Khipu shows them to agents. It doesn't enforce them." },
      { title: "Change any of it by asking a conversation", text: "That is owner direction, and it needs your confirmation." },
    ],
    opens: [{ label: "Open Accounts and routing", target: { kind: "settings", section: "routing" } }],
  },
  caps: {
    lede: "A cap keeps an account from running out. A pause holds it until a date. An exception allows something for a few days and always ends.",
    steps: [
      { title: "Cap", text: "The most an account should be used before agents are told to look elsewhere." },
      { title: "Pause until a date", text: "Hold an account out of use for a set time." },
      { title: "Exceptions end", text: "Every exception has an end date, so none pile up." },
    ],
    opens: [{ label: "Open Accounts and routing", target: { kind: "settings", section: "routing" } }],
  },
  saving: {
    lede: "Khipu saves a session after five messages or twenty minutes, before compacting, and at the end. Cloud agents save only when they choose to.",
    steps: [
      { title: "What gets saved", text: "A short summary, the decisions, and what you prefer, for each session." },
      { title: "What your agent sees", text: "At the start and with each message, the top three relevant memories, short." },
      { title: "Where to look", text: "Activity shows every saved session. Recall searches them." },
    ],
    opens: [{ label: "Open Capture & models", target: { kind: "settings", section: "capture" } }],
  },
  forget: {
    lede: "Forget hides a saved session from every agent.",
    steps: [
      { title: "Open the session", text: "In Activity or Recall." },
      { title: "Choose Forget and confirm", text: "It stops appearing in search and recall, and its vectors are deleted. The row itself is kept, so this is not a shred." },
      { title: "Agents can't forget on their own", text: "They can forget only when you confirm." },
    ],
  },
  projects: {
    lede: "Khipu groups sessions into projects by name, and keeps a list of what you still owe.",
    steps: [
      { title: "Projects", text: "Merge two names that are one project, or keep a pair apart." },
      { title: "Owed", text: "Follow-ups, blockers and promises Khipu found in your sessions. Close one when it's done." },
      { title: "Work", text: "What is waiting on you right now." },
    ],
    opens: [{ label: "Open Projects", target: { kind: "settings", section: "projects" } }],
  },
  backups: {
    lede: "Khipu keeps a copy of its data folder, a database backup, an off-site graph copy, and an offline copy on this Mac.",
    steps: [
      { title: "Data folder backup", text: "Save one from Settings → Data & backups." },
      { title: "Database backup", text: "Goes red after about 36 hours. A button to run it again is coming." },
      { title: "Off-site graph copy", text: "Needs a terminal today." },
    ],
    opens: [{ label: "Open Data & backups", target: { kind: "settings", section: "data" } }],
  },
  another_mac: {
    lede: "Move Khipu to another Mac with a join kit.",
    steps: [
      { title: "On this Mac, save a join kit", text: "Pick a passphrase." },
      { title: "AirDrop it to the other Mac", text: "" },
      { title: "Open it on the new Mac", text: "Welcome walks through it." },
    ],
    opens: [{ label: "Open Another Mac", target: { kind: "settings", section: "another-mac" } }],
  },
  gateway: {
    lede: "The gateway is the one part of Khipu that cloud agents reach over the internet. Your database stays private.",
    steps: [
      { title: "Who needs one", text: "Only cloud agents, such as Grok Bot, and Aegis." },
      { title: "Run it on a server you control", text: "It needs a terminal today." },
      { title: "Bring back two things", text: "The https address and the key." },
    ],
    opens: [{ label: "Open the gateway address", target: { kind: "settings", section: "another-mac" } }],
    note: "Setting up a gateway happens outside Khipu today and needs a terminal.",
  },
  red: {
    lede: "Every health check on Home has a plain meaning and one fix.",
    steps: [
      { title: "Open Home", text: "A red row says what's wrong." },
      { title: "Use the row's button", text: "Most problems fix with one click. Some just need a wait." },
    ],
  },
};
