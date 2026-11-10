import { IntegrationsPanel } from "../../IntegrationsPanel";
import { OwnerActions } from "../../OwnerActions";
import type { SettingsCtx } from "../context";

/** The existing apps panel, with the same data and the same buttons. */
export function AppsPage({ ctx, active, onAnotherMac }: { ctx: SettingsCtx; active: boolean; onAnotherMac: () => void }) {
  return (
    <>
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
      onAnotherMac={onAnotherMac}
    />
    {/* The older Settings' "Owner actions" section, unchanged: whether agents may make
        the changes you ask them for, and which apps they may do it from. */}
    <section className="st-group" aria-label="Changes agents make for you">
      <OwnerActions active={active} />
    </section>
    </>
  );
}
