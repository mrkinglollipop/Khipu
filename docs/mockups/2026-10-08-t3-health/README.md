# Harnesses: a T3 Code card (mock, approved 2026-10-08)

Static mock for showing Khipu-in-T3 health in the desktop app. It follows slices A to C of
`docs/plans/2026-10-07-khipu-t3.md`. Open either `.html` in a browser; it follows the system appearance.
`App.css` is a copy of the app stylesheet; `mock.css` holds the mock-only layout.

- `t3-card-ok.html`: T3 found, thread lookup working, captures linked to threads.
- `t3-card-lookup-broken.html`: T3's thread records changed shape. The card and the rail warn. The copy
  says only what Khipu can still know: when it last linked a capture to a thread.

Decisions in the mock:

- One T3 Code card, full width, above Claude Code, because T3 runs both Claude and Codex. Hooks stay
  installed per Claude home in the Claude Code card; the T3 card links to them rather than repeating them.
- The card only appears when T3 is installed (its settings file exists).
- It warns when the thread lookup fails, or when T3 has been used in the last day (its database
  changed) but no capture was linked to a thread. It never claims a capture came from T3 when the
  lookup can't say.
- Backed by a doctor check with the same facts, so `khipu doctor` and the app agree.
- Recheck reruns the lookup. Search T3 captures opens Recall filtered to `via: t3`.
