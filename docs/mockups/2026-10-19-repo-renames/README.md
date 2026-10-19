# Settings → Projects: renamed repos (mock, 2026-10-19, for approval) (approved by Matt 2026-10-19)

Static mock for the app part of slice D in `docs/plans/2026-10-19-repo-renames.md`. Open either `.html` in a
browser; it follows the system appearance. `App.css` is a copy of the app stylesheet on main today (v0.4.15);
`mock.css` holds mock-only layout. Each screen is rendered at 1100×720 in light (`<name>.png`) and dark
(`<name>-dark.png`). Times are Monday 2026-10-19; `lollipop-labs`, `field-notes` and `site-2025` are fictional.

- `settings-projects.html`: the new Projects section. Each project that has other names shows its current
  name, what it holds across all its names, and one row per older name with how Khipu knew (GitHub, Same
  checkout, or You), a sentence saying what that means, the date, and Undo. Examples: `mrkinglollipop/olivewood`
  with the older name `mrkinglollipop/church-harness` (GitHub, today), and the folder `field-notes`, renamed from
  `notes-scratch` on the same checkout. Below it, Kept separate lists pairs the owner said are different, each
  with Undo. A last line says the same list and Undo work from any conversation.
- `same-project-prompt.html`: the question "These look like the same project" at the top of Projects, for
  `mrkinglollipop/olivewood` and its fork `lollipop-labs/olivewood`, which share a first commit but nothing
  confirmed a rename. It says why Khipu is asking, that they stay two projects until answered, what Merge and
  Keep separate each do, that either can be undone, and that the owner can answer in any conversation.

Decisions in the mock:

- Projects sits in the Settings list after Work, and carries a count while a question is waiting. The same
  count would show as a Home attention item linking here (not drawn).
- The words are "older name" and "current name"; "alias" does not appear on screen. The current name is the
  newest one, per decision 1 of the scope.
- How Khipu knew is a short tag plus one plain sentence, so the tag never has to explain itself.
- Undo sits on each older name, not on the project, because each name was added by its own evidence and
  removing one must leave the others.
- Merge and Keep separate are equal buttons with no default. A shared first commit is as likely a fork as a
  rename, so the app does not lean either way. Merge offers which name to show, defaulting to the one used
  most recently.
- Keep separate is a recorded answer with its own Undo, so the question never comes back on its own and the
  owner can still change their mind.
- Nothing on either screen needs the app: the conversation line on each points to `khipu_project` and the owner
  actions in chat (decision 4 of the scope).
