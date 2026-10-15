import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/react";

// findBy*/waitFor poll a condition until it holds; their 1 s default is how
// long a stuck wait lasts before it fails, and a busy machine's first render
// of App can take longer. No test relies on a wait timing out.
configure({ asyncUtilTimeout: 15_000 });

// jsdom (as of the version this repo pins) does not implement the <dialog>
// element's imperative API — `ui.tsx`'s <Dialog> calls `showModal()`/`close()`
// on the native element for focus-trap/Escape/backdrop behavior "for free".
// Polyfill just enough of it (toggle the `open` attribute) so a dialog can be
// opened and closed under jsdom; nothing here needs real modality or focus
// trapping to assert on content and disabled state.
if (typeof HTMLDialogElement !== "undefined") {
  if (!HTMLDialogElement.prototype.showModal) {
    HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
      this.setAttribute("open", "");
    };
  }
  if (!HTMLDialogElement.prototype.close) {
    HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
      this.removeAttribute("open");
      this.dispatchEvent(new Event("close"));
    };
  }
}
