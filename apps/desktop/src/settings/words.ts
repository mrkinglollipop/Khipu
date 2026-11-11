// Plain words for the Settings rows and pages.
import { fmtAge } from "../time";
import type { MacRow, MacsDoc } from "./data";
import { macName } from "./data";

export function seenWords(m: MacRow): string {
  if (m.this_mac) return "checked just now";
  if (m.age_s == null) return "last seen at an unknown time";
  return m.age_s < 90 ? "seen just now" : `seen ${fmtAge(m.age_s)} ago`;
}

export function savingWords(m: MacRow): string {
  if (m.saving === true) return "saving normally";
  if (m.saving === false) return "not saving right now";
  return "saving not known yet";
}

/** One line per Mac for the Your Macs page. A stale row says so in words. */
export function macLine(m: MacRow): string {
  const parts = [m.version ? `Khipu ${m.version}` : "version not known"];
  if (m.stale) {
    parts.push(`last heard from ${m.age_s == null ? "a while ago" : `${fmtAge(m.age_s)} ago`}, so this may be out of date`);
  } else {
    parts.push(savingWords(m), seenWords(m));
  }
  return parts.join(" · ");
}

export function macsSummary(doc: MacsDoc | null): string {
  if (!doc) return "Reading…";
  const macs = doc.macs;
  if (macs.length === 0) return "Not known yet";
  const names = macs.map(macName);
  const stale = macs.filter((m) => m.stale);
  if (stale.length) return `${names.join(" and ")} · ${macName(stale[0])} not heard from lately`;
  if (macs.length === 1) return `${names[0]} · no other Mac yet`;
  const versions = new Set(macs.map((m) => m.version));
  return `${names.join(" and ")} · ${versions.size === 1 ? "same version" : "different versions"}`;
}

export function signinsSummary(doc: MacsDoc | null): string {
  if (!doc) return "Reading…";
  const me = doc.macs.find((m) => m.this_mac);
  const list = me?.signins;
  if (!list || list.length === 0) return "Not known yet";
  const out = list.filter((s) => !s.signed_in);
  if (out.length === 0) return `All ${list.length} accounts signed in`;
  return out.length === 1 ? `${out[0].label} needs signing in` : `${out.length} accounts need signing in`;
}
