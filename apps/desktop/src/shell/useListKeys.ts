// Up and Down (and Home and End) move through a list of buttons and pick
// each one as it is reached, the way a Mac list does. Spread the result on
// the list's container:
//   const keys = useListKeys();
//   <div {...keys}> ...buttons with class "row"... </div>
// Items are found with `itemSelector` (default: every enabled button in the
// container). The row the focus lands on is clicked, so a place needs no
// separate "selected" wiring for the keyboard.
import { useCallback } from "react";
import type { KeyboardEvent } from "react";

export function useListKeys(itemSelector = "button:not([disabled]):not([aria-disabled='true'])") {
  const onKeyDown = useCallback(
    (e: KeyboardEvent<HTMLElement>) => {
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp" && e.key !== "Home" && e.key !== "End") return;
      const container = e.currentTarget;
      const items = Array.from(container.querySelectorAll<HTMLElement>(itemSelector));
      const at = items.findIndex((el) => el === e.target || el.contains(e.target as Node));
      if (at < 0) return;
      const to =
        e.key === "ArrowDown" ? at + 1 : e.key === "ArrowUp" ? at - 1 : e.key === "Home" ? 0 : items.length - 1;
      const next = items[to];
      if (!next || to === at) return;
      e.preventDefault();
      next.focus();
      next.scrollIntoView?.({ block: "nearest" });
      next.click();
    },
    [itemSelector],
  );
  return { onKeyDown };
}
