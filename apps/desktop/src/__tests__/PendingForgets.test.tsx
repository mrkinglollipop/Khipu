import { describe, expect, it } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { PendingForgets } from "../PendingForgets";

function runner(answers: Record<string, unknown[]>) {
  const calls: string[][] = [];
  const run = async (args: string[]) => {
    calls.push(args);
    const queue = answers[args.join(" ")] ?? [];
    return JSON.stringify(queue.length > 1 ? queue.shift() : queue[0] ?? {});
  };
  return { run, calls };
}

const NONE = { ok: true, strings: [], ids: [], count: 0 };

describe("PendingForgets", () => {
  it("shows nothing when no forget is listed", async () => {
    const { run, calls } = runner({ "episode forget-pending --from-app": [NONE] });
    const { container } = render(<PendingForgets run={run} />);
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(container).toBeEmptyDOMElement();
  });

  it("sends the listed forgets from the app and reads the list again", async () => {
    const { run, calls } = runner({
      "episode forget-pending --from-app": [{ ok: true, strings: ["k:aa", "h:bb"], ids: [], count: 2 }, NONE],
      "episode forget-pending --send --from-app": [{ ok: true, pending: 0 }],
    });
    const { container } = render(<PendingForgets run={run} />);
    const button = await screen.findByRole("button", { name: "Send 2 forgets to the hub" });
    expect(screen.getByText("2 forgets haven't reached the hub")).toBeInTheDocument();
    fireEvent.click(button);
    await waitFor(() => expect(container).toBeEmptyDOMElement());
    expect(calls).toContainEqual(["episode", "forget-pending", "--send", "--from-app"]);
  });

  it("says what is still listed when the hub does not take them all", async () => {
    const { run } = runner({
      "episode forget-pending --from-app": [{ ok: true, strings: ["k:aa"], ids: [], count: 1 }],
      "episode forget-pending --send --from-app": [{ ok: false, pending: 1, error: "not sent: the hub refused" }],
    });
    render(<PendingForgets run={run} />);
    fireEvent.click(await screen.findByRole("button", { name: "Send 1 forget to the hub" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("not sent: the hub refused.");
    expect(screen.getByText("1 forget hasn't reached the hub")).toBeInTheDocument();
  });

  it("offers both answers for a forgotten episode number the hub still holds", async () => {
    const listed = { ok: true, strings: [], ids: [{ id: 42, summary: "An unrelated hub episode" }], count: 1 };
    const { run, calls } = runner({
      "episode forget-pending --from-app": [listed, listed, NONE],
      "episode forget-pending --forget-here 42 --from-app": [{ ok: false, error: "forget not done" }],
      "episode forget-pending --not-same 42 --from-app": [{ ok: true }],
    });
    const { container } = render(<PendingForgets run={run} />);
    expect(await screen.findByText(/An unrelated hub episode/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /to the hub/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Forget episode 42 here" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("forget not done.");
    fireEvent.click(screen.getByRole("button", { name: "Not the same episode" }));
    await waitFor(() => expect(container).toBeEmptyDOMElement());
    expect(calls).toContainEqual(["episode", "forget-pending", "--forget-here", "42", "--from-app"]);
    expect(calls).toContainEqual(["episode", "forget-pending", "--not-same", "42", "--from-app"]);
  });
});
