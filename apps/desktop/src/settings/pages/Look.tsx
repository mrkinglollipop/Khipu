// Settings › Look. Two controls: Light / Dark / Follow this Mac, and whether
// Khipu opens when this Mac starts. Both are about this Mac only.
import { useEffect, useId, useState } from "react";
import { disable, enable, isEnabled } from "@tauri-apps/plugin-autostart";
import { Segmented } from "../../ui";
import { readLook, saveLook, useMacIsDark } from "../lookModel";
import type { Look } from "../lookModel";

const OPTIONS = [
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
  { value: "system", label: "Follows this Mac" },
] as const;

/** `part` lets Settings show each card as its own row (Look; Open at login). */
export function LookPage({ macName = null, part = "both" }: { macName?: string | null; part?: "both" | "look" | "opening" }) {
  const [look, setLook] = useState<Look>(readLook);
  const macDark = useMacIsDark();
  const mac = macName ?? "this Mac";
  const [login, setLogin] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const id = useId().replace(/:/g, "");

  useEffect(() => {
    let live = true;
    isEnabled().then(
      (v) => live && setLogin(v),
      () => live && setErr("Khipu couldn't check whether it opens when this Mac starts."),
    );
    return () => {
      live = false;
    };
  }, []);

  function choose(next: Look) {
    setLook(next);
    saveLook(next);
  }

  async function toggleLogin(want: boolean) {
    setBusy(true);
    setErr(null);
    try {
      if (want) await enable();
      else await disable();
      setLogin(await isEnabled());
    } catch {
      setErr(`Khipu couldn't change this. Your Mac still ${want ? "won't" : "will"} open Khipu at start.`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="look-page">
      {part !== "opening" ? <div className="section-card">
        <div className="section-head">Look <small>Only on {mac}</small></div>
        <div className="section-body">
          <Segmented ariaLabel="Colours" options={OPTIONS} value={look} onChange={choose} />
          <p className="muted" aria-live="polite">
            {look === "system"
              ? `Following ${mac}, which is ${macDark ? "dark" : "light"} right now.`
              : `Always ${look}. Only on ${mac}.`}
          </p>
        </div>
      </div> : null}
      {part !== "look" ? <div className="section-card">

        <div className="section-body">
          <div className="opt-row">
            <input
              id={`${id}-login`}
              className="switch"
              type="checkbox"
              role="switch"
              checked={login === true}
              disabled={login === null || busy}
              aria-describedby={`${id}-what`}
              onChange={(e) => void toggleLogin(e.target.checked)}
            />
            <div className="opt-text">
              <div className="opt-title">
                <label className="opt-name" htmlFor={`${id}-login`}>
                  {`Open Khipu when ${mac} starts`}
                </label>
              </div>
              <p id={`${id}-what`} className="opt-what">
                Only on {mac}. Turn it off here at any time.
              </p>
            </div>
          </div>
          {err ? (
            <p className="muted" role="alert">
              {err}
            </p>
          ) : null}
        </div>
      </div> : null}
    </div>
  );
}
