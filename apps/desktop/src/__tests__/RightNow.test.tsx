// D3: Home's "Right now" card — pending turns per live harness, queue depth,
// captured-today, and the Capture now button. Pure component test: no Tauri,
// no App.tsx — a fake LivenessPayload in, a click handler out.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { RightNowCard } from "../RightNow";
import type { LivenessPayload } from "../IntegrationsPanel";

afterEach(() => {
  cleanup();
});

describe("RightNowCard", () => {
  it("shows pending turns and last-capture age for a live harness", () => {
    const liveness: LivenessPayload = {
      ok: true,
      harnesses: {
        claude_code: {
          ok: true,
          seen: true,
          pending_turns: 3,
          last_captured_age_s: 240,
        },
      },
      queue_depth: 1,
      captured_today: 5,
    };
    render(
      <RightNowCard
        liveness={liveness}
        busy={false}
        message={null}
        onCaptureNow={() => {}}
      />,
    );
    expect(screen.getByText("Claude Code")).toBeInTheDocument();
    expect(
      screen.getByText(/3 turns waiting for capture · last capture 4m ago/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Queue depth 1 · captured today 5/),
    ).toBeInTheDocument();
  });

  it("shows 'nothing waiting' when a live harness has no pending turns", () => {
    const liveness: LivenessPayload = {
      ok: true,
      harnesses: {
        aegis: { ok: true, seen: true, pending_turns: 0, last_captured_age_s: null },
      },
      queue_depth: 0,
      captured_today: 0,
    };
    render(
      <RightNowCard
        liveness={liveness}
        busy={false}
        message={null}
        onCaptureNow={() => {}}
      />,
    );
    expect(
      screen.getByText(/nothing waiting · last capture never/),
    ).toBeInTheDocument();
  });

  it("shows the empty state when no harness has run a hook yet", () => {
    render(
      <RightNowCard
        liveness={null}
        busy={false}
        message={null}
        onCaptureNow={() => {}}
      />,
    );
    expect(screen.getByText("No live sessions yet")).toBeInTheDocument();
  });

  it("Capture now calls the handler and shows a busy label while pending", () => {
    const onCaptureNow = vi.fn();
    const { rerender } = render(
      <RightNowCard
        liveness={null}
        busy={false}
        message={null}
        onCaptureNow={onCaptureNow}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Capture now" }));
    expect(onCaptureNow).toHaveBeenCalledTimes(1);

    rerender(
      <RightNowCard
        liveness={null}
        busy={true}
        message={null}
        onCaptureNow={onCaptureNow}
      />,
    );
    expect(screen.getByRole("button", { name: "Capturing…" })).toBeDisabled();
  });

  it("shows the queued-capture message after Capture now returns", () => {
    render(
      <RightNowCard
        liveness={null}
        busy={false}
        message="Queued; lands at the end of the current turn."
        onCaptureNow={() => {}}
      />,
    );
    expect(
      screen.getByText("Queued; lands at the end of the current turn."),
    ).toBeInTheDocument();
  });
});
