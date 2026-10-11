import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { FeedbackForm } from "../FeedbackForm";

const invokeMock = vi.fn();

vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));
vi.mock("@tauri-apps/plugin-dialog", () => ({
  open: vi.fn(async () => "/tmp/not-approved.txt"),
}));

afterEach(() => {
  cleanup();
  invokeMock.mockReset();
});

describe("FeedbackForm", () => {
  it("sends only paths approved by the Rust attachment picker", async () => {
    invokeMock.mockImplementation(async (command: string) => {
      if (command === "select_feedback_attachments") return ["/tmp/approved.txt"];
      if (command === "feedback_file_sizes") return [42];
      if (command === "send_feedback") return null;
      throw new Error(`unexpected command: ${command}`);
    });
    render(<FeedbackForm open appVersion="0.4.11" onClose={() => {}} onSendingChange={() => {}} returnFocusRef={{ current: null }} />);

    fireEvent.click(screen.getByRole("button", { name: "Attach files" }));
    expect(await screen.findByText("approved.txt")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Reply-to email"), { target: { value: "me@example.com" } });
    fireEvent.change(document.querySelector("textarea.feedback-message")!, { target: { value: "Please help" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(invokeMock).toHaveBeenCalledWith("send_feedback", {
      replyTo: "me@example.com", message: "Please help", appVersion: "0.4.11", paths: ["/tmp/approved.txt"],
    }));
  });
});
