// Settings › Look: which colours the window uses. Stored on this Mac only (in
// this window's own storage, never shared with another Mac). No stored choice
// means "follow the Mac": the CSS then obeys the Mac's own appearance and
// reacts when it changes. A stored choice sets `data-theme` on the root, and
// App.css makes the same tokens follow it.
import { useEffect, useState } from "react";

export type Look = "light" | "dark" | "system";

export const LOOK_KEY = "khipu.look";

export function readLook(): Look {
  try {
    const v = window.localStorage.getItem(LOOK_KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

/** Put the choice on the root element. "system" removes the override. */
export function applyLook(look: Look): void {
  const root = document.documentElement;
  if (look === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", look);
}

export function saveLook(look: Look): void {
  try {
    if (look === "system") window.localStorage.removeItem(LOOK_KEY);
    else window.localStorage.setItem(LOOK_KEY, look);
  } catch {
    /* storage blocked: the choice still applies until the window closes */
  }
  applyLook(look);
}

/** Run once at start, before the first paint, so a saved choice never flashes. */
export function applyStoredLook(): void {
  applyLook(readLook());
}

const QUERY = "(prefers-color-scheme: dark)";

/** True while the Mac itself is in dark appearance; follows changes. */
export function useMacIsDark(): boolean {
  const [dark, setDark] = useState(() => (typeof window.matchMedia === "function" ? window.matchMedia(QUERY).matches : false));
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia(QUERY);
    const on = () => setDark(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return dark;
}
