# Harnesses: one row per Claude home (mock, awaiting approval)

Static mock for slice A of `docs/plans/2026-10-07-khipu-t3.md`: the Harnesses screen lists every Claude
home Khipu finds (`~/.claude`, `CLAUDE_CONFIG_DIR`, each Claude instance in T3's settings). Open either
`.html` in a browser; it follows the system appearance. `App.css` is a copy of the app stylesheet at the
time; `mock.css` holds mock-only layout.

- `harnesses-homes.html`: both homes installed. The T3 home's `settings.json` is a link to `~/.claude`,
  so its hooks are the Default home's.
- `harnesses-home-missing.html`: T3's second home found but not installed. The card reads
  "1 home not installed" instead of "Recording", and the row offers Install.

Decisions in the mock:

- One Claude Code card, not one card per home. Capture, recall and the verify probe are evidence about
  the harness; hooks and memory tools are installed per home. A card per home would repeat the same
  evidence on each card.
- The card is not green while any home it found is not installed.
- A home whose `settings.json` links to another home's shares that home's hooks. Remove on it takes out
  only its memory tools entry; it never removes hooks another home uses.
