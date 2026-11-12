import type { ReactNode } from "react";
// What App hands Settings: state it already owns (models, key presence, the
// doctor-derived evidence the apps panel reads) plus a few jumps. Nothing here
// is new data; the old Settings read the same values.
import type { useModelVerify } from "../modelVerify";
import type { SecretsPresence } from "../secretsPresence";
import type { DoctorSynthProvider, SynthRow } from "../SynthProvider";
import type { LivenessPayload, RecallProbeStatus, T3Health } from "../IntegrationsPanel";
import type { GuideId } from "../guidesModel";

export type SettingsCtx = {
  appVersion: string;
  /** "M5" / "M1", or null while the app has no name for this Mac. */
  thisMac: string | null;
  runKhipu: (args: string[]) => Promise<string>;
  onToast: (msg: string) => void;
  // Apps that use Khipu (IntegrationsPanel, unchanged)
  liveness: LivenessPayload | null;
  recallProbe: RecallProbeStatus | null;
  t3: T3Health | null;
  refreshHealth: () => Promise<string | null | void> | void;
  onSearchT3Captures: () => void;
  onOpenGuide: (id: GuideId) => void;
  // Models and API keys (App's models state and its save)
  synth: SynthRow;
  modelsError: string | null;
  modelsSaving: boolean;
  modelsMsg: string | null;
  onSynthChange: (patch: Partial<SynthRow>) => void;
  onSaveModels: () => Promise<void> | void;
  secretsPresence: SecretsPresence | null;
  secretsPresenceMsg: string | null;
  modelCheck: ReturnType<typeof useModelVerify>;
  onKeySaved: () => Promise<void> | void;
  doctorSynth: DoctorSynthProvider | null;
  // Where memory is kept
  dsnOk: boolean | null;
  dbReachable: boolean | null;
  onMoveMemory: () => void;
  // Help
  onOpenGuides: () => void;
  onFeedback: () => void;
  /** "Connect to a different database…": the first-run flow at its Database step. */
  onDatabaseStep: () => void;
  /** "Run setup again": the first-run flow from the start. */
  onRunSetup: () => void;
  // Your Macs: setting up another Mac (the join kit, moved from the older Settings)
  anotherMac?: ReactNode;
  // Updates: what's new in this version (the card and its history, moved from the old Home and Settings)
  whatsNew?: ReactNode;
  /** A deep link into one row (notices, guides, the Apps page's "another Mac"). */
  openRow?: { id: string; n: number } | null;
};
