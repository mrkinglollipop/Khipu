// Offline callback probe for the desktop-audit regressions. It executes the
// production callback bodies without a Tauri webview; run from this app with
// `node --test scripts/desktop-audit-callback-probe.mjs`.
import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import { stripTypeScriptTypes } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const app = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const source = (name) => fs.readFileSync(path.join(app, "src", name), "utf8");
const between = (text, start, end) => {
  const first = text.indexOf(start);
  assert.notEqual(first, -1, `missing ${start}`);
  const last = text.indexOf(end, first);
  assert.notEqual(last, -1, `missing ${end}`);
  return text.slice(first, last);
};
const dataModule = (text) => import(`data:text/javascript;base64,${Buffer.from(stripTypeScriptTypes(text, { mode: "strip" })).toString("base64")}`);

test("provisional proposal Confirm sends the stable rule entity", async () => {
  const settings = source("RoutingSettings.tsx");
  const target = between(settings, "function ruleTarget(", "/** A standing rule");
  const proposal = between(settings, "function ProposalRowView(", "function ProposalsSection(");
  const confirm = between(proposal, "  const confirm = () =>", "  const dismiss = () =>");
  const mod = await dataModule(`${target}
export async function drive() {
  const calls = [];
  const p = { kind: "rule" };
  const rule = { n: null, number_pending: true, entity: "rule:pending-1", revision: "shown-revision" };
  const ruleMissing = false;
  const confirmId = "proposal:confirm:p1";
  const expect = null;
  const refuse = async () => false;
  const attempt = async (...args) => { calls.push(args); return true; };
${confirm}
  await confirm();
  return calls;
}`);
  assert.deepEqual(await mod.drive(), [["proposal:confirm:p1", "khipu_route_rule", { action: "confirm", entity: "rule:pending-1", expect: "shown-revision" }]]);
});

test("end-or-keep retries the same rule action with the collected map", async () => {
  const settings = source("RoutingSettings.tsx");
  const eok = between(settings, "function useEndOrKeep(", "function EndOrKeepChoices(");
  const holdChoiceIds = between(source("routingProposals.ts"), "export function holdChoiceIds(", "export type HoldChoiceTarget");
  const mod = await dataModule(`
let asked = null;
const useState = (value) => [value, (next) => { asked = typeof next === "function" ? next(asked) : next; }];
${holdChoiceIds}
${eok}
export async function drive() {
  const calls = [];
  let cleanup = 0;
  let refusal = "say end or keep in holds for: h1";
  const w = {
    run: async (...args) => { calls.push(args); return calls.length > 1; },
    errorOf: () => refusal,
  };
  const flow = useEndOrKeep(w);
  if (await flow.attempt("rule:edit", "khipu_route_rule", { action: "edit", n: 3, text: "local" }, null, undefined, () => { cleanup += 1; }) !== false) throw new Error("first write unexpectedly passed");
  if (asked.ids.join(",") !== "h1") throw new Error("missing hold id");
  if (await asked.retry({ h1: "keep" }) !== true) throw new Error("retry did not pass");
  return { calls, cleanup };
}`);
  const result = await mod.drive();
  assert.equal(result.cleanup, 1);
  assert.deepEqual(result.calls, [
    ["rule:edit", "khipu_route_rule", { action: "edit", n: 3, text: "local" }, null, undefined],
    ["rule:edit", "khipu_route_rule", { action: "edit", n: 3, text: "local", holds: { h1: "keep" } }, null, undefined],
  ]);
});

test("end-or-keep retries the same displayed confirmation revision with the collected map", async () => {
  const settings = source("RoutingSettings.tsx");
  const eok = between(settings, "function useEndOrKeep(", "function EndOrKeepChoices(");
  const holdChoiceIds = between(source("routingProposals.ts"), "export function holdChoiceIds(", "export type HoldChoiceTarget");
  const mod = await dataModule(`
let asked = null;
const useState = (value) => [value, (next) => { asked = typeof next === "function" ? next(asked) : next; }];
${holdChoiceIds}
${eok}
export async function drive() {
  const calls = [];
  let cleanup = 0;
  let refusal = "say end or keep in holds for: h1";
  const w = {
    run: async (...args) => { calls.push(args); return calls.length > 1; },
    errorOf: () => refusal,
  };
  const flow = useEndOrKeep(w);
  if (await flow.attempt("rule:confirm", "khipu_route_rule", { action: "confirm", n: 3, expect: "shown-revision" }, null, undefined, () => { cleanup += 1; }) !== false) throw new Error("first write unexpectedly passed");
  if (asked.ids.join(",") !== "h1") throw new Error("missing hold id");
  if (await asked.retry({ h1: "keep" }) !== true) throw new Error("retry did not pass");
  return { calls, cleanup };
}`);
  const result = await mod.drive();
  assert.equal(result.cleanup, 1);
  assert.deepEqual(result.calls, [
    ["rule:confirm", "khipu_route_rule", { action: "confirm", n: 3, expect: "shown-revision" }, null, undefined],
    ["rule:confirm", "khipu_route_rule", { action: "confirm", n: 3, expect: "shown-revision", holds: { h1: "keep" } }, null, undefined],
  ]);
});

test("Home priority Confirm retries with the chosen holds", async () => {
  const card = source("RoutingCard.tsx");
  const holds = between(source("routingProposals.ts"), "export function holdChoiceIds(", "export type HoldChoiceTarget");
  const priority = between(card, "function PriorityRow(", "function plural(");
  const act = between(priority, "  const act = async", "  return (");
  const mod = await dataModule(`${holds}
export async function drive() {
  const calls = [];
  const p = { version: "p7" };
  let busy = null;
  const setBusy = (value) => { busy = typeof value === "function" ? value(busy) : value; };
  const setError = () => {};
  const setHoldIds = () => {};
  const setHolds = () => {};
  const callCli = async (_command, args) => {
    calls.push(args);
    return calls.length === 1 ? { ok: false, error: "say end or keep in holds for: h1" } : { ok: true };
  };
  const reload = async () => {};
${act}
  await act("confirm");
  await act("confirm", { h1: "keep" });
  return calls;
}`);
  assert.deepEqual(await mod.drive(), [
    { action: "confirm", expect: "p7" },
    { action: "confirm", expect: "p7", holds: { h1: "keep" } },
  ]);
});

test("successful single Owed writes refresh every list and Work", async () => {
  const appSource = source("App.tsx");
  const owedWrite = between(appSource, "  const owedWrite = useCallback(", "  /** Home's one fix action");
  const mod = await dataModule(`
const useCallback = (fn) => fn;
export async function drive() {
  const lists = [];
  let workLoads = 0;
  const setOwedBusyId = () => {};
  const setOwedSaved = () => {};
  const setError = () => {};
  const runKhipu = async () => JSON.stringify({ ok: true });
  const parseJson = JSON.parse;
  const loadOwed = async (...args) => { lists.push(args); };
  const work = { load: async (fresh) => { if (fresh !== true) throw new Error("Work did not receive a forced reload"); workLoads += 1; } };
${owedWrite}
  await owedWrite(41, ["--close=41"]);
  return { lists, workLoads };
}`);
  assert.deepEqual(await mod.drive(), { lists: [["open", true], ["closed", true], ["stale", true]], workLoads: 1 });
});
