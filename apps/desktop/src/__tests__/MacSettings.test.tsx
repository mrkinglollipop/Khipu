// Settings › Your Macs, the parts moved or added in slice A11: naming each Mac
// (`khipu_config_set` routing_mac_names), the warning when two Macs share a
// name, the settings the Macs disagree on and the copy switch (the older
// routing screen's commands, unchanged), and Add another Mac.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

const calls: Array<{ cmd: string; args?: Record<string, unknown> }> = [];
let config: Record<string, unknown> = {};
let refuse: string | null = null;

vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(async (cmd: string, args?: Record<string, unknown>) => {
    calls.push({ cmd, args });
    if (cmd === "khipu_config_show") return JSON.stringify(config);
    if (cmd === "khipu_config_set" || cmd === "khipu_owner_answer") {
      if (refuse) return JSON.stringify({ ok: false, error: refuse });
      return JSON.stringify({ ok: true });
    }
    return "{}";
  }),
}));

import { YourMacsPage } from "../settings/pages/YourMacs";
import type { MacRow, MacsDoc } from "../settings/data";

const M5 = "0123456789abcdef0123456789abcdef";
const M1 = "fedcba9876543210fedcba9876543210";
const mac = (id: string, name: string, here: boolean): MacRow => ({
  mac: id, name, this_mac: here, version: "0.4.15", seen_at: "x", age_s: 0, stale: false,
  saving: true, health: null, running: null, signins: null,
});
const loaded = (macs: MacRow[]) => ({ data: { macs } as MacsDoc, error: null, loading: false });
const writes = () => calls.filter((c) => c.cmd !== "khipu_config_show");

beforeEach(() => {
  calls.length = 0;
  refuse = null;
  config = { routing: { routing_mac_names: { value: {} }, routing_replication_live: { value: true } }, settings_sync: { enabled: true } };
});
afterEach(cleanup);

describe("Your Macs: names", () => {
  it("says plainly when two Macs show the same name, and points at the name field", async () => {
    render(<YourMacsPage macs={loaded([mac(M5, "MacBook-Pro", true), mac(M1, "MacBook-Pro", false)])} />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Two of your Macs are called “MacBook-Pro”");
    fireEvent.click(within(alert).getByRole("button", { name: "Name the other Mac" }));
    expect(screen.getByRole("textbox", { name: /Your other Mac/ })).toHaveFocus();
  });

  it("gives no warning when the names differ", async () => {
    render(<YourMacsPage macs={loaded([mac(M5, "M5", true), mac(M1, "M1", false)])} />);
    await screen.findByRole("textbox", { name: /This Mac/ });
    expect(screen.queryByText(/of your Macs are called/)).toBeNull();
  });

  it("saves this Mac's name with the existing settings command and keeps the other Mac's name", async () => {
    config = { routing: { routing_mac_names: { value: { [M1]: "M1" } } } };
    const named = vi.fn();
    render(<YourMacsPage macs={loaded([mac(M5, "MacBook-Pro", true), mac(M1, "M1", false)])} onNamed={named} />);
    const field = await screen.findByRole("textbox", { name: /This Mac/ });
    await waitFor(() => expect(field).toBeEnabled());
    fireEvent.change(field, { target: { value: "  M5 " } });
    fireEvent.click(screen.getAllByRole("button", { name: "Save name" })[0]);
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0]).toEqual({ cmd: "khipu_config_set", args: { key: "routing_mac_names", value: JSON.stringify({ [M1]: "M1", [M5]: "M5" }) } });
    await waitFor(() => expect(named).toHaveBeenCalledTimes(1));
  });

  it("names the other Mac the same way, and clearing a name removes only that one", async () => {
    config = { routing: { routing_mac_names: { value: { [M5]: "M5", [M1]: "Old" } } } };
    render(<YourMacsPage macs={loaded([mac(M5, "M5", true), mac(M1, "Old", false)])} />);
    const other = await screen.findByRole("textbox", { name: /Your other Mac/ });
    await waitFor(() => expect(other).toHaveValue("Old"));
    fireEvent.change(other, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Clear name" }));
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0].args).toEqual({ key: "routing_mac_names", value: JSON.stringify({ [M5]: "M5" }) });
  });

  it("shows a refusal in words and does not claim the name was saved", async () => {
    refuse = "routing_mac_names: a Mac name must be one line of at most 64 characters";
    const named = vi.fn();
    render(<YourMacsPage macs={loaded([mac(M5, "MacBook-Pro", true)])} onNamed={named} />);
    const field = await screen.findByRole("textbox", { name: /This Mac/ });
    await waitFor(() => expect(field).toBeEnabled());
    fireEvent.change(field, { target: { value: "M5" } });
    fireEvent.click(screen.getByRole("button", { name: "Save name" }));
    expect(await screen.findByText(refuse)).toBeInTheDocument();
    expect(named).not.toHaveBeenCalled();
  });

  it("never writes on load or while typing", async () => {
    render(<YourMacsPage macs={loaded([mac(M5, "MacBook-Pro", true), mac(M1, "MacBook-Pro", false)])} />);
    const field = await screen.findByRole("textbox", { name: /This Mac/ });
    fireEvent.change(field, { target: { value: "M5" } });
    await new Promise((r) => setTimeout(r, 20));
    expect(writes()).toEqual([]);
  });
});

describe("Your Macs: what the Macs disagree on (moved from the older routing screen)", () => {
  it("offers Keep and Use for each difference with the old command and values", async () => {
    config = {
      routing: { routing_busy_load: { value: 75, kind: "count" } },
      settings_sync: { enabled: true, differences: [{ key: "routing_busy_load", name: "Wait while the Mac is busy", local: 75, remote: 90 }] },
    };
    render(<YourMacsPage macs={loaded([mac(M5, "M5", true), mac(M1, "M1", false)])} />);
    const group = await screen.findByRole("group", { name: "Wait while the Mac is busy" });
    expect(group).toHaveTextContent("This Mac: 75 · Other Mac: 90");
    fireEvent.click(within(group).getByRole("button", { name: "Use 90" }));
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0]).toEqual({ cmd: "khipu_config_set", args: { key: "routing_busy_load", value: "90" } });
  });

  it("sends on and off, the words the bridge accepts, for a switch: Keep, Use and Use the other Mac's for all", async () => {
    config = {
      routing: { routing_pace_order: { value: true, kind: "switch" }, routing_push_prompt: { value: false, kind: "switch" } },
      settings_sync: { enabled: true, differences: [
        { key: "routing_pace_order", name: "Order by pace", local: true, remote: false },
        { key: "routing_push_prompt", name: "Tell conversations", local: false, remote: true },
      ] },
    };
    render(<YourMacsPage macs={loaded([mac(M5, "M5", true), mac(M1, "M1", false)])} />);
    const first = await screen.findByRole("group", { name: "Order by pace" });
    fireEvent.click(within(first).getByRole("button", { name: "Keep on" }));
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0]).toEqual({ cmd: "khipu_config_set", args: { key: "routing_pace_order", value: "on" } });
    fireEvent.click(within(first).getByRole("button", { name: "Use off" }));
    await waitFor(() => expect(writes()).toHaveLength(2));
    expect(writes()[1]).toEqual({ cmd: "khipu_config_set", args: { key: "routing_pace_order", value: "off" } });
    fireEvent.click(screen.getByRole("button", { name: "Use the other Mac’s for all" }));
    await waitFor(() => expect(writes()).toHaveLength(4));
    expect(writes().slice(2)).toEqual([
      { cmd: "khipu_config_set", args: { key: "routing_pace_order", value: "off" } },
      { cmd: "khipu_config_set", args: { key: "routing_push_prompt", value: "on" } },
    ]);
  });

  it("answers a change another Mac holds with the old owner answer", async () => {
    config = { settings_sync: { enabled: true, waiting: [{ id: "r1", request: "r1", mac: "M1", ask: "Pause Claude?", answerable: true }] } };
    render(<YourMacsPage macs={loaded([mac(M5, "M5", true), mac(M1, "M1", false)])} />);
    const group = await screen.findByRole("group", { name: "M1" });
    fireEvent.click(within(group).getByRole("button", { name: "No" }));
    await waitFor(() => expect(writes()).toEqual([{ cmd: "khipu_owner_answer", args: { request: "r1", answer: "no" } }]));
  });

  it("turns the copy between Macs off with the old switch", async () => {
    render(<YourMacsPage macs={loaded([mac(M5, "M5", true)])} />);
    const sw = await screen.findByRole("checkbox", { name: /Copy your account order/ });
    await waitFor(() => expect(sw).toBeChecked());
    fireEvent.click(sw);
    await waitFor(() => expect(writes()).toEqual([{ cmd: "khipu_config_set", args: { key: "routing_replication_live", value: "off" } }]));
  });
});

describe("Your Macs: Add another Mac", () => {
  it("shows the join section even when the list of Macs could not be read", () => {
    render(<YourMacsPage macs={{ data: null, error: "no answer", loading: false }} anotherMac={<p>JOIN-KIT</p>} />);
    expect(screen.getByRole("heading", { name: "Add another Mac" })).toBeInTheDocument();
    expect(screen.getByText("JOIN-KIT")).toBeInTheDocument();
  });
});
