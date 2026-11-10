import { IntegrationsPanel } from "../../IntegrationsPanel";
import type { SettingsCtx } from "../context";

/** The existing apps panel, with the same data and the same buttons. */
export function AppsPage({ ctx, active }: { ctx: SettingsCtx; active: boolean }) {
  return (
    <IntegrationsPanel
      runKhipu={ctx.runKhipu}
      onToast={ctx.onToast}
      active={active}
      liveness={ctx.liveness}
      recallProbe={ctx.recallProbe}
      t3={ctx.t3}
      refreshHealth={ctx.refreshHealth}
      onSearchT3Captures={ctx.onSearchT3Captures}
      onOpenGuide={ctx.onOpenGuide}
      onAnotherMac={() => ctx.onOpenOldSettings("another-mac")}
    />
  );
}
