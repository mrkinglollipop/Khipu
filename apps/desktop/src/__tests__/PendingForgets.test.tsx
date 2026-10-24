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

describe("PendingForgets", () => {
  it("shows nothing when no forget is waiting", async () => {
    const { run, calls } = runner({ "episode forget-pending": [{ ok: true, count: 0 }] });
    const { container } = render(<PendingForgets run={run} />);
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(container).toBeEmptyDOMElement();
  });

  it("sends the waiting forgets from the app and reads the count again", async () => {
    const { run, calls } = runner({
      "episode forget-pending": [{ ok: true, count: 2 }, { ok: true, count: 0 }],
      "episode forget-pending --send --from-app": [{ ok: true, pending: 0 }],
    });
    const { container } = render(<PendingForgets run={run} />);
    const button = await screen.findByRole("button", { name: "Send 2 forgets to the hub" });
    expect(screen.getByText("2 forgets haven't reached the hub")).toBeInTheDocument();
    fireEvent.click(button);
    await waitFor(() => expect(container).toBeEmptyDOMElement());
    expect(calls).toContainEqual(["episode", "forget-pending", "--send", "--from-app"]);
  });

  it("says what is still waiting when the hub does not confirm them all", async () => {
    const { run } = runner({
      "episode forget-pending": [{ ok: true, count: 1 }],
      "episode forget-pending --send --from-app": [{ ok: false, pending: 1, error: "the hub refused" }],
    });
    render(<PendingForgets run={run} />);
    fireEvent.click(await screen.findByRole("button", { name: "Send 1 forget to the hub" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("the hub refused; 1 forget still waiting.");
    expect(screen.getByText("1 forget hasn't reached the hub")).toBeInTheDocument();
  });
});
